import tempfile
import unittest
from unittest.mock import patch

import test_scan_engine
from auto_execution import authorization_result
from execution_settings import load_execution_settings, save_execution_settings
from limit_ladder import (PREFLIGHT_SUBSET_PROFILE, ladder_quality_acceptable,
                          preflight_ladder_layers)
from scan_engine import (controlling_entry_zone, evaluate,
                         overlapping_protection_zone)


class Broker:
    def profit(self, kind, symbol, volume, entry, exit):
        return (exit-entry)*100*volume*(1 if kind == 0 else -1)

    def margin(self, kind, symbol, volume, entry):
        return 100*volume


class StrategyV30Tests(unittest.TestCase):
    def test_stronger_higher_timeframe_zone_defers_local_plan(self):
        snapshot, config = test_scan_engine.RulesTest().fixture()
        config.update(minimum_setup_score=0, higher_timeframe_opposition_block_threshold=0,
                      minimum_ladder_reward_risk=0)
        confirmation_time = snapshot['bars']['M5'][-2]['time']
        setup = {
            'direction': 'SELL', 'family': 'order_block_retest',
            'level': 100.05, 'invalidation': 102.0,
            'entry_zone': {'low': 100.05, 'high': 101.0,
                           'trigger_side': 'BID', 'direction': 'SELL'},
            'confirmation_time': confirmation_time,
            'confirmation_close': 99.0, 'state': 'CONFIRMED',
            'chart_patterns': ['bearish_rejection'],
        }
        context = {'trend': 'bearish', 'atr14_simple': 1,
                   'pivots': {'high': [{'price': 103}], 'low': [{'price': 90}]}}
        zones = [
            {'kind': 'SUPPLY', 'price': 102.75, 'low': 102.5, 'high': 103.0,
             'status': 'active', 'active': True, 'strength': 5,
             'active_strength': 5, 'sources': ['M15'], 'active_sources': ['M15'],
             'confirmed_time': confirmation_time-900},
            {'kind': 'DEMAND', 'price': 90, 'low': 89.8, 'high': 90.2,
             'status': 'active', 'active': True, 'strength': 3,
             'active_strength': 3, 'sources': ['H1'], 'active_sources': ['H1'],
             'confirmed_time': confirmation_time-3600},
        ]
        with patch('scan_engine.structure', return_value=context), \
                patch('scan_engine.structural_zones', return_value=zones), \
                patch('setups.detect', return_value=[setup]):
            result = evaluate(snapshot, config, Broker())
        self.assertEqual(result['setup_state'], 'WATCH_ZONE')
        self.assertEqual(result['setup_lifecycle'], 'WATCH_ZONE')
        self.assertIsNone(result['entry_plan'])
        self.assertEqual(result['watch_entry_zone']['sources'], ['M15'])
        self.assertIn('fresh completed rejection', result['reasons'][2].lower())

    def test_zone_selection_is_causal_and_directional(self):
        zones = [
            {'kind': 'SUPPLY', 'low': 102.5, 'high': 103,
             'status': 'active', 'active_sources': ['M15'],
             'active_strength': 4, 'confirmed_time': 900},
            {'kind': 'SUPPLY', 'low': 101.5, 'high': 102,
             'status': 'active', 'active_sources': ['H1'],
             'active_strength': 8, 'confirmed_time': 1100},
        ]
        selected = controlling_entry_zone(
            zones, 'SELL', 102, 100, 1000, 1, .01,
            {'controlling_zone_max_distance_atr': 3})
        self.assertEqual(selected['low'], 102.5)
        self.assertIsNone(controlling_entry_zone(
            zones, 'BUY', 98, 100, 1000, 1, .01,
            {'controlling_zone_max_distance_atr': 3}))

    def test_overlapping_higher_timeframe_zone_controls_protection(self):
        zone = {'kind': 'DEMAND', 'low': 98, 'high': 100,
                'status': 'active', 'active_sources': ['M15', 'H1'],
                'active_strength': 7, 'confirmed_time': 900}
        selected = overlapping_protection_zone(
            [zone], 'BUY', {'low': 99, 'high': 100.5}, 1000)
        self.assertIs(selected, zone)

    def test_live_plan_stop_sits_beyond_full_controlling_zone(self):
        snapshot, config = test_scan_engine.RulesTest().fixture()
        config.update(minimum_setup_score=0, higher_timeframe_opposition_block_threshold=0,
                      minimum_ladder_reward_risk=0)
        confirmation_time = snapshot['bars']['M5'][-2]['time']
        supply = {
            'kind': 'SUPPLY', 'price': 100.75, 'low': 100.5, 'high': 101.0,
            'status': 'active', 'active': True, 'strength': 5,
            'active_strength': 5, 'sources': ['M15'], 'active_sources': ['M15'],
            'confirmed_time': confirmation_time-900,
        }
        demand = {
            'kind': 'DEMAND', 'price': 90, 'low': 89.8, 'high': 90.2,
            'status': 'active', 'active': True, 'strength': 3,
            'active_strength': 3, 'sources': ['H1'], 'active_sources': ['H1'],
            'confirmed_time': confirmation_time-3600,
        }
        setup = {
            'direction': 'SELL', 'family': 'supply_rejection',
            'level': 100.5, 'invalidation': 101.0,
            'entry_zone': {'low': 100.5, 'high': 101.0,
                           'trigger_side': 'BID', 'direction': 'SELL'},
            'structural_zone': supply,
            'confirmation_time': confirmation_time,
            'confirmation_close': 99.0, 'state': 'CONFIRMED',
            'chart_patterns': ['bearish_rejection'],
        }
        context = {'trend': 'bearish', 'atr14_simple': 1,
                   'pivots': {'high': [{'price': 101}], 'low': [{'price': 90}]}}
        with patch('scan_engine.structure', return_value=context), \
                patch('scan_engine.structural_zones', return_value=[supply, demand]), \
                patch('setups.detect', return_value=[setup]):
            result = evaluate(snapshot, config, Broker())
        self.assertEqual(result['setup_state'], 'WAIT_RETEST')
        self.assertGreaterEqual(result['entry_plan']['stop'], 101.25)
        self.assertEqual(result['entry_plan']['structural_invalidation'], 101.0)

    def test_preflight_keeps_only_valid_layers_without_redistributing_risk(self):
        plan = {
            'direction': 'SELL', 'stop': 106, 'm5_atr': 4,
            'risk_fraction': .30,
            'limit_layers': [
                {'index': 1, 'quality': 'near', 'price': 101,
                 'target': 95, 'risk_weight': .25},
                {'index': 2, 'quality': 'middle', 'price': 103,
                 'target': 95, 'risk_weight': .35},
                {'index': 3, 'quality': 'deep', 'price': 105.5,
                 'target': 95, 'risk_weight': .40},
            ],
        }
        config = {
            'minimum_layer_stop_atr_fraction': .25,
            'minimum_ladder_reward_risk': .8,
            'take_profit_close_fraction': .65,
            'minimum_ladder_span_spread_multiple': 2,
            'minimum_ladder_span_atr_fraction': .25,
        }
        filtered = preflight_ladder_layers(plan, 100, 100.2, config)
        self.assertEqual([layer['index'] for layer in filtered['limit_layers']], [2])
        self.assertEqual(filtered['ladder_execution_profile'], PREFLIGHT_SUBSET_PROFILE)
        self.assertAlmostEqual(filtered['limit_ladder_total_risk_fraction'], .105)
        self.assertEqual(filtered['preflight_layers_requested'], 3)
        accepted, _, _ = ladder_quality_acceptable(filtered, 100, 100.2, config)
        self.assertTrue(accepted)

    def test_reviewed_mode_requires_same_snapshot_informed_agreement(self):
        decision = {'snapshot_id': 's1', 'action': 'SELL'}
        review = {'snapshot_id': 's1', 'review': {
            'status': 'ok', 'verdict': 'AGREE', 'recommended_action': 'SELL'}}
        result = authorization_result('reviewed', decision, review_result=review)
        self.assertEqual(result['status'], 'AUTHORIZED')
        self.assertEqual(result['signal_origin'], 'reviewed_deterministic')
        review['snapshot_id'] = 'old'
        self.assertEqual(
            authorization_result('reviewed', decision, review_result=review)['status'],
            'NO_AUTHORIZATION')

    def test_reviewed_mode_is_a_persisted_runtime_choice(self):
        with tempfile.TemporaryDirectory() as folder:
            saved = save_execution_settings(folder, {'entry_mode': 'reviewed'})
            self.assertEqual(saved['entry_mode'], 'reviewed')
            self.assertEqual(load_execution_settings(folder)['entry_mode'], 'reviewed')


if __name__ == '__main__':
    unittest.main()
