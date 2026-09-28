import copy
import tempfile
import time
import unittest
from collections import namedtuple
from types import SimpleNamespace
from unittest.mock import patch

import automation
from auto_execution import (authorization_result, execute_if_fresh,
                            supervise_pending_orders)
from entry_plan import plan_key, quote_blocker
from mt5_connector import MT5Adapter
from limit_ladder import attach_limit_ladder
from scan_engine import _evaluate_candidate
from test_entry_gates import Broker
import test_scan_engine


def execution_case():
    snapshot, config = test_scan_engine.RulesTest().fixture()
    now = time.time()
    config.update(max_spread=.5, cost_per_lot=5, expected_login=1,
                  timestamp_offset_seconds=0, minimum_ladder_reward_risk=1.0,
                  runner_enabled=False)
    plan = dict(rule_version='structure-research-9', setup_id='setup-1', symbol='XAUUSD.c',
                direction='BUY', account=dict(login=1, server='demo', currency='USC', mode='demo'),
                level=100, structural_invalidation=98,
                zone=dict(low=100, high=100.26, trigger_side='BID', direction='BUY'),
                stop=99, targets=[102], expires_at=now+300,
                confirmation_time=int(now)//300*300-300, risk_fraction=.5, cost_per_lot=5)
    return snapshot, config, plan


class Terminal:
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_REAL = 2
    TIMEFRAME_M5 = 5
    SYMBOL_FILLING_FOK = 1
    SYMBOL_FILLING_IOC = 2
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_REMOVE = 8
    ORDER_TIME_GTC = 0
    ORDER_TIME_SPECIFIED = 2
    SYMBOL_EXPIRATION_SPECIFIED = 4
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_DONE_PARTIAL = 10010
    TRADE_RETCODE_PLACED = 10008

    def __init__(self):
        self.tick = SimpleNamespace(time=int(time.time()), bid=100.1, ask=100.36)
        self.account = SimpleNamespace(balance=1000, login=1, server='demo', currency='USC',
                                       trade_mode=0, trade_allowed=True, margin_free=1000)
        self.positions = []
        self.sent = []
        self.reclaimed = False
        self.after_check = lambda: None

    def account_info(self):
        return self.account

    def symbol_info(self, symbol):
        return SimpleNamespace(digits=2, point=.01, trade_stops_level=0,
                               trade_freeze_level=0, volume_max=100, volume_min=.01,
                               volume_step=.01, filling_mode=1, trade_tick_size=.01,
                               expiration_mode=15)

    def symbol_info_tick(self, symbol):
        return copy.copy(self.tick)

    def symbols_get(self):
        return [SimpleNamespace(name=name, currency_base='XAU') for name in ('XAUUSD.c', 'XAUEUR')]

    def terminal_info(self):
        return SimpleNamespace(connected=True, trade_allowed=True, tradeapi_disabled=False)

    def positions_get(self):
        return self.positions

    def orders_get(self):
        return []

    def copy_rates_from_pos(self, *args):
        t = self.tick.time//300*300
        return [dict(time=t-600, close=101),
                dict(time=t-300, close=99 if self.reclaimed else 101),
                dict(time=t, close=100.1)]

    def order_calc_profit(self, kind, symbol, volume, entry, exit):
        return (exit-entry)*100*volume*(1 if kind == 0 else -1)

    def order_calc_margin(self, kind, symbol, volume, entry):
        return 100*volume

    def order_check(self, request):
        self.after_check()
        return namedtuple('Check', 'retcode request comment')(0, request, 'Done')

    def order_send(self, request):
        self.sent.append(request)
        return namedtuple('Sent', 'retcode request order deal')(10009, request, 7, 8)


class EntryPlanTests(unittest.TestCase):
    @staticmethod
    def pending_terminal(**changes):
        terminal = Terminal()
        _, _, plan = execution_case()
        order = dict(ticket=77, symbol='XAUUSD.c', magic=123, type=2,
                     comment='Tradew S:0123456789', price_open=100.52,
                     sl=99, tp=102, volume_current=.1, volume_initial=.1)
        order.update(changes)
        terminal.pending_orders = [SimpleNamespace(**order)]
        terminal.orders_get = lambda: list(terminal.pending_orders)
        return terminal, plan, {
            'pending_price': 100.52, 'stop': 99, 'target': 102, 'volume': .1}

    def test_buy_zone_width_equal_to_spread_is_not_a_quality_ladder(self):
        snapshot, config, _ = execution_case()
        snapshot['tick'].update(bid=100.1, ask=100.36)
        setup = dict(direction='BUY', family='breakout_follow_through', level=100,
                     invalidation=98, confirmation_time=snapshot['bars']['M5'][-2]['time'],
                     confirmation_close=101, state='CONFIRMED')
        result = dict(action='WAIT', structure={'M5': {'atr14_simple': 1}}, risk_currency='USC')
        result = _evaluate_candidate(snapshot, config, Broker(), result,
                                     ('BUY', 0, 100, 100.36, 97.7, [110], 60, setup), .26)
        self.assertEqual(result['action'], 'WAIT')
        self.assertEqual(result['setup_state'], 'FILTERED')
        self.assertIn('below the required', result['reasons'][0])
        self.assertEqual(result['planned_entry_zone']['trigger_side'], 'BID')

    def submit(self, terminal, plan=None):
        _, config, default = execution_case()
        plan = plan or default
        return MT5Adapter(terminal).execute_market_order(
            'BUY', 'XAUUSD.c', 99, 102, .5, 5, 50, 123, '0123456789', plan, config)

    def test_sender_rejects_quote_outside_zone_and_never_sends(self):
        terminal = Terminal()
        terminal.tick.bid, terminal.tick.ask = 100.5, 100.76
        self.assertEqual(self.submit(terminal)['status'], 'blocked_outside_entry_zone')
        self.assertFalse(terminal.sent)

    def test_sender_rechecks_all_gold_account_and_quote_age(self):
        for kind, expected in [('exposure', 'blocked_existing_exposure'),
                               ('account', 'blocked_account_changed'),
                               ('stale', 'blocked_stale_quote'),
                               ('spread', 'blocked_spread_exceeded')]:
            terminal = Terminal()
            if kind == 'exposure':
                terminal.positions = [SimpleNamespace(symbol='XAUEUR')]
            elif kind == 'account':
                terminal.account.login = 999
            elif kind == 'stale':
                terminal.tick.time -= 100
            else:
                terminal.tick.ask = 101
            with self.subTest(kind=kind):
                self.assertEqual(self.submit(terminal)['status'], expected)
                self.assertFalse(terminal.sent)

    def test_sender_allows_unchanged_owned_runner_at_breakeven(self):
        terminal = Terminal()
        terminal.positions = [SimpleNamespace(
            ticket=88, symbol='XAUUSD.c', type=0, volume=.1,
            price_open=95, sl=95, tp=0, magic=123,
            comment='Tradew S:1111111111 L1R')]
        result = self.submit(terminal)
        self.assertTrue(result['accepted'])
        self.assertTrue(terminal.sent)

    def test_sender_blocks_runner_whose_stop_is_below_entry(self):
        terminal = Terminal()
        terminal.positions = [SimpleNamespace(
            ticket=88, symbol='XAUUSD.c', type=0, volume=.1,
            price_open=95, sl=94.99, tp=0, magic=123,
            comment='Tradew S:1111111111 L1R')]
        self.assertEqual(self.submit(terminal)['status'],
                         'blocked_existing_exposure')
        self.assertFalse(terminal.sent)

    def test_sender_rejects_completed_reclaim(self):
        terminal = Terminal()
        terminal.reclaimed = True
        _, _, plan = execution_case()
        plan['confirmation_time'] -= 300
        self.assertEqual(self.submit(terminal, plan)['status'], 'blocked_completed_reclaim')

    def test_armed_plan_places_native_expiring_buy_limit(self):
        terminal = Terminal()
        terminal.tick.bid, terminal.tick.ask = 101, 101.26
        _, config, plan = execution_case()
        result = MT5Adapter(terminal).place_pending_limit(
            'BUY', 'XAUUSD.c', 99, 102, .5, 5, 123, '0123456789', plan, config)
        self.assertTrue(result['accepted'])
        self.assertEqual(result['status'], 'pending_submitted')
        self.assertEqual(result['execution_type'], 'BROKER_LIMIT')
        self.assertEqual(terminal.sent[0]['action'], terminal.TRADE_ACTION_PENDING)
        self.assertEqual(terminal.sent[0]['type'], terminal.ORDER_TYPE_BUY_LIMIT)
        self.assertEqual(terminal.sent[0]['price'], 100.52)
        self.assertEqual(terminal.sent[0]['expiration'], int(plan['expires_at']//60*60))
        self.assertEqual(terminal.sent[0]['sl'], 99)
        self.assertEqual(terminal.sent[0]['tp'], 102)

    def test_pending_expiration_is_translated_back_to_broker_time(self):
        terminal = Terminal()
        terminal.tick.bid, terminal.tick.ask = 101, 101.26
        _, config, plan = execution_case()
        config['timestamp_offset_seconds'] = 10800
        terminal.tick.time += 10800
        result = MT5Adapter(terminal).place_pending_limit(
            'BUY', 'XAUUSD.c', 99, 102, .5, 5, 123, '0123456789', plan, config)
        self.assertTrue(result['accepted'])
        expected = int((plan['expires_at']+10800)//60*60)
        self.assertEqual(terminal.sent[0]['expiration'], expected)
        self.assertEqual(result['broker_expiration'], expected)
        self.assertEqual(result['expiration'], int(plan['expires_at']))

    def test_armed_sell_plan_places_sell_limit_above_market(self):
        terminal = Terminal()
        terminal.tick.bid, terminal.tick.ask = 99, 99.26
        _, config, plan = execution_case()
        plan.update(direction='SELL', level=100, structural_invalidation=102,
                    zone=dict(low=99.74, high=100, trigger_side='BID', direction='SELL'),
                    stop=101, targets=[98])
        result = MT5Adapter(terminal).place_pending_limit(
            'SELL', 'XAUUSD.c', 101, 98, .5, 5, 123, '0123456789', plan, config)
        self.assertTrue(result['accepted'])
        self.assertEqual(terminal.sent[0]['type'], terminal.ORDER_TYPE_SELL_LIMIT)
        self.assertEqual(terminal.sent[0]['price'], 99.74)

    def test_independent_plan_uses_its_explicit_native_limit_price(self):
        terminal = Terminal()
        terminal.tick.bid, terminal.tick.ask = 101, 101.26
        _, config, plan = execution_case()
        plan.update(source='independent_llm', pending_price=100.7)
        result = MT5Adapter(terminal).place_pending_limit(
            'BUY', 'XAUUSD.c', 99, 102, .5, 5, 123, '0123456789', plan, config)
        self.assertTrue(result['accepted'])
        self.assertEqual(terminal.sent[0]['price'], 100.7)

    def test_limit_ladder_splits_one_risk_budget_across_three_prices(self):
        terminal = Terminal()
        terminal.tick.bid, terminal.tick.ask = 101, 101.26
        _, config, plan = execution_case()
        config['minimum_ladder_reward_risk'] = .8  # Every fixture layer passes its own floor.
        plan = attach_limit_ladder(plan, terminal.tick.bid, terminal.tick.ask, config)
        result = MT5Adapter(terminal).place_pending_limit(
            'BUY', 'XAUUSD.c', 99, 102, .5, 5, 123, '0123456789', plan, config)
        self.assertTrue(result['accepted'])
        self.assertEqual(result['status'], 'pending_ladder_submitted')
        self.assertEqual(result['layers_submitted'], 3)
        self.assertEqual(len(terminal.sent), 3)
        self.assertEqual([item['price'] for item in terminal.sent], [100.52, 100.39, 100.26])
        self.assertTrue(all(item['sl'] == 99 and item['tp'] == 102
                            for item in terminal.sent))
        self.assertLessEqual(result['estimated_cash_risk'], result['risk_budget'])
        self.assertAlmostEqual(result['risk_budget'], 500)
        self.assertGreater(result['orders'][2]['volume'], result['orders'][0]['volume'])

    def test_limit_ladder_splits_each_layer_into_tp_and_runner_children(self):
        baseline_terminal = Terminal()
        baseline_terminal.tick.bid, baseline_terminal.tick.ask = 101, 101.26
        _, config, plan = execution_case()
        config['minimum_ladder_reward_risk'] = .5  # TP1 portion alone passes for each layer.
        plan = attach_limit_ladder(plan, baseline_terminal.tick.bid,
                                   baseline_terminal.tick.ask, config)
        baseline = MT5Adapter(baseline_terminal).place_pending_limit(
            'BUY', 'XAUUSD.c', 99, 102, .5, 5, 123,
            '0123456789', plan, config)

        terminal = Terminal()
        terminal.tick.bid, terminal.tick.ask = 101, 101.26
        config['runner_enabled'] = True
        split = MT5Adapter(terminal).place_pending_limit(
            'BUY', 'XAUUSD.c', 99, 102, .5, 5, 123,
            '0123456789', plan, config)

        self.assertTrue(split['accepted'])
        self.assertEqual(split['execution_type'], 'BROKER_LIMIT_LADDER_TP_RUNNER')
        self.assertEqual(split['layers_submitted'], 3)
        self.assertEqual(split['orders_submitted'], 6)
        self.assertEqual(len(terminal.sent), 6)
        self.assertEqual([request['tp'] for request in terminal.sent],
                         [102, 0.0, 102, 0.0, 102, 0.0])
        self.assertEqual([request['comment'][-1] for request in terminal.sent],
                         ['T', 'R', 'T', 'R', 'T', 'R'])
        self.assertAlmostEqual(split['total_volume'], baseline['total_volume'])
        self.assertAlmostEqual(split['estimated_cash_risk'],
                               baseline['estimated_cash_risk'])

    def test_market_order_splits_tp_and_runner_without_changing_total_risk(self):
        baseline_terminal = Terminal()
        _, config, plan = execution_case()
        baseline = MT5Adapter(baseline_terminal).execute_market_order(
            'BUY', 'XAUUSD.c', 99, 102, .5, 5, 50, 123,
            '0123456789', plan, config)

        terminal = Terminal()
        config['runner_enabled'] = True
        split = MT5Adapter(terminal).execute_market_order(
            'BUY', 'XAUUSD.c', 99, 102, .5, 5, 50, 123,
            '0123456789', plan, config)

        self.assertTrue(split['accepted'])
        self.assertEqual(split['execution_type'], 'MARKET_TP_RUNNER')
        self.assertEqual(len(terminal.sent), 2)
        self.assertEqual([request['tp'] for request in terminal.sent], [102, 0.0])
        self.assertEqual([request['comment'][-1] for request in terminal.sent], ['T', 'R'])
        self.assertAlmostEqual(split['volume'], baseline['volume'])
        self.assertAlmostEqual(split['estimated_cash_risk'],
                               baseline['estimated_cash_risk'])

    def test_ladder_skips_a_reached_near_layer_without_chasing_it(self):
        terminal = Terminal()
        terminal.tick.bid, terminal.tick.ask = 101, 101.26
        _, config, plan = execution_case()
        plan = attach_limit_ladder(plan, terminal.tick.bid, terminal.tick.ask, config)
        terminal.tick.bid, terminal.tick.ask = 100.19, 100.45
        result = MT5Adapter(terminal).place_pending_limit(
            'BUY', 'XAUUSD.c', 99, 102, .5, 5, 123, '0123456789', plan, config)
        self.assertTrue(result['accepted'])
        self.assertEqual(result['status'], 'pending_ladder_partial')
        self.assertEqual(result['layers_submitted'], 2)
        self.assertEqual([item['price'] for item in terminal.sent], [100.39, 100.26])
        self.assertIn('price_already_reached',
                      {item['status'] for item in result['layer_checks']})

    def test_pending_limit_requires_price_to_remain_on_placement_side(self):
        terminal = Terminal()
        _, config, plan = execution_case()
        # Already in the BUY zone: the immediate market path owns this state.
        self.assertEqual(MT5Adapter(terminal).place_pending_limit(
            'BUY', 'XAUUSD.c', 99, 102, .5, 5, 123, '0123456789', plan, config)['status'],
            'blocked_pending_price_too_close')
        self.assertFalse(terminal.sent)

    def test_pending_order_check_failure_is_confirmed_pre_send(self):
        class BrokenCheck(Terminal):
            def order_check(self, request):
                return None
            def last_error(self):
                return (-2, 'invalid expiration')
        terminal = BrokenCheck()
        terminal.tick.bid, terminal.tick.ask = 101, 101.26
        _, config, plan = execution_case()
        result = MT5Adapter(terminal).place_pending_limit(
            'BUY', 'XAUUSD.c', 99, 102, .5, 5, 123, '0123456789', plan, config)
        self.assertEqual(result['status'], 'order_check_unavailable')
        self.assertFalse(result['send_attempted'])
        self.assertFalse(terminal.sent)

    def test_pending_supervisor_keeps_valid_owned_limit(self):
        terminal, plan, expected = self.pending_terminal()
        _, config, _ = execution_case()
        result = MT5Adapter(terminal).revalidate_pending_limit(
            77, 'XAUUSD.c', 123, '0123456789', plan, expected, config)
        self.assertEqual(result['status'], 'pending_still_valid')
        self.assertFalse(terminal.sent)

    def test_pending_supervisor_cancels_completed_m5_reclaim(self):
        terminal, plan, expected = self.pending_terminal()
        _, config, _ = execution_case()
        terminal.reclaimed = True
        plan['confirmation_time'] -= 300
        result = MT5Adapter(terminal).revalidate_pending_limit(
            77, 'XAUUSD.c', 123, '0123456789', plan, expected, config)
        self.assertEqual(result['status'], 'pending_cancelled')
        self.assertEqual(result['invalidation_reason'], 'completed_m5_reclaim')
        self.assertEqual(terminal.sent[0]['action'], terminal.TRADE_ACTION_REMOVE)
        self.assertEqual(terminal.sent[0]['order'], 77)

    def test_pending_supervisor_cancels_changed_terms_but_not_foreign_order(self):
        _, config, _ = execution_case()
        terminal, plan, expected = self.pending_terminal(tp=101.5)
        result = MT5Adapter(terminal).revalidate_pending_limit(
            77, 'XAUUSD.c', 123, '0123456789', plan, expected, config)
        self.assertEqual(result['invalidation_reason'], 'order_terms_changed')
        self.assertEqual(result['status'], 'pending_cancelled')
        foreign, plan, expected = self.pending_terminal(magic=999)
        result = MT5Adapter(foreign).revalidate_pending_limit(
            77, 'XAUUSD.c', 123, '0123456789', plan, expected, config)
        self.assertEqual(result['status'], 'blocked_order_identity_mismatch')
        self.assertFalse(foreign.sent)

    def test_pending_supervisor_allows_same_setup_ladder_siblings(self):
        terminal, plan, expected = self.pending_terminal()
        _, config, _ = execution_case()
        sibling = SimpleNamespace(
            ticket=78, symbol='XAUUSD.c', magic=123, type=2,
            comment='Tradew S:0123456789 L2', price_open=100.3,
            sl=99, tp=102, volume_current=.1, volume_initial=.1)
        terminal.pending_orders.append(sibling)
        terminal.positions = [SimpleNamespace(
            ticket=79, symbol='XAUUSD.c', magic=123,
            comment='Tradew S:0123456789 L1')]
        result = MT5Adapter(terminal).revalidate_pending_limit(
            77, 'XAUUSD.c', 123, '0123456789', plan, expected, config)
        self.assertEqual(result['status'], 'pending_still_valid')
        self.assertFalse(terminal.sent)

    def test_scan_supervisor_recovers_submitted_plan_and_checks_exact_ticket(self):
        _, config, plan = execution_case()
        config['execution_magic'] = 123
        execution = {'accepted': True, 'status': 'pending_submitted',
                     'pending_price': 100.52, 'stop': 99, 'target': 102,
                     'volume': .1, 'result': {'order': 77}}
        calls = []
        class Supervisor:
            def revalidate_pending_limit(self, *args):
                calls.append(args)
                return {'accepted': True, 'status': 'pending_still_valid', 'ticket': 77}
            def close(self):
                pass
        decision = {'data_quality': [], 'positions_summary': {'pending_orders': [
            {'ticket': 77, 'symbol': 'XAUUSD.c', 'magic': 123,
             'comment': 'Tradew S:0123456789'}]}}
        with tempfile.TemporaryDirectory() as folder:
            from pathlib import Path
            import json
            path = Path(folder)/'auto_executions.jsonl'
            path.write_text(json.dumps({
                'event': 'ATTEMPT_FINISHED', 'setup_id': 'setup-1',
                'setup_ref': '0123456789', 'entry_plan': plan,
                'execution': execution})+'\n', encoding='utf-8')
            result = supervise_pending_orders(decision, config, folder, Supervisor)
        self.assertEqual(result[0]['status'], 'pending_still_valid')
        self.assertEqual(calls[0][0], 77)
        self.assertEqual(calls[0][4], plan)

    def test_scan_supervisor_recovers_every_ladder_ticket(self):
        _, config, plan = execution_case()
        config['execution_magic'] = 123
        execution = {
            'accepted': True, 'status': 'pending_ladder_submitted',
            'broker_expiration': 123456, 'orders': [
                {'pending_price': 100.5, 'stop': 99, 'target': 102, 'volume': .1,
                 'result': {'order': 77}},
                {'pending_price': 100.3, 'stop': 99, 'target': 103, 'volume': .2,
                 'result': {'order': 78}},
            ],
        }
        calls = []
        class Supervisor:
            def revalidate_pending_limit(self, *args):
                calls.append(args)
                return {'accepted': True, 'status': 'pending_still_valid',
                        'ticket': args[0]}
            def close(self):
                pass
        decision = {'data_quality': [], 'positions_summary': {'pending_orders': [
            {'ticket': ticket, 'symbol': 'XAUUSD.c', 'magic': 123,
             'comment': 'Tradew S:0123456789'} for ticket in (77, 78)]}}
        with tempfile.TemporaryDirectory() as folder:
            from pathlib import Path
            import json
            Path(folder, 'auto_executions.jsonl').write_text(json.dumps({
                'event': 'ATTEMPT_FINISHED', 'setup_id': 'setup-1',
                'setup_ref': '0123456789', 'entry_plan': plan,
                'execution': execution})+'\n', encoding='utf-8')
            result = supervise_pending_orders(decision, config, folder, Supervisor)
        self.assertEqual([item['ticket'] for item in result], [77, 78])
        self.assertEqual([call[5]['pending_price'] for call in calls], [100.5, 100.3])

    def test_quote_change_during_precheck_prevents_send(self):
        terminal = Terminal()
        terminal.after_check = lambda: setattr(terminal.tick, 'bid', 100.7)
        self.assertEqual(self.submit(terminal)['status'], 'blocked_quote_changed_before_send')
        self.assertFalse(terminal.sent)

    def test_armed_approval_executes_later_only_with_identical_plan(self):
        _, config, plan = execution_case()
        armed = dict(snapshot_id='armed', setup_id='setup-1', setup_ref='0123456789',
                     setup_state='ARMED_LIMIT', symbol='XAUUSD.c', account_mode='demo',
                     action='WAIT', planned_direction='BUY', entry_plan=plan,
                     snapshot_time=time.time(), data_quality=[], bid=101, ask=101.26)
        blind = dict(snapshot_id='armed', analysis=dict(
            status='ok', setup_status='READY', recommended_action='BUY',
            risk_tier='MEDIUM'))
        review = dict(snapshot_id='armed', review=dict(status='ok', verdict='AGREE', recommended_action='BUY'))
        self.assertTrue(automation._independent_can_authorize(armed, blind))
        approval = authorization_result('consensus', armed, review, blind)
        self.assertEqual(approval['status'], 'AUTHORIZED')
        current = {**armed, 'snapshot_id': 'retest', 'action': 'BUY', 'setup_state': 'ACTIONABLE',
                   'bid': 100.1, 'ask': 100.36, 'stop': 99, 'targets': [102],
                   'setup_ref': '0123456789', 'symbol': 'XAUUSD.c', 'account_mode': 'demo'}
        calls = []
        class Sender:
            def place_pending_limit(self, *args):
                calls.append(('pending', args))
                return dict(accepted=True, send_attempted=True, status='pending_submitted')
            def execute_market_order(self, *args):
                calls.append(('market', args))
                return dict(accepted=True, send_attempted=True, status='submitted')
            def close(self):
                pass
        with tempfile.TemporaryDirectory() as folder:
            changed = {**current, 'entry_plan': {**plan, 'stop': 98}}
            self.assertEqual(execute_if_fresh(changed, armed, approval, config, folder, Sender)['status'],
                             'reviewed_plan_changed')
            pending = execute_if_fresh(armed, armed, approval, config, folder, Sender)
            self.assertTrue(pending['accepted'])
            self.assertEqual(pending['status'], 'pending_submitted')
            self.assertEqual(execute_if_fresh(current, armed, approval, config, folder, Sender)['status'],
                             'duplicate_setup_blocked')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'pending')

    def test_approval_expires_and_wait_is_not_approval(self):
        _, _, plan = execution_case()
        armed = dict(snapshot_id='a', setup_state='ARMED_LIMIT', planned_direction='BUY',
                     action='WAIT', entry_plan=plan, snapshot_time=time.time()-400)
        blind = dict(snapshot_id='a', analysis=dict(status='ok', setup_status='WATCH', recommended_action='WAIT'))
        self.assertEqual(authorization_result('independent', armed, independent_result=blind)['status'],
                         'NO_AUTHORIZATION')
        settings = dict(entry_mode='independent', provider='gemini')
        key = 'independent|gemini|' + plan_key(armed)
        with patch.dict(automation._plan_approvals, {key: dict(decision=armed)}, clear=True):
            self.assertIsNone(automation.approved_plan(armed, settings))

    def test_plan_expiry_and_reclaim_share_quote_rules(self):
        _, _, plan = execution_case()
        self.assertEqual(quote_blocker(plan, 100.1, 100.36, plan['expires_at'], .5), 'plan_expired')
        self.assertEqual(quote_blocker(plan, 99.9, 100.16, time.time(), .5), 'level_reclaimed')

    def test_approval_cache_reuses_only_current_plan_and_provider(self):
        _, _, plan = execution_case()
        armed = dict(snapshot_id='a', setup_state='ARMED_LIMIT', planned_direction='BUY',
                     action='WAIT', entry_plan=plan, snapshot_time=time.time())
        current = {**armed, 'snapshot_id': 'b', 'setup_state': 'ACTIONABLE', 'action': 'BUY'}
        settings = dict(entry_mode='consensus', provider='gemini')
        key = 'consensus|gemini|' + plan_key(armed)
        cached = dict(decision=armed, authorization=dict(status='AUTHORIZED'))
        with patch.dict(automation._plan_approvals, {key: cached}, clear=True):
            self.assertEqual(automation.approved_plan(current, settings), cached)
            self.assertIsNone(automation.approved_plan(current, {**settings, 'provider': 'astra'}))
            changed = {**current, 'entry_plan': {**plan, 'targets': [104]}}
            self.assertIsNone(automation.approved_plan(changed, settings))

    def test_safe_presend_rejection_can_retry_but_uncertain_send_cannot(self):
        import json
        from pathlib import Path
        from auto_execution import _already_attempted
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'journal.jsonl'
            started = dict(setup_id='s', event='ATTEMPT_STARTED')
            finished = dict(setup_id='s', event='ATTEMPT_FINISHED',
                            execution=dict(status='blocked_outside_entry_zone', accepted=False))
            path.write_text(json.dumps(started)+'\n'+json.dumps(finished)+'\n')
            self.assertFalse(_already_attempted(path, 's'))
            path.write_text(json.dumps(started)+'\n')
            self.assertTrue(_already_attempted(path, 's'))
            finished['execution']['status'] = 'execution_error'
            path.write_text(json.dumps(started)+'\n'+json.dumps(finished)+'\n')
            self.assertTrue(_already_attempted(path, 's'))
            finished['execution']['error'] = 'MT5 pending order_check returned no result: invalid'
            path.write_text(json.dumps(started)+'\n'+json.dumps(finished)+'\n')
            self.assertFalse(_already_attempted(path, 's'))
            finished['execution'] = {'status': 'order_send_rejected', 'send_attempted': True,
                                     'result': {'retcode': 10022, 'order': 0, 'deal': 0}}
            path.write_text(json.dumps(started)+'\n'+json.dumps(finished)+'\n')
            self.assertFalse(_already_attempted(path, 's'))
            finished['execution'] = {'status': 'blocked_minimum_volume',
                                     'accepted': False}
            path.write_text(json.dumps(started)+'\n'+json.dumps(finished)+'\n')
            self.assertTrue(_already_attempted(path, 's'))

    def test_new_exposure_during_order_check_blocks_send(self):
        terminal = Terminal()
        terminal.after_check = lambda: terminal.positions.append(SimpleNamespace(symbol='XAUEUR'))
        self.assertEqual(self.submit(terminal)['status'], 'blocked_existing_exposure')
        self.assertFalse(terminal.sent)

    def test_armed_watch_does_not_suppress_actionable_review(self):
        _, _, plan = execution_case()
        decision = dict(snapshot_id='a', setup_state='ARMED_LIMIT', action='WAIT',
                        planned_direction='BUY', entry_plan=plan, buy_setup_score=80)
        settings = dict(entry_mode='consensus', provider='gemini')
        with patch.object(automation, 'AUTO_GEMINI', True), \
                patch.object(automation, '_pending_review', None), \
                patch.object(automation, '_active_plan_job', None), \
                patch.dict(automation._reviewed_at, {}, clear=True):
            self.assertTrue(automation.consider_automatic_review(decision, settings))
            armed_key = automation._pending_review['job_key']
            automation._reviewed_at[armed_key] = time.time()
            automation._pending_review = None
            actionable = {**decision, 'snapshot_id': 'b', 'setup_state': 'ACTIONABLE', 'action': 'BUY'}
            self.assertTrue(automation.consider_automatic_review(actionable, settings))
            self.assertNotEqual(automation._pending_review['job_key'], armed_key)
