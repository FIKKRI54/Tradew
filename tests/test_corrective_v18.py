import tempfile
import threading
import time
import unittest
from collections import namedtuple

from auto_execution import authorization_result
from limit_ladder import attach_limit_ladder
from mt5_connector import MT5Adapter, _execution_history_blocker
from scan_engine import confirmed_retest
from setup_tracker import SetupTracker
from test_entry_plans import Terminal, execution_case


class CorrectiveV18Tests(unittest.TestCase):
    def test_completed_rejection_is_required_after_confirmation(self):
        zone = {'low': 100, 'high': 100.25}
        bars = [
            {'time': 60, 'open': 100.1, 'high': 100.3, 'low': 99.9, 'close': 100.2},
            {'time': 120, 'open': 100.1, 'high': 100.5, 'low': 99.9, 'close': 100.4},
        ]
        self.assertIsNone(confirmed_retest(bars, zone, 'BUY', 120))
        self.assertEqual(confirmed_retest(bars, zone, 'BUY', 60), 120)

    def test_v18_sender_reproduces_retest_from_broker_history(self):
        class RetestTerminal(Terminal):
            TIMEFRAME_M1 = 1

            def copy_rates_from_pos(self, symbol, timeframe, *_):
                if timeframe == self.TIMEFRAME_M1:
                    retest = self.tick.time//60*60-60
                    return [
                        {'time': retest, 'open': 100.1, 'high': 100.5,
                         'low': 99.9, 'close': 100.4},
                        {'time': retest+60, 'open': 101, 'high': 101,
                         'low': 101, 'close': 101},
                    ]
                return super().copy_rates_from_pos(symbol, timeframe)

        terminal = RetestTerminal()
        terminal.tick.bid, terminal.tick.ask = 101, 101.26
        _, config, plan = execution_case()
        retest = terminal.tick.time//60*60-60
        plan.update(rule_version='structure-research-18',
                    confirmation_time=retest-60, retest_confirmation_time=retest)
        accepted = MT5Adapter(terminal).place_pending_limit(
            'BUY', 'XAUUSD.c', 99, 102, .5, 5, 123, '0123456789', plan, config)
        self.assertTrue(accepted['accepted'])
        plan['retest_confirmation_time'] = retest+1
        second = RetestTerminal()
        second.tick.bid, second.tick.ask = 101, 101.26
        rejected = MT5Adapter(second).place_pending_limit(
            'BUY', 'XAUUSD.c', 99, 102, .5, 5, 123, '0123456789', plan, config)
        self.assertEqual(rejected['status'], 'blocked_retest_not_reproduced')

    def test_v19_sender_rejects_narrow_ladder_before_any_send(self):
        terminal = Terminal()
        terminal.tick.bid, terminal.tick.ask = 101, 101.26
        _, config, plan = execution_case()
        plan.update(rule_version='independent-auto-19', source='independent_llm',
                    pending_price=100.52, level=100.31, m5_atr=1)
        plan = attach_limit_ladder(
            plan, terminal.tick.bid, terminal.tick.ask, config)
        result = MT5Adapter(terminal).place_pending_limit(
            'BUY', 'XAUUSD.c', 99, 102, .5, 5, 123,
            '0123456789', plan, config)
        self.assertEqual(result['status'], 'blocked_ladder_span_too_narrow')
        self.assertFalse(result['send_attempted'])
        self.assertFalse(terminal.sent)
        self.assertLess(result['ladder_span'], result['minimum_ladder_span'])

    def test_v20_sender_accepts_verified_reduced_single_limit(self):
        terminal = Terminal()
        terminal.tick.bid, terminal.tick.ask = 101, 101.26
        _, config, plan = execution_case()
        plan.update(rule_version='independent-auto-20', source='independent_llm',
                    pending_price=100.52, level=100.31, m5_atr=1,
                    targets=[103])
        plan = attach_limit_ladder(
            plan, terminal.tick.bid, terminal.tick.ask, config)
        result = MT5Adapter(terminal).place_pending_limit(
            'BUY', 'XAUUSD.c', 99, 103, .5, 5, 123,
            '0123456789', plan, config)
        self.assertTrue(result['accepted'])
        self.assertEqual(result['layers_submitted'], 1)
        self.assertEqual(len(terminal.sent), 1)
        self.assertEqual(terminal.sent[0]['price'], 100.52)
        self.assertLessEqual(result['estimated_cash_risk'], 125)

    def test_v20_sender_still_rejects_narrow_deterministic_ladder(self):
        terminal = Terminal()
        terminal.tick.bid, terminal.tick.ask = 101, 101.26
        _, config, plan = execution_case()
        plan.update(rule_version='structure-research-20', pending_price=100.52,
                    level=100.31, m5_atr=1, targets=[103])
        plan = attach_limit_ladder(
            plan, terminal.tick.bid, terminal.tick.ask, config)
        result = MT5Adapter(terminal).place_pending_limit(
            'BUY', 'XAUUSD.c', 99, 103, .5, 5, 123,
            '0123456789', plan, config)
        self.assertEqual(result['status'], 'blocked_ladder_span_too_narrow')
        self.assertFalse(terminal.sent)

    def test_loss_history_brakes_new_submission(self):
        Deal = namedtuple('Deal', 'entry magic position_id comment time profit commission swap fee')
        now = time.time()
        deals = [
            Deal(0, 77, 1, 'Tradew S:0123456789', now-120, 0, 0, 0, 0),
            Deal(1, 77, 1, '', now-60, -100, 0, 0, 0),
        ]

        class History:
            DEAL_ENTRY_IN = 0
            DEAL_ENTRY_OUT = 1
            DEAL_ENTRY_OUT_BY = 3

            def history_deals_get(self, *_):
                return deals

        constraints = dict(max_session_entries=3, max_consecutive_losses=2,
                           loss_cooldown_seconds=900)
        account = type('Account', (), {'balance': 900})()
        self.assertEqual(_execution_history_blocker(
            History(), account, 77, constraints, now), 'loss_cooldown')

    def test_realized_daily_loss_does_not_block_submission(self):
        Deal = namedtuple('Deal', 'entry magic position_id comment time profit commission swap fee')
        now = time.time()
        deals = [
            Deal(0, 77, 1, 'Tradew S:0123456789', now-120, 0, 0, 0, 0),
            Deal(1, 77, 1, '', now-60, -900, 0, 0, 0),
        ]

        class History:
            DEAL_ENTRY_IN = 0
            DEAL_ENTRY_OUT = 1
            DEAL_ENTRY_OUT_BY = 3

            def history_deals_get(self, *_):
                return deals

        constraints = dict(max_session_entries=3, max_consecutive_losses=2,
                           loss_cooldown_seconds=0)
        account = type('Account', (), {'balance': 100})()
        self.assertIsNone(_execution_history_blocker(
            History(), account, 77, constraints, now))

    def test_tracker_serializes_concurrent_setup_records(self):
        with tempfile.TemporaryDirectory() as folder:
            identity = {'account': {'login': 1, 'server': 'demo', 'currency': 'USC'},
                        'account_mode': 'demo'}

            def record(index):
                SetupTracker(folder).record({
                    'setup_id': f'XAUUSD.c:test:BUY:{index}:100',
                    'symbol': 'XAUUSD.c', 'planned_direction': 'BUY',
                    'setup_state': 'WAIT_RETEST', 'snapshot_time': index,
                }, identity)

            threads = [threading.Thread(target=record, args=(index,)) for index in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(SetupTracker(folder).state['setups']), 12)

    def test_pending_sibling_prevents_premature_setup_closure(self):
        with tempfile.TemporaryDirectory() as folder:
            tracker = SetupTracker(folder)
            identity = {'account': {'login': 1, 'server': 'demo', 'currency': 'USC'},
                        'account_mode': 'demo'}
            result = {'setup_id': 'XAUUSD.c:test:BUY:1:100', 'symbol': 'XAUUSD.c',
                      'planned_direction': 'BUY', 'setup_state': 'ARMED_LIMIT',
                      'snapshot_time': 1}
            tracker.record(result, identity)
            ref = result['setup_ref']
            deals = [
                {'ticket': 1, 'position_id': 10, 'entry_role': 'IN',
                 'comment': f'Tradew S:{ref} L1', 'profit': 0, 'commission': 0,
                 'swap': 0, 'fee': 0, 'time': 2},
                {'ticket': 2, 'position_id': 10, 'entry_role': 'OUT', 'comment': '',
                 'profit': -5, 'commission': 0, 'swap': 0, 'fee': 0, 'time': 3},
            ]
            snapshot = {**identity, 'retrieval_end_utc': 4, 'positions': [],
                        'pending_orders': [{'comment': f'Tradew S:{ref} L2'}],
                        'recent_deals': deals}
            tracker.attach(snapshot)
            self.assertNotEqual(tracker.state['setups'][ref]['state'], 'CLOSED')
            snapshot['pending_orders'] = []
            tracker.attach(snapshot)
            self.assertEqual(tracker.state['setups'][ref]['state'], 'CLOSED')

    def test_independent_ready_read_authorizes_its_own_direction(self):
        result = authorization_result(
            'independent', {'snapshot_id': 's', 'action': 'WAIT'},
            independent_result={'snapshot_id': 's', 'analysis': {
                'status': 'ok', 'setup_status': 'READY',
                'recommended_action': 'SELL', 'risk_tier': 'MEDIUM',
                'trade_plan': {'direction': 'SELL'}}})
        self.assertEqual(result['status'], 'AUTHORIZED')
        self.assertEqual(result['direction'], 'SELL')


if __name__ == '__main__':
    unittest.main()
