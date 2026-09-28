import unittest
from unittest.mock import patch
import test_scan_engine
from scan_engine import evaluate


class Broker:
    def profit(self, kind, symbol, volume, entry, exit):
        return (exit-entry)*100*volume*(1 if kind == 0 else -1)

    def margin(self, kind, symbol, volume, entry):
        return 100*volume


class EntryGatesTest(unittest.TestCase):
    def run_case(self, direction='BUY', confirmation=None, level=None, target=None,
                 reclaimed=False, extra_setup=False, broker=None):
        snapshot, config = test_scan_engine.RulesTest().fixture()
        buy = direction == 'BUY'
        config.update(minimum_setup_score=0, minimum_higher_timeframe_alignment=0,
                      minimum_ladder_reward_risk=0)
        setup = dict(direction=direction, family='breakout_follow_through',
                     level=level if level is not None else (99.95 if buy else 100.05),
                     invalidation=98 if buy else 102,
                     confirmation_time=snapshot['bars']['M5'][-2]['time'],
                     confirmation_close=confirmation if confirmation is not None else (101 if buy else 99),
                     state='CONFIRMED')
        if reclaimed:
            setup['confirmation_time'] = snapshot['bars']['M5'][-3]['time']
            snapshot['bars']['M5'][-2].update(open=99, close=98.9, low=98, high=100)
        context = dict(trend=('neutral' if extra_setup else
                              'bullish' if buy else 'bearish'), atr14_simple=1,
                       pivots={'high': [{'price': target or 110}], 'low': [{'price': 90}]})
        setups = [setup]
        if extra_setup:
            setups.append(dict(setup, direction='SELL', level=100.05, invalidation=102,
                               confirmation_close=100))
        target_price = target if target is not None else 110
        zones = [
            {'kind': 'SUPPLY', 'price': target_price,
             'low': target_price-.1, 'high': target_price+.1,
             'status': 'active', 'active': True, 'strength': 3,
             'sources': ['H1'], 'active_sources': ['H1'],
             'broken_sources': [], 'unknown_sources': []},
            {'kind': 'DEMAND', 'price': 90, 'low': 89.9, 'high': 90.1,
             'status': 'active', 'active': True, 'strength': 3,
             'sources': ['H1'], 'active_sources': ['H1'],
             'broken_sources': [], 'unknown_sources': []},
        ]
        with patch('scan_engine.structure', return_value=context), \
                patch('scan_engine.structural_zones', return_value=zones), \
                patch('setups.detect', return_value=setups):
            return evaluate(snapshot, config, broker or Broker())

    def test_favorable_pullbacks_wait_for_completed_retest(self):
        for direction in ('BUY', 'SELL'):
            with self.subTest(direction=direction):
                result = self.run_case(direction)
                self.assertEqual(result['action'], 'WAIT')
                self.assertEqual(result['planned_direction'], direction)
                self.assertEqual(result['setup_state'], 'WAIT_RETEST')

    def test_extended_price_waits_for_virtual_limit_instead_of_chasing(self):
        for direction, level in [('BUY', 99), ('SELL', 101)]:
            result = self.run_case(direction, level=level)
            self.assertEqual(result['action'], 'WAIT')
            self.assertEqual(result['setup_state'], 'WAIT_RETEST')
            self.assertNotIn('entry', result)

    def test_favorable_quote_must_hold_setup_level(self):
        for direction, level in [('BUY', 100.05), ('SELL', 99.95)]:
            result = self.run_case(direction, level=level)
            self.assertEqual(result['action'], 'WAIT')
            self.assertIn('no longer holds', result['reasons'][0])

    def test_completed_reclaim_requires_new_confirmation(self):
        result = self.run_case(reclaimed=True)
        self.assertEqual(result['setup_state'], 'INVALIDATED')
        self.assertEqual(result['action'], 'WAIT')

    def test_near_structural_target_reports_ladder_rr(self):
        result = self.run_case(target=100.5)
        self.assertEqual(result['action'], 'WAIT')
        self.assertLess(result['entry_checks']['planned_ladder_reward_risk'], 1)

    def test_blocked_higher_score_does_not_hide_eligible_sell(self):
        result = self.run_case(confirmation=99, level=100.05, extra_setup=True)
        self.assertEqual(result['action'], 'WAIT')
        self.assertEqual(result['planned_direction'], 'SELL')
        self.assertEqual(len(result['candidate_checks']), 2)

    def test_broker_sizing_is_deferred_until_submission(self):
        class InvalidBroker(Broker):
            def margin(self, *args):
                return float('nan')
        result = self.run_case(broker=InvalidBroker())
        self.assertEqual(result['action'], 'WAIT')
        self.assertEqual(result['setup_state'], 'WAIT_RETEST')
