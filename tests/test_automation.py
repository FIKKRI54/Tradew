import unittest
import tempfile
import time
from unittest.mock import patch

import automation
from auto_execution import (apply_automatic_trailing, authorization_result,
                            consensus_result, execute_if_fresh)
from execution_settings import load_execution_settings, save_execution_settings
from llm_advisors import decision_review_key
from main import decision_audit_signature
from notification_service import remove_subscription, save_subscription, signal_key
from audit_storage import compact_record


class AutomationTests(unittest.TestCase):
    def test_independent_mode_authorizes_ready_grounded_plan(self):
        decision = {'snapshot_id': 's1', 'action': 'BUY'}
        independent = {'snapshot_id': 's1', 'analysis': {
            'status': 'ok', 'setup_status': 'READY', 'recommended_action': 'BUY',
            'risk_tier': 'MEDIUM',
            'trade_plan': {'direction': 'BUY'}}}
        self.assertEqual(
            authorization_result('deterministic', decision)['status'], 'AUTHORIZED')
        result = authorization_result('independent', decision, independent_result=independent)
        self.assertEqual(result['status'], 'AUTHORIZED')
        self.assertEqual(result['signal_origin'], 'independent_llm')
        independent['analysis']['recommended_action'] = 'SELL'
        independent['analysis']['trade_plan']['direction'] = 'SELL'
        self.assertEqual(
            authorization_result('independent', decision, independent_result=independent)['status'],
            'AUTHORIZED')
        independent['analysis']['trade_plan'] = None
        self.assertEqual(
            authorization_result('independent', decision, independent_result=independent)['status'],
            'NO_AUTHORIZATION')
        independent['analysis']['trade_plan'] = {'direction': 'SELL'}
        independent['analysis']['risk_tier'] = None
        self.assertEqual(
            authorization_result('independent', decision, independent_result=independent)['status'],
            'NO_AUTHORIZATION')

    def test_independent_selected_risk_fraction_reaches_broker(self):
        from test_entry_plans import execution_case
        _, config, base_plan = execution_case()
        plan = {**base_plan, 'source': 'independent_llm', 'risk_tier': 'MEDIUM',
                'risk_reason': 'Clear but imperfect alignment.', 'risk_fraction': .2}
        current = {
            'snapshot_id': 'tier-live', 'setup_id': 'tier-setup',
            'setup_ref': '0123456789', 'setup_state': 'ARMED_LIMIT',
            'planned_direction': 'BUY', 'action': 'WAIT', 'symbol': 'XAUUSD.c',
            'account_mode': 'demo', 'entry_plan': plan, 'data_quality': [],
            'snapshot_time': time.time(), 'bid': 101, 'ask': 101.26,
        }
        calls = []

        class Broker:
            def place_pending_limit(self, *args):
                calls.append(args)
                return {'accepted': True, 'status': 'pending_submitted'}

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as folder:
            result = execute_if_fresh(
                current, current, {'status': 'AUTHORIZED', 'direction': 'BUY'},
                config, folder, Broker)
        self.assertTrue(result['accepted'])
        self.assertEqual(calls[0][4], .2)

    def test_execution_settings_are_validated_and_persisted(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(load_execution_settings(folder)['entry_mode'], 'consensus')
            saved = save_execution_settings(folder, {
                'entry_mode': 'deterministic', 'provider': 'astra',
                'auto_trailing': False})
            self.assertEqual(load_execution_settings(folder), saved)
            paused = save_execution_settings(folder, {'paused': True})
            self.assertTrue(paused['paused'])
            with self.assertRaises(ValueError):
                save_execution_settings(folder, {'entry_mode': 'unknown'})
            with self.assertRaisesRegex(ValueError, 'paused must be boolean'):
                save_execution_settings(folder, {'paused': 'yes'})

    def test_pause_blocks_final_execution_even_after_authorization(self):
        with tempfile.TemporaryDirectory() as folder:
            save_execution_settings(folder, {'paused': True})
            result = execute_if_fresh(
                {}, {}, {'status': 'AUTHORIZED'},
                {'automatic_execution': True}, folder,
                broker_factory=lambda: self.fail('broker must not be created while paused'))
        self.assertFalse(result['attempted'])
        self.assertEqual(result['status'], 'automation_paused')

    def test_execution_settings_api_updates_runtime_choices(self):
        import main
        from api_server import app
        with tempfile.TemporaryDirectory() as folder, patch.object(main, 'DATA_ROOT', folder):
            response = app.test_client().put('/api/execution/settings', json={
                'entry_mode': 'independent', 'provider': 'astra',
                'auto_trailing': True})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()['provider'], 'astra')
            self.assertEqual(app.test_client().get(
                '/api/execution/settings').get_json()['entry_mode'], 'independent')

    def test_three_way_consensus_requires_same_snapshot_ready_and_agree(self):
        decision = {'snapshot_id': 's1', 'action': 'SELL'}
        review = {'snapshot_id': 's1', 'review': {
            'status': 'ok', 'verdict': 'AGREE', 'recommended_action': 'SELL'}}
        independent = {'snapshot_id': 's1', 'analysis': {
            'status': 'ok', 'setup_status': 'READY', 'recommended_action': 'SELL',
            'risk_tier': 'MEDIUM'}}
        self.assertEqual(consensus_result(decision, review, independent)['status'],
                         'CONSENSUS')
        independent['analysis']['recommended_action'] = 'WAIT'
        self.assertEqual(consensus_result(decision, review, independent)['status'],
                         'NO_CONSENSUS')

    def test_authorization_explains_provider_failure(self):
        decision = {'snapshot_id': 's1', 'action': 'BUY'}
        unavailable = {'snapshot_id': 's1', 'analysis': {
            'status': 'unavailable', 'error': 'browser timed out'}}
        result = authorization_result(
            'independent', decision, independent_result=unavailable)
        self.assertEqual(result['status'], 'NO_AUTHORIZATION')
        self.assertIn('independent_analysis_unavailable', result['blockers'])
        self.assertEqual(result['errors'], ['browser timed out'])

    def test_structured_provider_failure_is_exposed(self):
        unavailable = {'analysis': {
            'status': 'unavailable', 'error': 'Gemini prompt did not submit'}}
        self.assertEqual(automation._provider_error(unavailable),
                         'Gemini prompt did not submit')

    def test_consensus_short_circuit_gate_requires_ready_same_direction(self):
        decision = {'snapshot_id': 's1', 'action': 'SELL'}
        ready = {'snapshot_id': 's1', 'analysis': {
            'status': 'ok', 'setup_status': 'READY', 'recommended_action': 'SELL',
            'risk_tier': 'MEDIUM'}}
        waiting = {'snapshot_id': 's1', 'analysis': {
            'status': 'ok', 'setup_status': 'WATCH', 'recommended_action': 'WAIT'}}
        self.assertTrue(automation._independent_can_authorize(decision, ready))
        self.assertFalse(automation._independent_can_authorize(decision, waiting))

    def test_independent_mode_uses_material_event_trigger_without_deterministic_plan(self):
        candle = {'time': 300, 'open': 100, 'high': 103, 'low': 99, 'close': 102}
        decision = {
            'snapshot_id': 'event-1', 'snapshot_time': 1,
            'action': 'WAIT', 'setup_state': 'NO_SETUP',
            'buy_setup_score': 0, 'sell_setup_score': 0, 'data_quality': [],
            'positions_summary': {'positions': [], 'pending_orders': []},
            'chart_data': {'M5': [candle, {**candle, 'time': 600}]},
            'structure': {'M5': {'atr14_simple': 2}},
            'chart_analysis': {'zones': []},
        }
        with patch.object(automation, 'AUTO_GEMINI', True):
            self.assertEqual(automation._independent_market_event(decision),
                             'm5_displacement:300:up')

    def test_independent_trigger_continues_with_breakeven_runner(self):
        candle = {'time': 300, 'open': 100, 'high': 103, 'low': 99, 'close': 102}
        runner = {
            'ticket': 7, 'type': 0, 'volume': .1, 'price_open': 95, 'sl': 95,
            'magic': 77, 'comment': 'Tradew S:0123456789 L1R',
        }
        decision = {
            'snapshot_id': 'runner-event', 'data_quality': [],
            'action': 'WAIT', 'setup_state': 'NO_SETUP',
            'buy_setup_score': 0, 'sell_setup_score': 0,
            'positions_summary': {'positions': [runner], 'pending_orders': []},
            'chart_data': {'M5': [candle, {**candle, 'time': 600}]},
            'structure': {'M5': {'atr14_simple': 2}},
            'chart_analysis': {'zones': []},
        }
        with patch.object(automation, 'AUTO_GEMINI', True), \
                patch.object(automation.main, 'load_config',
                             return_value={'execution_magic': 77}):
            self.assertEqual(automation._independent_market_event(decision),
                             'm5_displacement:300:up')
            decision['positions_summary']['positions'][0]['sl'] = 94.99
            self.assertIsNone(automation._independent_market_event(decision))

    def test_independent_execution_refreshes_broker_snapshot_after_ai(self):
        source = {'snapshot_id': 'blind-old', 'snapshot_time': time.time()-90}
        fresh = {'snapshot_id': 'broker-fresh', 'snapshot_time': time.time(),
                 'symbol': 'XAUUSD.c'}
        planned = {
            'snapshot_id': 'broker-fresh', 'snapshot_time': fresh['snapshot_time'],
            'setup_id': 'setup-fresh', 'entry_plan': {'rule_version': 'independent-auto-22'},
        }
        independent = {'snapshot_id': 'blind-old', 'analysis': {'status': 'ok'}}
        authorization = {'status': 'AUTHORIZED', 'direction': 'BUY'}
        config = {
            'max_tick_age_seconds': 10, 'expected_login': 1,
            'expected_server': 'demo', 'expected_currency': 'USC',
            'expected_mode': 'demo',
        }
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(automation.main, 'DATA_ROOT', folder), \
                patch.object(automation.main, 'scan', return_value=fresh) as scan, \
                patch.object(automation, 'build_independent_execution_decision',
                             return_value=planned) as build, \
                patch('setup_tracker.SetupTracker.record') as record, \
                patch.object(automation, 'execute_if_fresh', return_value={
                    'attempted': True, 'accepted': True,
                    'status': 'pending_submitted'}) as execute:
            returned_fresh, returned_plan, result = (
                automation.execute_independent_authorization(
                    source, independent, authorization, config))
        scan.assert_called_once_with(audit_policy='changes', lock_timeout=20.0)
        build.assert_called_once_with(source, fresh, independent, config)
        record.assert_called_once()
        execute.assert_called_once_with(
            planned, planned, authorization, config, folder)
        self.assertIs(returned_fresh, fresh)
        self.assertIs(returned_plan, planned)
        self.assertEqual(result['source_snapshot_id'], 'blind-old')
        self.assertEqual(result['execution_snapshot_id'], 'broker-fresh')
        self.assertLess(result['execution_snapshot_age_seconds'], 1)

    def test_fresh_consensus_submits_once_and_blocks_duplicate_setup(self):
        import time
        from test_entry_plans import execution_case
        _, config, plan = execution_case()
        current = {
            'snapshot_id': 'fresh', 'setup_id': 'setup-1', 'setup_ref': '0123456789',
            'action': 'BUY', 'setup_state': 'ACTIONABLE', 'stop': 99,
            'targets': [102], 'symbol': 'XAUUSD.c',
            'account_mode': 'demo', 'data_quality': [],
            'entry_plan': plan, 'snapshot_time': time.time(), 'bid': 100.1, 'ask': 100.36,
        }
        reviewed = {**current, 'snapshot_id': 'old'}
        consensus = {'status': 'CONSENSUS', 'direction': 'BUY'}
        config = {**config,
            'automatic_execution': True, 'expected_mode': 'demo',
            'balance_risk_fraction': .5, 'cost_per_lot': 5,
            'order_deviation_points': 50, 'execution_magic': 123,
        }
        calls = []

        class Broker:
            def execute_market_order(self, *args):
                calls.append(args)
                return {'accepted': True, 'status': 'submitted', 'order': 7}

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as folder:
            first = execute_if_fresh(current, reviewed, consensus, config, folder,
                                     broker_factory=Broker)
            second = execute_if_fresh(current, reviewed, consensus, config, folder,
                                      broker_factory=Broker)
        self.assertTrue(first['accepted'])
        self.assertEqual(second['status'], 'duplicate_setup_blocked')
        self.assertEqual(len(calls), 1)

    def test_auto_trailing_submits_only_stop_improvements(self):
        decision = {
            'account_mode': 'demo', 'data_quality': [],
            'positions_summary': {'positions': [
                {'ticket': 9, 'symbol': 'XAUUSD.c'}]},
            'proposed_management_changes': [
                {'ticket': 9, 'action': 'REDUCE RISK', 'proposed_stop': 101},
                {'ticket': 10, 'action': 'CLOSE', 'proposed_stop': 99}],
        }
        calls = []

        class Broker:
            def modify_position_stop(self, *args):
                calls.append(args)
                return {'accepted': True, 'status': 'stop_modified'}

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as folder:
            result = apply_automatic_trailing(
                decision, {'expected_mode': 'demo', 'execution_magic': 1},
                folder, broker_factory=Broker)
        self.assertEqual(len(result), 1)
        self.assertEqual(calls, [(9, 'XAUUSD.c', 101, 1)])

    def test_review_key_ignores_ticks_but_tracks_score_bands(self):
        base = {
            'symbol': 'XAUUSD', 'action': 'WAIT', 'setup_id': 'x:breakout:1',
            'setup_state': 'WATCH_LEVEL', 'buy_setup_score': 61,
            'sell_setup_score': 20, 'data_quality': [], 'snapshot_id': 'first',
            'bid': 4300.0,
        }
        next_tick = {**base, 'snapshot_id': 'second', 'bid': 4300.5,
                     'buy_setup_score': 64}
        stronger = {**next_tick, 'buy_setup_score': 65}
        self.assertEqual(decision_review_key(base), decision_review_key(next_tick))
        self.assertNotEqual(decision_review_key(base), decision_review_key(stronger))

    def test_auto_gemini_requires_clean_potential_signal(self):
        decision = {
            'snapshot_id': 's1', 'action': 'WAIT', 'setup_state': 'WATCH_LEVEL',
            'buy_setup_score': 60, 'sell_setup_score': 10, 'data_quality': [],
        }
        with patch.object(automation, 'AUTO_GEMINI', True), \
                patch.object(automation, 'MIN_SCORE', 60):
            self.assertTrue(automation._potential_signal(decision))
            self.assertFalse(automation._potential_signal(
                {**decision, 'data_quality': ['Stale tick']}))
            self.assertFalse(automation._potential_signal(
                {**decision, 'setup_state': 'NO_SETUP'}))
            self.assertFalse(automation._potential_signal(
                {**decision, 'setup_state': 'EXPIRED'}))
            self.assertFalse(automation._potential_signal(
                {**decision, 'setup_state': 'INVALIDATED'}))
            self.assertTrue(automation._potential_signal(
                {**decision, 'setup_state': 'ARMED_LIMIT'}))
            self.assertFalse(automation.consider_automatic_review(
                decision, {'entry_mode': 'independent', 'provider': 'astra',
                           'auto_trailing': True, 'paused': True}))

    def test_continuous_audit_signature_ignores_display_prices(self):
        base = {
            'action': 'WAIT', 'setup_state': 'NO_SETUP', 'buy_setup_score': 10,
            'sell_setup_score': 20, 'reasons': ['No setup'], 'data_quality': [],
            'bid': 4300, 'ask': 4301, 'snapshot_id': 'one',
        }
        refreshed = {**base, 'bid': 4302, 'ask': 4303, 'snapshot_id': 'two'}
        self.assertEqual(decision_audit_signature(base),
                         decision_audit_signature(refreshed))

    def test_continuous_audit_uses_five_point_score_bands(self):
        base = {'action': 'WAIT', 'setup_state': 'WATCH_LEVEL',
                'buy_setup_score': 61, 'sell_setup_score': 20,
                'reasons': ['Watch'], 'data_quality': []}
        self.assertEqual(decision_audit_signature(base),
                         decision_audit_signature({**base, 'buy_setup_score': 64}))
        self.assertNotEqual(decision_audit_signature(base),
                            decision_audit_signature({**base, 'buy_setup_score': 65}))

    def test_compact_audit_omits_full_chart_and_pivot_arrays(self):
        record = {
            'rule_version': 'r', 'configuration': {}, 'outcome': None,
            'snapshot': {'bars': {'M5': [{'time': i} for i in range(20)]},
                         'recent_deals': [{'ticket': 1}], 'snapshot_id': 's'},
            'decision': {'action': 'WAIT', 'chart_data': {'M5': [1] * 100},
                         'structure': {'M5': {'trend': 'bullish', 'atr14_simple': 2,
                            'pivots': {'high': [{'price': 10}], 'low': [{'price': 5}]}}}},
        }
        compact = compact_record(record)
        self.assertNotIn('bars', compact['snapshot'])
        self.assertEqual(len(compact['snapshot']['bar_evidence']['M5']), 8)
        self.assertNotIn('chart_data', compact['decision'])
        self.assertNotIn('pivots', compact['decision']['structure']['M5'])
        self.assertEqual(compact['decision']['structure']['M5']['latest_confirmed_high'], 10)

    def test_phone_alerts_only_accept_clean_buy_or_sell_actions(self):
        buy = {'action': 'BUY', 'setup_id': 'x:breakout:buy:1', 'data_quality': []}
        self.assertEqual(signal_key(buy), 'BUY:x:breakout:buy:1')
        self.assertIsNone(signal_key({**buy, 'action': 'WAIT'}))
        self.assertIsNone(signal_key({**buy, 'data_quality': ['Stale tick']}))

    def test_push_subscription_is_validated_and_deduplicated(self):
        subscription = {
            'endpoint': 'https://push.example.test/device-1',
            'expirationTime': None,
            'keys': {'p256dh': 'a' * 65, 'auth': 'b' * 16},
        }
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(save_subscription(folder, subscription), 1)
            self.assertEqual(save_subscription(folder, subscription), 1)
            self.assertEqual(remove_subscription(folder, subscription['endpoint']), 0)
            with self.assertRaises(ValueError):
                save_subscription(folder, {**subscription, 'endpoint': 'http://unsafe.test'})


if __name__ == '__main__':
    unittest.main()
