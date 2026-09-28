import copy
import unittest
from unittest.mock import patch

import test_scan_engine
from test_entry_gates import Broker
from replay_engine import replay_history, replay_m5
from scan_engine import evaluate


class ReplayLiveTests(unittest.TestCase):
    def active_zones(self):
        return [
            {'kind': 'SUPPLY', 'price': 110, 'low': 109.9, 'high': 110.1,
             'status': 'active', 'active': True, 'strength': 3,
             'sources': ['H1'], 'active_sources': ['H1'],
             'broken_sources': [], 'unknown_sources': []},
            {'kind': 'DEMAND', 'price': 98, 'low': 97.9, 'high': 98.1,
             'status': 'active', 'active': True, 'strength': 3,
             'sources': ['H1'], 'active_sources': ['H1'],
             'broken_sources': [], 'unknown_sources': []},
        ]

    def fixture(self):
        snapshot, config = test_scan_engine.RulesTest().fixture()
        now = snapshot['tick']['time']
        for b in snapshot['bars']['M1'][-11:]:
            b.update(open=101, close=101, high=102, low=100, spread=10)
        snapshot['bars']['M1'][-11].update(
            open=99.8, close=100.4, high=100.5, low=99.7, spread=10)
        for b in snapshot['bars']['M5'][-10:]:
            b.update(open=100, close=101, high=102, low=99)
        config.update(replay_slippage_price=0, replay_initial_balance=1000,
                      runner_enabled=False)
        setup = dict(direction='BUY', family='breakout_follow_through', level=100,
                     invalidation=98, confirmation_time=now-900,
                     confirmation_close=101, state='CONFIRMED')
        context = dict(trend='bullish', atr14_simple=1,
                       pivots={'high': [{'price': 110}], 'low': [{'price': 98}]})
        return snapshot, config, setup, context

    def test_replay_uses_production_evaluator_and_balance_risk(self):
        snapshot, config, setup, context = self.fixture()
        seen = []
        def inspect(s, c, b, **kwargs):
            for timeframe, seconds in test_scan_engine.TIMEFRAMES.items():
                self.assertTrue(all(bar['time']+seconds <= s['tick']['time']
                                    for bar in s['bars'][timeframe][:-1]))
            result = evaluate(s, c, b, **kwargs)
            seen.append(result)
            return result
        with patch('setups.detect', return_value=[setup]), \
                patch('scan_engine.structure', return_value=context), \
                patch('scan_engine.structural_zones', return_value=self.active_zones()), \
                patch('scan_engine.evaluate', side_effect=inspect):
            report = replay_history(snapshot, config, Broker(), count=2, auto_trailing=False)
        self.assertEqual(report['processed_events'], 10)
        self.assertFalse(any(item['data_quality'] for item in seen))
        self.assertEqual(len(report['trades']), 3)
        trade = report['trades'][0]
        total_risk = sum(item['initial_cash_risk'] for item in report['trades'])
        self.assertGreater(total_risk, 290)
        self.assertLessEqual(total_risk, 300)
        self.assertEqual(report['authorization_mode'], 'deterministic_counterfactual')

    def test_replay_models_tp_and_runner_children_like_live_submission(self):
        snapshot, config, setup, context = self.fixture()
        config['runner_enabled'] = True
        with patch('setups.detect', return_value=[setup]), \
                patch('scan_engine.structure', return_value=context), \
                patch('scan_engine.structural_zones', return_value=self.active_zones()):
            report = replay_history(snapshot, config, Broker(), count=2,
                                    auto_trailing=False)
        roles = [trade['management_role'] for trade in report['trades']]
        self.assertEqual(roles.count('take_profit'), 3)
        self.assertEqual(roles.count('runner'), 3)
        self.assertEqual(len(report['trades']), 6)

    def test_completed_reclaim_blocks_replay_like_live(self):
        snapshot, config, setup, context = self.fixture()
        snapshot['bars']['M5'][-3].update(close=99.5)
        setup['confirmation_time'] -= 300
        # Price is away from the zone before reclaim, and returns only after it.
        for bar in snapshot['bars']['M1'][-11:-6]:
            bar.update(open=101, close=101, high=101.2, low=100.8)
        with patch('setups.detect', return_value=[setup]), \
                patch('scan_engine.structure', return_value=context), \
                patch('scan_engine.structural_zones', return_value=self.active_zones()):
            report = replay_history(snapshot, config, Broker(), count=1, auto_trailing=False)
        self.assertEqual(report['trades'], [])

    def test_trailing_proposals_change_actual_replay_stop(self):
        snapshot, config, setup, context = self.fixture()
        proposals = dict(changes=[dict(ticket=-1, action='TRAIL SL', proposed_stop=100.05)],
                         risk_by_ticket=[], actual_portfolio_cash_risk=0, known_cash_risk=0,
                         planned_cash_risk=0, pending_cash_risk=0, alerts=[])
        with patch('setups.detect', return_value=[setup]), \
                patch('scan_engine.structure', return_value=context), \
                patch('scan_engine.structural_zones', return_value=self.active_zones()), \
                patch('position_review.review', return_value=proposals):
            on = replay_history(snapshot, config, Broker(), count=2, auto_trailing=True)
            off = replay_history(snapshot, config, Broker(), count=2, auto_trailing=False)
        self.assertEqual(on['trades'][0]['final_stop'], 100.05)
        self.assertEqual(on['trades'][0]['outcome'], 'STOP')
        self.assertEqual(off['trades'][0]['outcome'], 'HORIZON')
        self.assertEqual(off['trades'][0]['stop_changes'], [])

    def test_legacy_replay_does_not_enter_after_intervening_reclaim(self):
        snapshot, config, _, _ = self.fixture()
        bars = [dict(time=i*300, open=103, high=104, low=102, close=103, spread=10)
                for i in range(90)]
        bars[61].update(open=99, high=99.8, low=98.5, close=99)
        bars[62].update(open=100.2, high=106, low=100, close=105)
        setup = dict(direction='BUY', family='breakout_follow_through', level=100,
                     invalidation=97, confirmation_time=bars[60]['time'],
                     confirmation_close=103, state='CONFIRMED')
        def detected(known, *_):
            return [setup] if known[-1]['time'] == bars[60]['time'] else []
        with patch('setups.detect', side_effect=detected), \
                patch('replay_engine.pivots', return_value={'high': [{'price': 105}], 'low': []}):
            report = replay_m5(bars, config, Broker(), snapshot['symbol'],
                               snapshot['specifications'][snapshot['symbol']])
        self.assertEqual(report['trades'], [])

    def test_replay_future_candles_cannot_change_earlier_entry(self):
        snapshot, config, setup, context = self.fixture()
        altered = copy.deepcopy(snapshot)
        altered['bars']['H4'][-1]['high'] = 5000
        with patch('setups.detect', return_value=[setup]), \
                patch('scan_engine.structure', return_value=context), \
                patch('scan_engine.structural_zones', return_value=self.active_zones()):
            first = replay_history(snapshot, config, Broker(), count=2, auto_trailing=False)
            second = replay_history(altered, config, Broker(), count=2, auto_trailing=False)
        self.assertEqual(first['trades'], second['trades'])

    def test_replay_applies_post_loss_cooldown(self):
        snapshot, config, setup, context = self.fixture()
        snapshot['bars']['M1'][-11]['low'] = 97
        snapshot['bars']['M1'][-6]['low'] = 97
        def detected(bars, *_):
            return [{**setup, 'confirmation_time': bars[-1]['time']}]
        with patch('setups.detect', side_effect=detected), \
                patch('scan_engine.structure', return_value=context), \
                patch('scan_engine.structural_zones', return_value=self.active_zones()):
            report = replay_history(snapshot, config, Broker(), count=2, auto_trailing=False)
        self.assertEqual(len(report['trades']), 3)
        self.assertEqual(len({trade['setup_id'] for trade in report['trades']}), 1)
        self.assertTrue(all(trade['net_result'] < 0 for trade in report['trades']))
