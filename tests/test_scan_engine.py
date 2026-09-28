import unittest
import math
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from scan_engine import TIMEFRAMES, validate
from scan_engine import (atr, pivots, floor_volume, evaluate, market_session,
                         structure, structural_zones, opposing_targets, context_gate)


def bar(i, high, low=1, close=2):
    return dict(time=i*300, open=2, high=high, low=low, close=close)


class RulesTest(unittest.TestCase):
    def fixture(self):
        from pathlib import Path
        import json
        config = json.loads(Path('tests/fixtures/scan_config.json').read_text())
        config.update(expected_login=1, expected_server='demo', expected_mode='demo', expected_currency='USC',
                      timestamp_offset_seconds=0, max_tick_age_seconds=10, max_collection_seconds=10,
                      max_price_drift=1, max_spread=1, per_trade_cash_budget=100, portfolio_cash_budget=200,
                      max_margin_fraction=.5, cost_per_lot=0, structural_tolerance=.01,
                      buffer_atr_fraction=.1, confirmation_expiry_bars=2,
                      volatility_low_ratio=.75, volatility_high_ratio=1.5)
        now = 14400*100
        bars = {tf: [dict(time=now-(199-i)*seconds, open=100+math.sin(i),
                         high=102+math.sin(i), low=98+math.sin(i), close=100+math.sin(i))
                     for i in range(200)] for tf, seconds in TIMEFRAMES.items()}
        snapshot = dict(snapshot_id='fixture', symbol='XAUUSD.c', retrieval_start_utc=now,
                        retrieval_end_utc=now+1, account=dict(login=1, server='demo', currency='USC',
                        balance=200, equity=200, margin_free=1000),
                        account_mode='demo', terminal=dict(connected=True), positions=[], pending_orders=[],
                        tick=dict(time=now, bid=100, ask=100.1), initial_tick=dict(time=now, bid=100, ask=100.1),
                        bars=bars, specifications={'XAUUSD.c': dict(trade_tick_size=.01, volume_min=.01,
                        volume_step=.01, volume_max=100, point=.01, trade_stops_level=0, trade_freeze_level=0)},
                        gold_ticks={'XAUUSD.c': dict(bid=100, ask=100.1)})
        return snapshot, config

    def test_valid_cent_snapshot(self):
        snapshot, config = self.fixture()
        self.assertEqual(validate(snapshot, config), [])

    def test_future_quote_blocks(self):
        snapshot, config = self.fixture()
        snapshot['tick']['time'] += 10800
        self.assertIn('Quote stale or future dated', validate(snapshot, config))

    def test_account_switch_blocks(self):
        snapshot, config = self.fixture()
        snapshot['account']['login'] = 2
        self.assertIn('Account mismatch: login', validate(snapshot, config))

    def test_forming_bar_does_not_change_structure(self):
        snapshot, config = self.fixture()
        class Broker:
            pass
        first = evaluate(snapshot, config, Broker())
        snapshot['bars']['M5'][-1]['high'] = 10000
        second = evaluate(snapshot, config, Broker())
        self.assertEqual(first['structure'], second['structure'])

    def test_d1_is_available_as_macro_context_and_daily_reference(self):
        snapshot, config = self.fixture()
        class Broker:
            pass
        result = evaluate(snapshot, config, Broker())
        self.assertIn('D1', result['structure'])
        reference = result['chart_analysis']['daily_reference']
        self.assertEqual(reference['close'], snapshot['bars']['D1'][-2]['close'])
        self.assertIn('never an entry trigger', reference['role'])

    def test_d1_conflict_requires_m5_reversal_but_never_authorizes_entry(self):
        contexts = {name: {'trend': 'mixed'} for name in TIMEFRAMES}
        contexts.update(H4={'trend': 'bullish'}, H1={'trend': 'bullish'},
                        M30={'trend': 'bullish'}, D1={'trend': 'bearish'})
        config = {'minimum_higher_timeframe_alignment': 2,
                  'minimum_setup_score': 0}
        failures = context_gate(contexts, 'BUY', {'chart_patterns': []}, 80, config)
        message = 'D1 macro trend opposes the setup without M5 reversal evidence'
        self.assertIn(message, failures)
        reversed_failures = context_gate(
            contexts, 'BUY', {'chart_patterns': ['bullish_rejection']}, 80, config)
        self.assertNotIn(message, reversed_failures)

    def test_pending_orders_prevent_new_entry(self):
        snapshot, config = self.fixture()
        snapshot['pending_orders'] = [dict(ticket=2, symbol='XAUUSD.c', type=2, volume_current=.01, price_open=99, sl=98)]
        class Broker:
            def profit(self, kind, symbol, volume, entry, stop):
                return (stop-entry)*volume
        result = evaluate(snapshot, config, Broker())
        self.assertEqual(result['action'], 'WAIT')
        self.assertNotIn('entry', result)

    def test_pending_exposure_signature_ignores_moving_quote_but_detects_term_change(self):
        from mt5_connector import pending_exposure_signature
        original = SimpleNamespace(ticket=7, symbol='XAUUSD.c', type=2,
            volume_current=.2, price_open=99, sl=97, tp=103, magic=101,
            comment='Tradew S:1234567890', type_time=2, time_expiration=99999,
            price_current=100)
        repriced = SimpleNamespace(**{**vars(original), 'price_current': 101})
        changed_stop = SimpleNamespace(**{**vars(original), 'sl': 96})
        names = {'XAUUSD.c'}
        self.assertEqual(pending_exposure_signature([original], names),
                         pending_exposure_signature([repriced], names))
        self.assertNotEqual(pending_exposure_signature([original], names),
                            pending_exposure_signature([changed_stop], names))

    def test_portfolio_breach_proposes_exact_partial_close(self):
        from position_review import review
        snapshot, config = self.fixture()
        snapshot['account']['balance'] = 400
        snapshot['positions'] = [dict(ticket=11, symbol='XAUUSD.c', type=0, volume=10,
                                     price_open=100, sl=50, tp=150, profit=0)]
        class Broker:
            def profit(self, kind, symbol, volume, entry, stop):
                return (stop-entry)*volume*(1 if kind == 0 else -1)
        contexts = {'M5': {'pivots': {'low': [], 'high': []}}}
        result = review(snapshot, config, Broker(), contexts, 1)
        change = result['changes'][0]
        self.assertEqual(change['action'], 'PARTIAL CLOSE')
        self.assertEqual((change['ticket'], change['volume'], change['remaining_volume']),
                         (11, 7.6, 2.4000000000000004))
        self.assertAlmostEqual(change['remaining_risk'], 120)
        self.assertEqual(snapshot['positions'][0]['sl'], 50)

    def test_management_preserves_full_original_position_record(self):
        snapshot, config = self.fixture()
        original = dict(ticket=21, symbol='XAUUSD.c', type=0, volume=.5,
                        price_open=99, sl=98, tp=110, profit=12.5, comment='manual')
        snapshot['positions'] = [original.copy()]
        class Broker:
            def profit(self, kind, symbol, volume, entry, stop):
                return (stop-entry)*volume*(1 if kind == 0 else -1)
        result = evaluate(snapshot, config, Broker())
        displayed = result['positions_summary']['positions'][0]
        self.assertEqual(displayed, original)
        self.assertEqual(displayed['type'], 0)
        self.assertIn('risk_by_ticket', result['positions_summary'])

    def test_failed_broker_risk_is_unknown_not_zero(self):
        from position_review import review
        snapshot, config = self.fixture()
        snapshot['positions'] = [dict(ticket=22, symbol='XAUUSD.c', type=0, volume=.1,
                                     price_open=100, sl=95, tp=110, profit=0)]
        class Broker:
            def profit(self, *args):
                return None
        contexts = {'M5': {'pivots': {'low': [], 'high': []}}}
        result = review(snapshot, config, Broker(), contexts, 1)
        self.assertIsNone(result['actual_portfolio_cash_risk'])
        self.assertIsNone(result['risk_by_ticket'][0]['cash_risk'])
        self.assertIn('Aggregate risk is unknown; no zero-risk assumption is permitted', result['alerts'])

    def test_original_thesis_invalidation_proposes_close(self):
        from position_review import review
        snapshot, config = self.fixture()
        snapshot['gold_ticks']['XAUUSD.c'] = {'bid': 98, 'ask': 98.1}
        snapshot['positions'] = [dict(ticket=23, symbol='XAUUSD.c', type=0, volume=.1,
            price_open=100, sl=95, tp=110, profit=-1,
            original_setup={'setup_ref': 'abc', 'symbol': 'XAUUSD.c', 'direction': 'BUY',
                            'account_login': 1, 'account_server': 'demo',
                            'account_mode': 'demo', 'account_currency': 'USC',
                            'original_entry': 100, 'original_invalidation': 99})]
        class Broker:
            def profit(self, kind, symbol, volume, entry, stop):
                return (stop-entry)*volume*(1 if kind == 0 else -1)
        contexts = {'M5': {'pivots': {'low': [], 'high': []}}}
        result = review(snapshot, config, Broker(), contexts, .1)
        self.assertEqual(result['changes'][0]['action'], 'CLOSE')
        self.assertIn('Original BUY thesis invalidated', result['changes'][0]['reason'])

    def test_one_r_moves_stop_to_breakeven_without_waiting_for_swing(self):
        from position_review import review
        snapshot, config = self.fixture()
        snapshot['gold_ticks']['XAUUSD.c'] = {'bid': 106, 'ask': 106.1}
        snapshot['positions'] = [dict(ticket=24, symbol='XAUUSD.c', type=0, volume=.1,
            price_open=100, sl=95, tp=115, profit=6,
            original_setup={'setup_ref': 'def', 'symbol': 'XAUUSD.c', 'direction': 'BUY',
                            'account_login': 1, 'account_server': 'demo',
                            'account_mode': 'demo', 'account_currency': 'USC',
                            'original_entry': 100, 'original_invalidation': 95})]
        class Broker:
            def profit(self, kind, symbol, volume, entry, stop):
                return (stop-entry)*volume*(1 if kind == 0 else -1)
        contexts = {'M5': {'pivots': {'low': [], 'high': []}}}
        result = review(snapshot, config, Broker(), contexts, .1)
        self.assertEqual(result['changes'][0]['action'], 'MOVE SL TO BE')
        self.assertEqual(result['changes'][0]['proposed_stop'], 100)

    def test_half_r_reduces_initial_price_risk_by_half(self):
        from position_review import review
        snapshot, config = self.fixture()
        snapshot['gold_ticks']['XAUUSD.c'] = {'bid': 102.5, 'ask': 102.6}
        snapshot['positions'] = [dict(ticket=25, symbol='XAUUSD.c', type=0, volume=.1,
            price_open=100, sl=95, tp=115, profit=2.5,
            original_setup={'setup_ref': 'half-r', 'symbol': 'XAUUSD.c', 'direction': 'BUY',
                            'account_login': 1, 'account_server': 'demo',
                            'account_mode': 'demo', 'account_currency': 'USC',
                            'original_entry': 100, 'original_invalidation': 95})]
        class Broker:
            def profit(self, kind, symbol, volume, entry, stop):
                return (stop-entry)*volume*(1 if kind == 0 else -1)
        contexts = {'M5': {'pivots': {'low': [], 'high': []}}}
        result = review(snapshot, config, Broker(), contexts, .1)
        change = result['changes'][0]
        self.assertEqual(change['action'], 'REDUCE RISK')
        self.assertEqual(change['proposed_stop'], 97.5)
        self.assertEqual(change['management_stage'], 'half_risk')

    def test_half_r_reduction_is_symmetric_for_sell(self):
        from position_review import review
        snapshot, config = self.fixture()
        snapshot['gold_ticks']['XAUUSD.c'] = {'bid': 97.4, 'ask': 97.5}
        snapshot['positions'] = [dict(ticket=26, symbol='XAUUSD.c', type=1, volume=.1,
            price_open=100, sl=105, tp=85, profit=2.5,
            original_setup={'setup_ref': 'half-r-sell', 'symbol': 'XAUUSD.c', 'direction': 'SELL',
                            'account_login': 1, 'account_server': 'demo',
                            'account_mode': 'demo', 'account_currency': 'USC',
                            'original_entry': 100, 'original_invalidation': 105})]
        class Broker:
            def profit(self, kind, symbol, volume, entry, stop):
                return (stop-entry)*volume*(1 if kind == 0 else -1)
        contexts = {'M5': {'pivots': {'low': [], 'high': []}}}
        result = review(snapshot, config, Broker(), contexts, .1)
        self.assertEqual(result['changes'][0]['action'], 'REDUCE RISK')
        self.assertEqual(result['changes'][0]['proposed_stop'], 102.5)

    def test_evaluator_surfaces_half_r_management_action(self):
        snapshot, config = self.fixture()
        snapshot['tick'] = {'time': snapshot['tick']['time'], 'bid': 102.5, 'ask': 102.6}
        snapshot['initial_tick'] = dict(snapshot['tick'])
        snapshot['gold_ticks']['XAUUSD.c'] = {'bid': 102.5, 'ask': 102.6}
        snapshot['positions'] = [dict(ticket=27, symbol='XAUUSD.c', type=0, volume=.1,
            price_open=100, sl=95, tp=115, profit=2.5,
            original_setup={'setup_ref': 'half-r-evaluate', 'symbol': 'XAUUSD.c',
                            'direction': 'BUY', 'account_login': 1,
                            'account_server': 'demo', 'account_mode': 'demo',
                            'account_currency': 'USC', 'original_entry': 100,
                            'original_invalidation': 95})]
        class Broker:
            def profit(self, kind, symbol, volume, entry, stop):
                return (stop-entry)*volume*(1 if kind == 0 else -1)
        result = evaluate(snapshot, config, Broker())
        self.assertEqual(result['action'], 'REDUCE RISK')
        self.assertEqual(result['proposed_management_changes'][0]['proposed_stop'], 97.5)

    def test_non_usd_gold_is_included_in_exposure_universe(self):
        from mt5_connector import is_gold_symbol, is_gold_usd_symbol
        xaueur = SimpleNamespace(name='XAUEUR.a', currency_base='XAU',
                                 currency_profit='EUR', description='Gold vs Euro')
        self.assertTrue(is_gold_symbol(xaueur))
        self.assertFalse(is_gold_usd_symbol(xaueur))

    def test_session_and_volatility_regime_are_explicit(self):
        self.assertEqual(market_session(8*3600), 'London')
        bars = [dict(time=i*300, open=100, high=101, low=99, close=100) for i in range(60)]
        for i, item in enumerate(bars[-15:]):
            item.update(high=104+i*.01, low=96-i*.01)
        context = structure(bars, .01, {'volatility_low_ratio': .75,
                                        'volatility_high_ratio': 1.5})
        self.assertEqual(context['volatility_regime'], 'high')

    def test_setup_tracker_reconciles_closed_position_outcome(self):
        from setup_tracker import SetupTracker
        with tempfile.TemporaryDirectory() as folder:
            tracker = SetupTracker(folder)
            result = {'setup_id': 'XAUUSD.c:breakout:BUY:1:100', 'symbol': 'XAUUSD.c',
                      'action': 'BUY', 'entry': 100, 'invalidation': 95,
                      'targets': [110], 'snapshot_time': 1, 'setup_state': 'ACTIONABLE'}
            identity = {'account': {'login': 1, 'server': 'demo', 'currency': 'USC'},
                        'account_mode': 'demo'}
            tracker.record(result, identity)
            tracker.bind(77, result['setup_ref'], 2)
            open_snapshot = {**identity, 'retrieval_end_utc': 2,
                             'positions': [{'ticket': 77, 'symbol': 'XAUUSD.c',
                                            'type': 0, 'comment': ''}],
                             'recent_deals': []}
            tracker.attach(open_snapshot)
            snapshot = {**identity, 'positions': [],
                        'recent_deals': [{'ticket': 88, 'position_id': 77,
                            'entry_role': 'OUT', 'profit': 12, 'commission': -1,
                            'swap': 0, 'fee': 0, 'time': 3}]}
            tracker.attach(snapshot)
            state = tracker.state['setups'][result['setup_ref']]
            self.assertEqual(state['state'], 'CLOSED')
            self.assertEqual(state['outcome']['net_result'], 11)

    def test_native_pending_fill_records_actual_entry_for_management(self):
        from setup_tracker import SetupTracker
        with tempfile.TemporaryDirectory() as folder:
            tracker = SetupTracker(folder)
            identity = {'account': {'login': 1, 'server': 'demo', 'currency': 'USC'},
                        'account_mode': 'demo'}
            result = {
                'setup_id': 'XAUUSD.c:retest:BUY:1:100', 'symbol': 'XAUUSD.c',
                'action': 'WAIT', 'planned_direction': 'BUY', 'invalidation': 95,
                'entry_plan': {'targets': [110]}, 'snapshot_time': 1,
                'setup_state': 'ARMED_LIMIT',
            }
            tracker.record(result, identity)
            snapshot = {**identity, 'retrieval_end_utc': 2, 'recent_deals': [],
                        'positions': [{'ticket': 91, 'symbol': 'XAUUSD.c', 'type': 0,
                                       'price_open': 100.25, 'tp': 110,
                                       'comment': 'Tradew S:'+result['setup_ref']}]}
            tracker.attach(snapshot)
            thesis = snapshot['positions'][0]['original_setup']
            self.assertEqual(thesis['original_entry'], 100.25)
            self.assertEqual(thesis['original_targets'], [110])

    def test_unprotected_positions_still_receive_aggregate_reduction(self):
        from position_review import review
        snapshot, config = self.fixture()
        config.update(per_trade_cash_budget=100, portfolio_cash_budget=150)
        snapshot['account']['balance'] = 300
        snapshot['positions'] = [
            dict(ticket=ticket, symbol='XAUUSD.c', type=0, volume=1,
                 price_open=100, sl=0, tp=120, profit=0)
            for ticket in (31, 32)]
        class Broker:
            def profit(self, kind, symbol, volume, entry, stop):
                return (stop-entry)*9*volume*(1 if kind == 0 else -1)
        contexts = {'M5': {'pivots': {'low': [{'price': 91}], 'high': []}}}
        result = review(snapshot, config, Broker(), contexts, 1)
        reductions = [change for change in result['changes']
                      if change['action'] in ('PARTIAL CLOSE', 'CLOSE')]
        self.assertIsNone(result['actual_portfolio_cash_risk'])
        self.assertLessEqual(result['planned_cash_risk'], 150)
        self.assertEqual(len(reductions), 1)
        self.assertEqual(reductions[0]['action'], 'CLOSE')

    def test_aggregate_reduction_preserves_trailing_stop_assumption(self):
        from position_review import review
        snapshot, config = self.fixture()
        config.update(per_trade_cash_budget=100, portfolio_cash_budget=50)
        snapshot['account']['balance'] = 100
        snapshot['positions'] = [dict(ticket=33, symbol='XAUUSD.c', type=0, volume=1,
                                     price_open=100, sl=80, tp=120, profit=0)]
        class Broker:
            def profit(self, kind, symbol, volume, entry, stop):
                return (stop-entry)*9*volume*(1 if kind == 0 else -1)
        contexts = {'M5': {'pivots': {'low': [{'price': 91}], 'high': []}}}
        result = review(snapshot, config, Broker(), contexts, 1)
        change = result['changes'][0]
        self.assertEqual(change['action'], 'PARTIAL CLOSE')
        self.assertEqual((change['volume'], change['remaining_volume']),
                         (.67, .32999999999999996))
        self.assertAlmostEqual(change['remaining_risk'], 29.7)
        self.assertEqual(change['proposed_stop_after_reduction'], 90)
        self.assertEqual(change['required_stop_action'], 'TRAIL SL')

    def test_mismatched_setup_binding_is_not_attached_or_used(self):
        from setup_tracker import SetupTracker
        with tempfile.TemporaryDirectory() as folder:
            tracker = SetupTracker(folder)
            identity = {'account': {'login': 1, 'server': 'demo', 'currency': 'USC'},
                        'account_mode': 'demo'}
            setup = {'setup_id': 'XAUUSD.c:breakout:BUY:1:100', 'symbol': 'XAUUSD.c',
                     'action': 'BUY', 'entry': 100, 'invalidation': 99,
                     'targets': [110], 'snapshot_time': 1, 'setup_state': 'ACTIONABLE'}
            tracker.record(setup, identity)
            tracker.bind(44, setup['setup_ref'])
            self.assertNotIn('44', tracker.state['bindings'])
            snapshot = {**identity, 'positions': [
                {'ticket': 44, 'symbol': 'XAUUSD.c', 'type': 1, 'comment': ''}],
                'recent_deals': [], 'retrieval_end_utc': 2}
            tracker.attach(snapshot)
            position = snapshot['positions'][0]
            self.assertNotIn('original_setup', position)
            self.assertIn('direction mismatch', position['setup_binding_error'])
            self.assertNotIn(44, tracker.state['setups'][setup['setup_ref']]['linked_tickets'])

    def test_binding_validates_account_symbol_and_direction(self):
        from setup_tracker import SetupTracker
        thesis = {'symbol': 'XAUUSD.c', 'direction': 'BUY', 'account_login': 1,
                  'account_server': 'demo', 'account_mode': 'demo',
                  'account_currency': 'USC'}
        snapshot = {'account': {'login': 1, 'server': 'demo', 'currency': 'USC'},
                    'account_mode': 'demo'}
        position = {'ticket': 1, 'symbol': 'XAUUSD.c', 'type': 0}
        self.assertIsNone(SetupTracker.binding_error(thesis, position, snapshot))
        self.assertIn('symbol mismatch', SetupTracker.binding_error(
            thesis, {**position, 'symbol': 'XAUEUR.c'}, snapshot))
        self.assertIn('direction mismatch', SetupTracker.binding_error(
            thesis, {**position, 'type': 1}, snapshot))
        self.assertIn('account login mismatch', SetupTracker.binding_error(
            thesis, position, {**snapshot, 'account': {**snapshot['account'], 'login': 2}}))

    def test_setup_tracker_never_overwrites_original_thesis(self):
        from setup_tracker import SetupTracker
        with tempfile.TemporaryDirectory() as folder:
            tracker = SetupTracker(folder)
            first = {'setup_id': 'XAUUSD.c:breakout:BUY:1:100', 'symbol': 'XAUUSD.c',
                     'action': 'BUY', 'entry': 100, 'invalidation': 95,
                     'targets': [110], 'snapshot_time': 1, 'setup_state': 'ACTIONABLE'}
            tracker.record(first, {})
            later = {'setup_id': first['setup_id'], 'symbol': 'XAUUSD.c', 'action': 'WAIT',
                     'invalidation': 96, 'snapshot_time': 2, 'setup_state': 'EXPIRED'}
            tracker.record(later, {})
            stored = tracker.state['setups'][first['setup_ref']]
            self.assertEqual(stored['original_entry'], 100)
            self.assertEqual(stored['original_invalidation'], 95)
            self.assertEqual(stored['original_targets'], [110])

    def test_replay_uses_next_bar_costs_and_stop_first(self):
        from replay_engine import replay_m5
        bars = [dict(time=i*300, open=100, high=101, low=99, close=100, spread=10)
                for i in range(90)]
        bars[61].update(high=106, low=97)
        snapshot, config = self.fixture()
        config.update(replay_spread_fallback=.1, replay_slippage_price=.05,
                      replay_horizon_bars=10, cost_per_lot=2)
        spec = snapshot['specifications']['XAUUSD.c']
        setup = {'direction': 'BUY', 'family': 'breakout_follow_through',
                 'level': 100, 'invalidation': 98, 'confirmation_time': bars[60]['time'],
                 'confirmation_close': 100, 'state': 'CONFIRMED'}
        def detected(known, *_):
            return [setup] if known[-1]['time'] == bars[60]['time'] else []
        class Broker:
            def profit(self, kind, symbol, volume, entry, exit):
                return (exit-entry)*100*volume*(1 if kind == 0 else -1)
        known_levels = {'high': [{'price': 105}], 'low': [{'price': 95}]}
        with patch('setups.detect', side_effect=detected), patch(
                'replay_engine.pivots', return_value=known_levels):
            report = replay_m5(bars, config, Broker(), 'XAUUSD.c', spec)
        trade = report['trades'][0]
        self.assertEqual(trade['entry_time'], bars[61]['time'])
        self.assertEqual(trade['outcome'], 'STOP')
        self.assertGreater(trade['estimated_cost'], 0)
        self.assertIn('stop first', report['fill_policy'])

    def test_replay_stop_gap_fills_at_worse_open(self):
        from replay_engine import replay_m5
        bars = [dict(time=i*300, open=100, high=101, low=99, close=100, spread=10)
                for i in range(90)]
        bars[62].update(open=95, high=96, low=94, close=95)
        snapshot, config = self.fixture()
        config.update(replay_spread_fallback=.1, replay_slippage_price=.05,
                      replay_horizon_bars=10, cost_per_lot=0)
        setup = {'direction': 'BUY', 'family': 'breakout_follow_through',
                 'level': 100, 'invalidation': 98, 'confirmation_time': bars[60]['time'],
                 'confirmation_close': 100, 'state': 'CONFIRMED'}
        def detected(known, *_):
            return [setup] if known[-1]['time'] == bars[60]['time'] else []
        class Broker:
            def profit(self, kind, symbol, volume, entry, exit):
                return (exit-entry)*100*volume*(1 if kind == 0 else -1)
        levels = {'high': [{'price': 105}], 'low': [{'price': 95}]}
        with patch('setups.detect', side_effect=detected), patch(
                'replay_engine.pivots', return_value=levels):
            report = replay_m5(bars, config, Broker(), 'XAUUSD.c',
                               snapshot['specifications']['XAUUSD.c'])
        trade = report['trades'][0]
        self.assertTrue(trade['gap_through_stop'])
        self.assertEqual(trade['exit'], 94.95)
        self.assertLess(trade['exit'], trade['stop'])

    def test_rule_version_identifies_native_pending_logic(self):
        from scan_engine import RULE_VERSION
        self.assertEqual(RULE_VERSION, 'structure-research-30')

    def test_terminal_setup_cannot_resurrect(self):
        from setup_tracker import SetupTracker
        with tempfile.TemporaryDirectory() as folder:
            tracker = SetupTracker(folder)
            snapshot = {'account': {'login': 1, 'server': 'demo', 'currency': 'USC'},
                        'account_mode': 'demo'}
            setup = {'setup_id': 'XAUUSD.c:retest:BUY:1:100',
                     'symbol': 'XAUUSD.c', 'snapshot_time': 1,
                     'setup_state': 'INVALIDATED', 'action': 'WAIT'}
            tracker.record(setup, snapshot)
            revived = {**setup, 'snapshot_time': 2, 'setup_state': 'ACTIONABLE',
                       'action': 'BUY', 'entry': 101, 'volume': .1}
            tracker.enforce_terminal_state(revived)
            tracker.record(revived, snapshot)
            self.assertEqual(revived['setup_state'], 'INVALIDATED')
            self.assertEqual(revived['action'], 'WAIT')
            self.assertNotIn('entry', revived)
            self.assertEqual(tracker.state['setups'][setup['setup_ref']]['state'],
                             'INVALIDATED')

    def test_llm_context_is_bounded_and_redacted(self):
        from llm_advisors import review_context
        decision = {'snapshot_id': 's1', 'action': 'WAIT', 'chart_data': {'M5': [1, 2]},
                    'account_summary': {'login': 123, 'equity': 10},
                    'positions_summary': {'positions': [
                        {'ticket': 1, 'symbol': 'XAUUSD.c', 'type': 0, 'volume': .1,
                         'comment': 'private broker comment'}], 'pending_orders': []}}
        context = review_context(decision)
        self.assertNotIn('chart_data', context)
        self.assertNotIn('account_summary', context)
        self.assertNotIn('comment', context['positions'][0])

    def test_codex_cli_review_is_read_only_and_schema_validated(self):
        from llm_advisors import review_with_codex
        payload = {'verdict': 'AGREE', 'recommended_action': 'WAIT',
                   'summary': 'The wait state matches the supplied flags.',
                   'risk_flags': [], 'conditions_to_proceed': ['Fresh M5 close']}
        calls = []
        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr='')
        import json
        result = review_with_codex({'snapshot_id': 's1', 'action': 'WAIT'},
            {'model': 'gpt-6-astra', 'reasoning_effort': 'low',
             'service_tier': 'fast', 'command': 'codex', 'timeout_seconds': 10},
            runner=runner)
        self.assertEqual(result['status'], 'ok')
        self.assertIn('read-only', calls[0][0])
        self.assertIn('gpt-6-astra', calls[0][0])
        self.assertIn('model_reasoning_effort="low"', calls[0][0])
        self.assertIn('features.fast_mode=true', calls[0][0])
        self.assertIn('service_tier="fast"', calls[0][0])
        self.assertEqual(result['reasoning_effort'], 'low')
        self.assertEqual(result['requested_service_tier'], 'fast')
        self.assertNotIn('dangerously-bypass-approvals-and-sandbox', calls[0][0])

    def test_codex_default_tier_disables_fast_mode(self):
        from llm_advisors import review_with_codex
        import json
        payload = {'verdict': 'AGREE', 'recommended_action': 'WAIT',
                   'summary': 'Wait remains appropriate.', 'risk_flags': [],
                   'conditions_to_proceed': ['Fresh M5 close']}
        calls = []
        def runner(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr='')
        result = review_with_codex({'snapshot_id': 's1', 'action': 'WAIT'},
            {'model': 'gpt-6-astra', 'reasoning_effort': 'high',
             'service_tier': 'default', 'command': 'codex', 'timeout_seconds': 10},
            runner=runner)
        self.assertEqual(result['status'], 'ok')
        self.assertIn('features.fast_mode=false', calls[0])
        self.assertIn('service_tier="default"', calls[0])

    def test_gemini_web_worker_extracts_fenced_json(self):
        from vps_gemini_reviewer import _complete_json, _extract_json
        result = _extract_json('Answer:\n```json\n{"verdict":"AGREE"}\n```')
        self.assertEqual(result, {'verdict': 'AGREE'})
        self.assertIsNone(_complete_json('{"verdict":"AG'))
        self.assertEqual(_complete_json('{"verdict":"AGREE"}'), {'verdict': 'AGREE'})

    def test_gemini_web_worker_retries_only_generic_service_failure(self):
        from vps_gemini_reviewer import _review, _transient_gemini_failure
        expected = {'status': 'ok'}
        failure = RuntimeError(
            'Gemini completed a non-JSON reply: Sorry, something went wrong. '
            'Please try your request again.')
        with patch('vps_gemini_reviewer._review_once',
                   side_effect=[failure, expected]), \
                patch('vps_gemini_reviewer._discard_automation_page') as discard, \
                patch('vps_gemini_reviewer.time.sleep'):
            self.assertEqual(_review({'prompt': 'bounded'}), expected)
        discard.assert_called_once_with()
        self.assertFalse(_transient_gemini_failure(
            RuntimeError('Gemini completed a non-JSON reply: WAIT for confirmation')))

    def test_gemini_vps_review_uses_only_bounded_ssh_request(self):
        from llm_advisors import _review_with_gemini_ssh
        import json
        payload = {'verdict': 'AGREE', 'recommended_action': 'WAIT',
                   'summary': 'The wait state matches the supplied flags.',
                   'risk_flags': [], 'conditions_to_proceed': ['Fresh scan']}
        calls = []
        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr='')
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'llm_review_schema.json').write_text(
                Path('llm_review_schema.json').read_text(encoding='utf-8'), encoding='utf-8')
            result = _review_with_gemini_ssh(
                {'snapshot_id': 's1', 'action': 'WAIT'},
                {'model': 'gemini-web-session', 'timeout_seconds': 10,
                 'ssh_host': 'deploy@example.test', 'remote_python': '/safe/python',
                 'remote_script': '/safe/reviewer.py'}, folder, runner)
        request = json.loads(calls[0][1]['input'])
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['provider'], 'gemini_vps')
        self.assertEqual(calls[0][0][-3:],
                         ['deploy@example.test', '/safe/python', '/safe/reviewer.py'])
        self.assertIn('DECISION_CONTEXT', request['prompt'])
        self.assertIn('REQUIRED_JSON_SCHEMA', request['prompt'])
        self.assertNotIn('api_key', calls[0][1]['input'].lower())

    def test_llm_assessment_never_has_execution_authority(self):
        from llm_advisors import _assessment
        review = {'status': 'ok', 'verdict': 'AGREE', 'recommended_action': 'WAIT'}
        assessment = _assessment(review, 'WAIT')
        self.assertEqual(assessment['status'], 'AGREEMENT')
        self.assertFalse(assessment['execution_authority'])

    def test_only_selected_llm_provider_is_called(self):
        from llm_advisors import review_decision
        import json
        response = {'provider': 'gemini_vps', 'model': 'gemini-web-session',
                    'status': 'ok', 'verdict': 'AGREE', 'recommended_action': 'WAIT',
                    'summary': 'Wait is consistent.', 'risk_flags': [],
                    'conditions_to_proceed': []}
        with tempfile.TemporaryDirectory() as folder:
            config = json.loads(Path('tests/fixtures/llm_config.json').read_text(encoding='utf-8'))
            Path(folder, 'llm_config.json').write_text(json.dumps(config), encoding='utf-8')
            with patch('llm_advisors.review_with_gemini', return_value=response) as gemini, \
                    patch('llm_advisors.review_with_codex') as astra:
                result = review_decision({'snapshot_id': 's1', 'action': 'WAIT'},
                                         'gemini', folder)
            gemini.assert_called_once()
            astra.assert_not_called()
            self.assertEqual(result['selected_provider'], 'gemini')

    def test_ai_review_api_attaches_only_to_matching_snapshot(self):
        import main
        from api_server import app
        main.LAST = {'snapshot_id': 's1', 'action': 'WAIT'}
        review = {'snapshot_id': 's1', 'selected_provider': 'gemini',
                  'review': {'status': 'unavailable'},
                  'assessment': {'status': 'UNAVAILABLE', 'execution_authority': False}}
        with patch('llm_advisors.review_decision', return_value=review) as reviewer:
            response = app.test_client().post('/api/ai/review', json={'provider': 'gemini'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(main.LAST['llm_review'], review)
        self.assertEqual(reviewer.call_args.args[1], 'gemini')

    def test_prospective_report_never_labels_rate_as_probability(self):
        from setup_tracker import SetupTracker
        from validation_report import prospective_report
        with tempfile.TemporaryDirectory() as folder:
            tracker = SetupTracker(folder)
            tracker.state['setups']['a'] = {
                'state': 'CLOSED', 'family': 'breakout', 'volatility_regime': 'normal',
                'outcome': {'net_result': 5}}
            tracker._save()
            report = prospective_report(folder, 100)
            self.assertEqual(report['status'], 'RESEARCH_ONLY')
            self.assertEqual(report['sample_size'], 1)
            self.assertNotIn('win_probability', report)

    def test_no_sender_in_adapter(self):
        from mt5_connector import MT5Adapter
        self.assertFalse(hasattr(MT5Adapter, 'order_send'))
        self.assertFalse(hasattr(MT5Adapter, 'execute_trade'))

    def test_structural_stop_never_widens(self):
        from position_review import review
        snapshot, config = self.fixture()
        snapshot['positions'] = [dict(ticket=12, symbol='XAUUSD.c', type=0, volume=.01,
                                     price_open=90, sl=99, tp=120, profit=.1)]
        class Broker:
            def profit(self, kind, symbol, volume, entry, stop):
                return (stop-entry)*volume
        contexts = {'M5': {'pivots': {'low': [{'price': 95}], 'high': []}}}
        result = review(snapshot, config, Broker(), contexts, 1)
        self.assertEqual(result['changes'], [])

    def test_expired_setup_cannot_be_renewed_by_old_breakout(self):
        from setups import detect
        bars = [bar(i, 10, low=1, close=5) for i in range(40)]
        for b in bars[-10:]:
            b.update(open=12, high=13, low=11, close=12)
        found = detect(bars, .1, .01, 2)
        self.assertFalse(any(s['family'] == 'breakout_follow_through' and s['state'] == 'CONFIRMED' for s in found))

    def test_candidate_waits_for_retest_before_broker_sizing(self):
        snapshot, config = self.fixture()
        context = dict(trend='bullish', atr14_simple=1,
                       pivots={'high': [{'price': 110}], 'low': [{'price': 90}]})
        setup = dict(direction='BUY', family='breakout_follow_through', level=99.95,
                     invalidation=98, confirmation_time=snapshot['bars']['M5'][-2]['time'], confirmation_close=100.1,
                     state='CONFIRMED')
        class Broker:
            def profit(self, kind, symbol, volume, entry, exit):
                return (exit-entry)*100*volume*(1 if kind == 0 else -1)
            def margin(self, kind, symbol, volume, entry):
                return 100*volume
        active_supply = {'kind': 'SUPPLY', 'price': 110, 'low': 109,
                         'high': 111, 'status': 'active', 'active': True,
                         'strength': 3, 'sources': ['H1'], 'active_sources': ['H1'],
                         'broken_sources': [], 'unknown_sources': []}
        with patch('scan_engine.structure', return_value=context), \
                patch('scan_engine.structural_zones', return_value=[active_supply]), \
                patch('setups.detect', return_value=[setup]):
            result = evaluate(snapshot, config, Broker())
            self.assertEqual(result['action'], 'WAIT')
            self.assertEqual(result['setup_state'], 'WAIT_RETEST')
            self.assertEqual(result['risk_currency'], 'USC')
            config['balance_risk_fraction'] = .000005
            result = evaluate(snapshot, config, Broker())
            self.assertEqual(result['action'], 'WAIT')
            self.assertNotIn('volume', result)

    def test_confirmation_requires_two_later_bars(self):
        bars = [bar(i, h) for i, h in enumerate([3, 4, 9, 5, 4])]
        self.assertEqual(pivots(bars[:4])['high'], [])
        self.assertEqual(pivots(bars)['high'][0]['confirmed_bar'], 1200)

    def test_equal_high_tie_is_rightmost(self):
        bars = [bar(i, h) for i, h in enumerate([3, 4, 9, 9, 4, 3])]
        self.assertEqual([p['time'] for p in pivots(bars)['high']], [900])

    def test_atr_is_simple_mean(self):
        bars = [bar(i, 3) for i in range(15)]
        bars[-1]['high'] = 17
        self.assertEqual(atr(bars), 3)

    def test_naked_chart_patterns_are_objective(self):
        from setups import candle_patterns, order_block
        candles = [
            dict(time=1, open=10, high=10.2, low=8.8, close=9),
            dict(time=2, open=8.9, high=10.6, low=8.7, close=10.4),
            dict(time=3, open=10.1, high=11.4, low=10, close=11.2),
        ]
        patterns = candle_patterns(candles[:2], 'BUY', .5)
        self.assertIn('bullish_engulfing', patterns)
        self.assertIn('two_soldiers', candle_patterns(candles, 'BUY', .5))
        block = order_block(candles, 'BUY', 8)
        self.assertEqual(block['time'], 1)
        self.assertEqual((block['body_low'], block['body_high']), (9, 10))

    def test_confirmed_pivots_cluster_into_supply_demand_zones(self):
        contexts = {
            timeframe: {'pivots': {
                'high': [{'price': 110 + offset, 'time': index}],
                'low': [{'price': 90 + offset, 'time': index}],
            }}
            for index, (timeframe, offset) in enumerate(
                [('M5', 0), ('M15', .05), ('H1', -.04)])
        }
        zones = structural_zones(contexts, .2, 2)
        supply = next(zone for zone in zones if zone['kind'] == 'SUPPLY')
        demand = next(zone for zone in zones if zone['kind'] == 'DEMAND')
        self.assertGreaterEqual(supply['strength'], 6)
        self.assertLess(demand['low'], demand['high'])
        self.assertEqual(supply['status'], 'unknown')
        self.assertFalse(supply['active'])
        self.assertEqual(opposing_targets(zones, 'BUY', 100, 0), [])

    def test_d1_zones_have_highest_structural_weight(self):
        contexts = {'D1': {'pivots': {
            'high': [{'price': 110, 'time': 0, 'confirmed_bar': 172800}],
            'low': []}}}
        bars = {'D1': [
            {'time': 0, 'open': 100, 'high': 111, 'low': 99, 'close': 105},
            {'time': 86400, 'open': 105, 'high': 109, 'low': 101, 'close': 106},
            {'time': 172800, 'open': 106, 'high': 109, 'low': 102, 'close': 107},
        ]}
        zone = structural_zones(contexts, .2, 2, bars, 259200, 0)[0]
        self.assertEqual(zone['strength'], 5)
        self.assertEqual(zone['active_sources'], ['D1'])

    def test_zone_activity_uses_source_timeframe_completed_closes(self):
        contexts = {'M5': {'pivots': {
            'high': [{'price': 110, 'time': 0, 'confirmed_bar': 300}],
            'low': [],
        }}}
        m5 = [
            {'time': 0, 'open': 109, 'high': 109.5, 'low': 108.5, 'close': 109},
            {'time': 300, 'open': 109, 'high': 109.5, 'low': 108.5, 'close': 109},
            {'time': 600, 'open': 109, 'high': 109.5, 'low': 108.5, 'close': 109},
        ]
        fast_break = [{'time': 600, 'open': 111, 'high': 112,
                       'low': 110.5, 'close': 111}]
        active = structural_zones(
            contexts, .2, 1, {'M5': m5, 'M1': fast_break}, 900, 0)[0]
        self.assertEqual(active['status'], 'active')
        self.assertEqual(active['active_sources'], ['M5'])

        broken_m5 = [*m5[:-1], {'time': 600, 'open': 111, 'high': 112,
                                'low': 110.5, 'close': 111}]
        broken = structural_zones(
            contexts, .2, 1, {'M5': broken_m5}, 900, 0)[0]
        self.assertEqual(broken['status'], 'broken')
        self.assertEqual(broken['broken_sources'], ['M5'])
        self.assertEqual(broken['invalidation_time'], 900)

    def test_only_active_opposing_zones_are_targets(self):
        zones = [
            {'kind': 'SUPPLY', 'price': 110, 'status': 'active'},
            {'kind': 'SUPPLY', 'price': 112, 'status': 'broken'},
            {'kind': 'SUPPLY', 'price': 114, 'status': 'unknown'},
        ]
        self.assertEqual(opposing_targets(zones, 'BUY', 108, 0), [110])
        self.assertEqual(opposing_targets(zones, 'BUY', 108, 0, 111), [])

    def test_volume_never_rounds_up(self):
        self.assertEqual(floor_volume(0.019999, .01), .01)
        self.assertEqual(floor_volume(.009, .01), 0)
        self.assertEqual(floor_volume(.3, .1), .3)

    def test_null_exposure_blocks_and_omits_entry(self):
        s = {'snapshot_id': 'test', 'retrieval_start_utc': 0, 'retrieval_end_utc': 1,
             'positions': None, 'pending_orders': None, 'errors': ['Disconnected']}
        result = evaluate(s, {}, None)
        self.assertEqual(result['action'], 'WAIT')
        self.assertNotIn('entry', result)
        self.assertEqual(result['execution_status'], 'advisory_only')


if __name__ == '__main__':
    unittest.main()
