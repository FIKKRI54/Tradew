import json
import os
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from llm_advisors import load_llm_config
from mt5_connector import MT5Adapter, RemoteMT5Adapter, create_mt5_adapter


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self):
        return json.dumps(self.payload).encode()


class DeploymentAdapterTest(unittest.TestCase):
    def test_local_execution_sizes_to_half_balance_and_sends_protected_order(self):
        from test_entry_plans import execution_case
        _, config, plan = execution_case()
        Check = namedtuple('Check', 'retcode request comment')
        Sent = namedtuple('Sent', 'retcode request order deal comment')

        class Terminal:
            ACCOUNT_TRADE_MODE_DEMO = 0
            ACCOUNT_TRADE_MODE_REAL = 2
            TIMEFRAME_M5 = 5
            ORDER_TYPE_BUY = 0
            ORDER_TYPE_SELL = 1
            SYMBOL_FILLING_FOK = 1
            SYMBOL_FILLING_IOC = 2
            ORDER_FILLING_FOK = 0
            ORDER_FILLING_IOC = 1
            ORDER_FILLING_RETURN = 2
            TRADE_ACTION_DEAL = 1
            ORDER_TIME_GTC = 0
            TRADE_RETCODE_PLACED = 10008
            TRADE_RETCODE_DONE = 10009
            TRADE_RETCODE_DONE_PARTIAL = 10010

            def __init__(self):
                self.sent = None

            def account_info(self):
                return SimpleNamespace(balance=1000, login=1, server='demo', currency='USC',
                                       trade_mode=0, trade_allowed=True, margin_free=1000)

            def terminal_info(self):
                return SimpleNamespace(connected=True, trade_allowed=True, tradeapi_disabled=False)

            def symbols_get(self):
                return [SimpleNamespace(name='XAUUSD.c', currency_base='XAU')]

            def copy_rates_from_pos(self, *args):
                import time
                t = int(time.time())//300*300
                return [dict(time=t-300, close=101), dict(time=t, close=100.1)]

            def order_calc_margin(self, kind, symbol, volume, entry):
                return 100*volume

            def symbol_info(self, symbol):
                return SimpleNamespace(digits=2, point=.01, trade_stops_level=0,
                    trade_freeze_level=0, volume_max=100, volume_step=.01,
                    volume_min=.01, filling_mode=1)

            def symbol_info_tick(self, symbol):
                import time
                return SimpleNamespace(ask=100.36, bid=100.1, time=int(time.time()))

            def positions_get(self, symbol=None):
                return ()

            def orders_get(self, symbol=None):
                return ()

            def order_calc_profit(self, kind, symbol, volume, entry, stop):
                return (stop-entry)*100*volume

            def order_check(self, request):
                return Check(0, request, 'Done')

            def order_send(self, request):
                self.sent = request
                return Sent(10009, request, 7, 8, 'Done')

        terminal = Terminal()
        result = MT5Adapter(terminal).execute_market_order(
            'BUY', 'XAUUSD.c', 99, 102, .5, 5, 50, 123, '0123456789', plan, config)
        self.assertTrue(result['accepted'])
        self.assertEqual(result['risk_budget'], 500)
        self.assertLessEqual(result['estimated_cash_risk'], 500)
        self.assertEqual(terminal.sent['comment'], 'Tradew S:0123456789')
        self.assertEqual(terminal.sent['sl'], 99)
        self.assertEqual(terminal.sent['tp'], 102)

    def test_local_auto_trailing_preserves_tp_and_never_widens_stop(self):
        Check = namedtuple('Check', 'retcode request comment')
        Sent = namedtuple('Sent', 'retcode request order deal comment')

        class Terminal:
            ORDER_TYPE_BUY = 0
            TRADE_ACTION_SLTP = 6
            TRADE_RETCODE_PLACED = 10008
            TRADE_RETCODE_DONE = 10009
            TRADE_RETCODE_DONE_PARTIAL = 10010

            def __init__(self):
                self.position = SimpleNamespace(ticket=7, type=0, sl=95, tp=110)
                self.sent = None

            def positions_get(self, symbol=None):
                return (self.position,)

            def symbol_info(self, symbol):
                return SimpleNamespace(digits=2, point=.01, trade_stops_level=0,
                                       trade_freeze_level=0)

            def symbol_info_tick(self, symbol):
                return SimpleNamespace(bid=105, ask=105.1)

            def order_check(self, request):
                return Check(0, request, 'Done')

            def order_send(self, request):
                self.sent = request
                return Sent(10009, request, 7, 8, 'Done')

        terminal = Terminal()
        adapter = MT5Adapter(terminal)
        widened = adapter.modify_position_stop(7, 'XAUUSD.c', 94, 123)
        self.assertEqual(widened['status'], 'stop_does_not_improve')
        result = adapter.modify_position_stop(7, 'XAUUSD.c', 101, 123)
        self.assertTrue(result['accepted'])
        self.assertEqual(terminal.sent['sl'], 101)
        self.assertEqual(terminal.sent['tp'], 110)

    def test_remote_adapter_sends_token_and_payload(self):
        seen = {}

        def opener(request, timeout):
            seen['url'] = request.full_url
            seen['token'] = request.get_header('X-tradew-bridge-token')
            seen['payload'] = json.loads(request.data)
            seen['timeout'] = timeout
            return Response({'value': -12.5})

        adapter = RemoteMT5Adapter('http://127.0.0.1:5010', 'secret', opener=opener)
        self.assertEqual(adapter.profit(0, 'XAUUSD.c', .1, 100, 99), -12.5)
        self.assertEqual(seen['url'], 'http://127.0.0.1:5010/profit')
        self.assertEqual(seen['token'], 'secret')
        self.assertEqual(seen['payload']['symbol'], 'XAUUSD.c')

    def test_factory_uses_bridge_when_configured(self):
        with patch.dict(os.environ, {
                'TRADEW_MT5_BRIDGE_URL': 'http://127.0.0.1:5010',
                'TRADEW_BRIDGE_TOKEN': 'secret'}, clear=False):
            self.assertIsInstance(create_mt5_adapter(), RemoteMT5Adapter)

    def test_bridge_execution_route_is_private_and_authenticated(self):
        try:
            import mt5_bridge
        except ModuleNotFoundError as exc:
            if exc.name == 'flask':
                self.skipTest('Flask is not installed in this interpreter')
            raise
        routes = {rule.rule for rule in mt5_bridge.app.url_map.iter_rules()}
        self.assertNotIn('/order', routes)
        self.assertIn('/execute', routes)
        self.assertIn('/pending', routes)
        self.assertIn('/position/stop', routes)
        self.assertIn('/positions/close-half/preview', routes)
        self.assertIn('/positions/close-half', routes)
        with patch.dict(os.environ, {'TRADEW_BRIDGE_TOKEN': 'secret'}, clear=False):
            client = mt5_bridge.app.test_client()
            self.assertEqual(client.get('/health').status_code, 401)
            response = client.get('/health', headers={'X-Tradew-Bridge-Token': 'secret'})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()['execution_authority'])

    def test_bridge_accepts_only_exact_independent_risk_tier_mapping(self):
        try:
            from mt5_bridge import _execution_configuration_matches
        except ModuleNotFoundError as exc:
            if exc.name == 'flask':
                self.skipTest('Flask is not installed in this interpreter')
            raise
        config = {
            'automatic_execution': True, 'balance_risk_fraction': .3,
            'cost_per_lot': 5,
        }
        independent = {
            'constraints': config, 'cost_per_lot': 5, 'risk_fraction': .1,
            'plan': {
                'source': 'independent_llm', 'risk_tier': 'LOW',
                'risk_reason': 'Valid setup with meaningful uncertainty.',
                'risk_fraction': .1,
            },
        }
        self.assertTrue(_execution_configuration_matches(independent, config))
        self.assertFalse(_execution_configuration_matches(
            {**independent, 'risk_fraction': .2}, config))
        self.assertFalse(_execution_configuration_matches({
            **independent, 'plan': {**independent['plan'], 'risk_tier': 'HIGH'}}, config))
        self.assertFalse(_execution_configuration_matches({
            **independent, 'plan': {**independent['plan'], 'risk_reason': ''}}, config))
        deterministic = {
            'constraints': config, 'cost_per_lot': 5, 'risk_fraction': .3,
            'plan': {'source': 'deterministic', 'risk_fraction': .3},
        }
        self.assertTrue(_execution_configuration_matches(deterministic, config))
        self.assertFalse(_execution_configuration_matches({
            **deterministic, 'risk_fraction': .1,
            'plan': {**deterministic['plan'], 'risk_fraction': .1}}, config))

    def test_portfolio_api_requires_pause_preview_and_exact_confirmation(self):
        try:
            import api_server
        except ModuleNotFoundError as exc:
            if exc.name == 'flask':
                self.skipTest('Flask is not installed in this interpreter')
            raise

        class Broker:
            def __init__(self):
                self.executions = 0

            def preview_half_close(self, symbol, config):
                return {'accepted': True, 'symbol': symbol,
                        'snapshot_token': 'a'*64, 'close_target_volume': 1.0}

            def close_half_positions(self, symbol, token, config):
                self.executions += 1
                return {'accepted': True, 'complete': True, 'symbol': symbol,
                        'verified_closed_volume': 1.0, 'remaining_volume': 1.0,
                        'remaining_position_count': 2}

            def close(self):
                return None

        broker = Broker()
        config = {'symbol': 'XAUUSD.c'}
        client = api_server.app.test_client()
        with (patch.object(api_server, '_portfolio_action_paused', return_value=True),
              patch.object(api_server.main, 'load_config', return_value=config),
              patch.object(api_server.main, 'create_mt5_adapter', return_value=broker),
              patch.object(api_server, '_record_portfolio_action', return_value=None)):
            preview = client.post('/api/portfolio/close-half/preview', json={})
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.get_json()['snapshot_token'], 'a'*64)
            rejected = client.post('/api/portfolio/close-half', json={
                'confirmation': 'close half', 'snapshot_token': 'a'*64})
            self.assertEqual(rejected.status_code, 400)
            executed = client.post('/api/portfolio/close-half', json={
                'confirmation': 'CLOSE 50%', 'snapshot_token': 'a'*64})
            self.assertEqual(executed.status_code, 200)
            self.assertTrue(executed.get_json()['complete'])
            self.assertEqual(broker.executions, 1)
        with patch.object(api_server, '_portfolio_action_paused', return_value=False):
            response = client.post('/api/portfolio/close-half/preview', json={})
            self.assertEqual(response.status_code, 409)

    def test_server_environment_can_select_local_gemini(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.joinpath('llm_config.json').write_text(json.dumps({
                'enabled': True, 'mode': 'single_provider', 'default_provider': 'astra',
                'codex': {'enabled': True, 'command': 'codex', 'model': 'astra',
                          'reasoning_effort': 'low',
                          'timeout_seconds': 30},
                'gemini': {'enabled': True, 'model': 'gemini', 'timeout_seconds': 30,
                           'transport': 'ssh', 'ssh_host': 'example',
                           'remote_python': '/python', 'remote_script': '/worker'},
            }), encoding='utf-8')
            with patch.dict(os.environ, {
                    'TRADEW_LLM_DEFAULT_PROVIDER': 'gemini',
                    'TRADEW_GEMINI_TRANSPORT': 'local_worker'}, clear=False):
                config = load_llm_config(root)
            self.assertEqual(config['default_provider'], 'gemini')
            self.assertEqual(config['gemini']['transport'], 'local_worker')


if __name__ == '__main__':
    unittest.main()
