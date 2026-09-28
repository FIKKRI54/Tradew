import unittest

from exit_management import (entry_exposure_allowed, position_role,
                             protected_runner, split_exit_volume)
from position_review import review
import test_scan_engine


class Broker:
    def profit(self, kind, symbol, volume, entry, exit_price):
        sign = 1 if kind == 0 else -1
        return (exit_price-entry)*sign*100*volume


class RunnerManagementTests(unittest.TestCase):
    def test_volume_split_is_closest_valid_65_35_allocation(self):
        self.assertEqual(split_exit_volume(.08, .01, .01, .65), (.05, .03))
        self.assertEqual(split_exit_volume(.03, .01, .01, .65), (.02, .01))
        self.assertEqual(split_exit_volume(.01, .01, .01, .65), (.01, 0.0))

    def test_broker_comment_roles_are_immutable_and_explicit(self):
        self.assertEqual(position_role('Tradew S:0123456789 L2T'), 'take_profit')
        self.assertEqual(position_role('Tradew S:0123456789 L2R'), 'runner')
        self.assertEqual(position_role('Tradew S:0123456789 L2'), 'legacy')

    def test_only_owned_breakeven_runners_allow_new_entry(self):
        buy = {
            'ticket': 1, 'type': 0, 'volume': .2, 'price_open': 100, 'sl': 100,
            'magic': 77, 'comment': 'Tradew S:0123456789 L1R',
        }
        sell = {
            'ticket': 2, 'type': 1, 'volume': .2, 'price_open': 105, 'sl': 104,
            'magic': 77, 'comment': 'Tradew S:0123456789 L2R',
        }
        self.assertTrue(protected_runner(buy, 77))
        self.assertTrue(protected_runner(sell, 77))
        self.assertTrue(entry_exposure_allowed([buy, sell], [], 77))
        self.assertFalse(entry_exposure_allowed(
            [{**buy, 'sl': 99.99}], [], 77))
        self.assertFalse(entry_exposure_allowed(
            [{**buy, 'comment': 'Tradew S:0123456789 L1T'}], [], 77))
        self.assertFalse(entry_exposure_allowed(
            [{**buy, 'magic': 88}], [], 77))
        self.assertFalse(entry_exposure_allowed([buy], [{'ticket': 9}], 77))

    def runner_case(self, current):
        snapshot, config = test_scan_engine.RulesTest().fixture()
        config.update(cost_per_lot=0, trailing_break_even_trigger_r=99,
                      trailing_risk_reduction_trigger_r=99)
        thesis = {
            'setup_ref': '0123456789', 'symbol': 'XAUUSD.c', 'direction': 'BUY',
            'account_login': 1, 'account_server': 'demo',
            'account_mode': 'demo', 'account_currency': 'USC',
            'original_entry': 100, 'original_invalidation': 95,
            'original_targets': [110], 'linked_tickets': [7, 8],
        }
        snapshot['positions'] = [{
            'ticket': 8, 'symbol': 'XAUUSD.c', 'type': 0, 'volume': .03,
            'price_open': 100, 'sl': 95, 'tp': 0, 'profit': 0,
            'comment': 'Tradew S:0123456789 L1R', 'original_setup': thesis,
        }]
        snapshot['tick'].update(bid=current, ask=current+.1)
        snapshot['initial_tick'].update(bid=current, ask=current+.1)
        snapshot['gold_ticks']['XAUUSD.c'].update(bid=current, ask=current+.1)
        snapshot['recent_deals'] = []
        contexts = {'M5': {'pivots': {'low': [{'price': 103}], 'high': []}}}
        return snapshot, config, contexts

    def test_runner_does_not_structurally_trail_before_tp1(self):
        snapshot, config, contexts = self.runner_case(105)
        result = review(snapshot, config, Broker(), contexts, .1)
        self.assertEqual(result['changes'], [])
        self.assertFalse(result['risk_by_ticket'][0]['runner_active'])

    def test_runner_trails_confirmed_m5_structure_after_tp1(self):
        snapshot, config, contexts = self.runner_case(111)
        result = review(snapshot, config, Broker(), contexts, .1)
        self.assertEqual(result['changes'][0]['action'], 'TRAIL SL')
        self.assertEqual(result['changes'][0]['management_stage'], 'runner_structure')
        self.assertTrue(result['risk_by_ticket'][0]['runner_active'])

    def test_tp_exit_deal_keeps_runner_active_after_retrace(self):
        snapshot, config, contexts = self.runner_case(106)
        snapshot['recent_deals'] = [{
            'position_id': 7, 'entry_role': 'OUT', 'reason': 5,
        }]
        result = review(snapshot, config, Broker(), contexts, .1)
        self.assertEqual(result['changes'][0]['management_stage'], 'runner_structure')

    def test_evaluator_keeps_scanning_with_breakeven_runner(self):
        from scan_engine import evaluate
        snapshot, config, _ = self.runner_case(105)
        config.update(trailing_risk_reduction_trigger_r=.5,
                      trailing_break_even_trigger_r=1.0)
        snapshot['positions'][0].update(
            sl=100, magic=config['execution_magic'])
        result = evaluate(snapshot, config, Broker())
        self.assertTrue(result['positions_summary']['new_entries_allowed'])
        self.assertEqual(result['positions_summary']['entry_exposure_state'],
                         'protected_runners_only')
        self.assertIn('observed_setups', result)


if __name__ == '__main__':
    unittest.main()
