import copy
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from auto_execution import build_independent_execution_decision
from exit_management import tp1_payoff
from independent_validation import (broker_independent_blocker, confirmation_evidence,
                                    entry_reachability, supported_entry,
                                    validate_independent_geometry,
                                    validate_market_freshness)
from limit_ladder import attach_limit_ladder
from mt5_connector import MT5Adapter, _execution_history_blocker
from test_entry_plans import Terminal, execution_case
from test_independent_ai import decision_fixture


class History:
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY = 0, 1, 3

    def __init__(self, deals, positions=(), orders=()):
        self.deals, self.positions, self.orders = deals, positions, orders
        self.window = None

    def history_deals_get(self, start, end):
        self.window = (start.timestamp(), end.timestamp())
        return [d for d in self.deals if self.window[0] <= d.time <= self.window[1]]

    def positions_get(self):
        return self.positions

    def orders_get(self):
        return self.orders


def deal(timestamp, entry, profit=0, volume=1, position=1, magic=77, ref='0123456789'):
    return SimpleNamespace(time=timestamp, entry=entry, profit=profit, volume=volume,
                           position_id=position, magic=magic, comment='Tradew S:'+ref,
                           commission=0, swap=0, fee=0)


class LossCorrectionsV27Tests(unittest.TestCase):
    def history_case(self):
        now = 1790363139
        config = dict(timestamp_offset_seconds=10800, max_session_entries=100,
                      max_consecutive_losses=100, loss_cooldown_seconds=900)
        return now, config, SimpleNamespace(balance=1000)

    def test_broker_clock_offset_cannot_hide_recent_loss(self):
        now, config, account = self.history_case()
        h = History([deal(now+10800-120, 0), deal(now+10800-76, 1, -82.26)])
        self.assertEqual(_execution_history_blocker(h, account, 77, config, now), 'loss_cooldown')
        self.assertEqual(h.window[1], now+10800+60)

    def test_zero_offset_cooldown_and_expiry(self):
        now, config, account = self.history_case()
        config['timestamp_offset_seconds'] = 0
        h = History([deal(now-1200, 0), deal(now-901, 1, -20)])
        self.assertIsNone(_execution_history_blocker(h, account, 77, config, now))

    def test_future_deal_cannot_mask_real_loss(self):
        now, config, account = self.history_case()
        h = History([deal(now+10800-120, 0), deal(now+10800-60, 1, -20),
                     deal(now+10800+10, 0, position=2, ref='abcdef1234'),
                     deal(now+10800+20, 1, 50, position=2, ref='abcdef1234')])
        self.assertEqual(_execution_history_blocker(h, account, 77, config, now), 'loss_cooldown')

    def test_partial_exit_is_not_a_closed_setup(self):
        now, config, account = self.history_case()
        h = History([deal(now+10800-120, 0), deal(now+10800-60, 1, -20, volume=.4)])
        self.assertIsNone(_execution_history_blocker(h, account, 77, config, now))

    def test_pending_sibling_defers_setup_closure(self):
        now, config, account = self.history_case()
        h = History([deal(now+10800-120, 0), deal(now+10800-60, 1, -20)],
                    orders=[SimpleNamespace(magic=77, comment='Tradew S:0123456789 L2')])
        self.assertIsNone(_execution_history_blocker(h, account, 77, config, now))

    def test_mobile_exit_still_counts_but_other_trades_do_not(self):
        now, config, account = self.history_case()
        h = History([deal(now+10800-120, 0), deal(now+10800-60, 1, -20, magic=0),
                     deal(now+10800-100, 0, position=2, magic=999),
                     deal(now+10800-10, 1, 100, position=2, magic=999)])
        self.assertEqual(_execution_history_blocker(h, account, 77, config, now), 'loss_cooldown')

    def test_missing_exposure_history_fails_closed(self):
        now, config, account = self.history_case()
        self.assertEqual(_execution_history_blocker(History([], positions=None), account, 77, config, now),
                         'execution_history_unavailable')

    def test_session_limit_counts_setups_not_ladder_children(self):
        now, config, account = self.history_case()
        config['max_session_entries'] = 2
        h = History([deal(now+10800-120, 0, position=p) for p in (1, 2, 3)])
        self.assertIsNone(_execution_history_blocker(h, account, 77, config, now))
        h.deals.append(deal(now+10800-120, 0, position=4, ref='abcdef1234'))
        self.assertEqual(_execution_history_blocker(h, account, 77, config, now), 'session_entry_limit')

    def independent_case(self):
        source = decision_fixture()
        source['snapshot_time'] = time.time()
        source['positions_summary'] = {'positions': [], 'pending_orders': []}
        live = {**source, 'snapshot_id': 'fresh', 'account_mode': 'demo'}
        config = json.loads(Path('tests/fixtures/scan_config.json').read_text())
        config['expected_mode'] = 'demo'
        analysis = {'snapshot_id': source['snapshot_id'], 'analysis': {
            'status': 'ok', 'setup_status': 'READY', 'recommended_action': 'BUY',
            'risk_tier': 'MEDIUM', 'risk_reason': 'Verified reversal at structure.',
            'trade_plan': {'direction': 'BUY', 'order_type': 'BUY_LIMIT', 'entry_price': 122,
                           'stop_loss': 120, 'take_profit': 125, 'confirmation_level': 121,
                           'structural_invalidation': 120, 'expires_in_minutes': 15}}}
        return source, live, analysis, config

    def test_supported_plan_preserves_exact_entry_and_risk_tier(self):
        s, live, a, c = self.independent_case()
        p = build_independent_execution_decision(s, live, a, c)['entry_plan']
        self.assertEqual(p['rule_version'], 'independent-auto-31')
        self.assertEqual([x['price'] for x in p['limit_layers']], [122])
        self.assertEqual(p['risk_fraction'], .2)
        self.assertAlmostEqual(p['limit_ladder_total_risk_fraction'], .05)
        self.assertIn('bullish_engulfing', p['confirmation_evidence']['patterns'])
        self.assertIn('entry_reachability', p['confirmation_evidence'])

    def test_distant_short_lived_limit_is_deferred_not_submitted(self):
        decision = decision_fixture()
        decision.update(bid=4234.92, ask=4235.18)
        config = json.loads(Path('tests/fixtures/scan_config.json').read_text())
        with self.assertRaisesRegex(ValueError, 'independent_entry_not_reachable'):
            entry_reachability(decision, 'SELL', 4265.09, 15, 5.762857, config)
        result = entry_reachability(decision, 'SELL', 4239.5, 15, 5.762857, config)
        self.assertLessEqual(result['distance'], result['maximum_distance'])

    def test_d1_only_zone_cannot_be_intraday_tp1(self):
        s, live, a, c = self.independent_case()
        for decision in (s, live):
            zone = decision['chart_analysis']['active_target_zones']['BUY'][0]
            zone['sources'] = ['D1']
            zone['active_sources'] = ['D1']
        with self.assertRaisesRegex(ValueError, 'active zone, confirmed pivot, or 1.50 ATR'):
            build_independent_execution_decision(s, live, a, c)

    def test_rollover_and_new_gap_are_not_fresh_entry_context(self):
        decision = decision_fixture()
        config = json.loads(Path('tests/fixtures/scan_config.json').read_text())
        decision['session'] = 'Rollover / closed-market window'
        with self.assertRaisesRegex(ValueError, 'independent_market_not_fresh'):
            validate_market_freshness(decision, 2, config)
        decision['session'] = 'Asia'
        decision['chart_data']['M5'][-2]['open'] += 10
        with self.assertRaisesRegex(ValueError, 'recent M5 gap'):
            validate_market_freshness(decision, 2, config)

    def test_sell_evidence_is_symmetric(self):
        s, live, a, c = self.independent_case()
        for bars in s['chart_data'].values():
            for b in bars:
                b.update(open=250-b['open'], close=250-b['close'],
                         high=250-b['low'], low=250-b['high'])
        s.update(bid=126.4, ask=126.6)
        for z in s['chart_analysis']['zones']:
            z.update(kind='DEMAND' if z['kind']=='SUPPLY' else 'SUPPLY',
                     low=250-z['high'], high=250-z['low'], price=250-z['price'])
        s['chart_analysis']['active_target_zones'] = {'BUY': [], 'SELL': [s['chart_analysis']['zones'][0]]}
        live = {**s, 'snapshot_id': 'fresh', 'account_mode': 'demo'}
        a['analysis']['recommended_action'] = 'SELL'
        p = a['analysis']['trade_plan']
        for key in ('entry_price', 'stop_loss', 'take_profit', 'confirmation_level', 'structural_invalidation'):
            p[key] = 250-p[key]
        p.update(direction='SELL', order_type='SELL_LIMIT')
        result = build_independent_execution_decision(s, live, a, c)['entry_plan']
        self.assertEqual(result['pending_price'], 128)
        self.assertIn('bearish_engulfing', result['confirmation_evidence']['patterns'])

    def test_stop_inside_thesis_is_rejected_without_repair(self):
        s, live, a, c = self.independent_case()
        a['analysis']['trade_plan']['stop_loss'] = 120.5
        with self.assertRaisesRegex(ValueError, 'inside structural invalidation'):
            build_independent_execution_decision(s, live, a, c)
        self.assertEqual(a['analysis']['trade_plan']['stop_loss'], 120.5)

    def test_buy_and_sell_stop_geometry_and_volatility(self):
        for direction, entry, stop, invalid in [('BUY', 4278.11, 4277, 4277.62),
                                                ('SELL', 4278.11, 4279.22, 4278.6)]:
            with self.subTest(direction=direction), self.assertRaisesRegex(ValueError, 'too small'):
                validate_independent_geometry(dict(direction=direction, pending_price=entry,
                    stop=stop, structural_invalidation=invalid), .26, 4.11, {})

    def test_opposing_m5_cannot_pass_on_m1_signal(self):
        s, live, a, c = self.independent_case()
        s['chart_data']['M5'][-2].update(open=123.2, close=121.8)
        with self.assertRaisesRegex(ValueError, 'M5 does not confirm direction'):
            build_independent_execution_decision(s, live, a, c)

    def test_countertrend_requires_reversal_break_not_soldiers_alone(self):
        bars = [dict(time=1, open=100, close=100.5, high=101, low=99),
                dict(time=2, open=100, close=101, high=101.2, low=99.8),
                dict(time=3, open=101, close=102, high=102.2, low=100.8)]
        self.assertIn('two_soldiers', confirmation_evidence(bars, 'BUY', 100, .1)['patterns'])
        with self.assertRaisesRegex(ValueError, 'countertrend'):
            confirmation_evidence(bars, 'BUY', 100, .1, True)

    def test_changed_confirmation_and_completed_reclaim_are_rejected(self):
        s, live, a, c = self.independent_case()
        live = copy.deepcopy(live)
        live['chart_data']['M5'][-2]['close'] += .01
        with self.assertRaisesRegex(ValueError, 'candle changed'):
            build_independent_execution_decision(s, live, a, c)
        live['chart_data'] = copy.deepcopy(s['chart_data'])
        live['chart_data']['M5'].insert(-1, {**s['chart_data']['M5'][-2],
                                           'time': s['chart_data']['M5'][-2]['time']+1, 'close': 120.9})
        with self.assertRaisesRegex(ValueError, 'reclaimed'):
            build_independent_execution_decision(s, live, a, c)

    def test_broken_zone_and_broken_pivot_cannot_support_entry(self):
        s = decision_fixture()
        s['chart_analysis']['zones'][1]['status'] = 'broken'
        s['structure']['M5']['pivots']['low'] = [{'price': 121.8, 'confirmed_bar': 100}]
        self.assertFalse(supported_entry(s, 'BUY', 122, .2))

    def test_three_layers_require_three_structural_prices(self):
        p = dict(rule_version='independent-auto-29', source='independent_llm', direction='SELL',
                 pending_price=100, level=98, stop=105, targets=[90], m5_atr=2, risk_fraction=.2,
                 verified_entry_prices=[100, 101, 102])
        ladder = attach_limit_ladder(p, 98, 98.26, {})
        self.assertEqual([x['price'] for x in ladder['limit_layers']], [100, 101, 102])
        self.assertEqual(sum(x['risk_weight'] for x in ladder['limit_layers']), 1)
        one = attach_limit_ladder({**p, 'verified_entry_prices': [100]}, 98, 98.26, {})
        self.assertEqual([x['price'] for x in one['limit_layers']], [100])

    def test_bad_near_layer_cannot_be_subsidized_by_deep_layers(self):
        m = Terminal()
        m.tick.bid, m.tick.ask = 101, 101.26
        _, c, p = execution_case()
        p = attach_limit_ladder(p, 101, 101.26, c)
        result = MT5Adapter(m).place_pending_limit('BUY', 'XAUUSD.c', 99, 102, .5, 5,
                                                  123, '0123456789', p, c)
        self.assertTrue(result['accepted'])
        self.assertNotIn(100.52, [r['price'] for r in m.sent])
        self.assertTrue(any(r['status'] == 'layer_reward_risk' for r in result['layer_checks']))
        for child in result['orders']:
            self.assertGreaterEqual(child['estimated_cash_reward']/child['estimated_cash_risk'], 1)

    def test_runner_upside_is_not_counted_as_banked_tp1_profit(self):
        c = {'runner_enabled': True, 'take_profit_close_fraction': .65}
        self.assertEqual(tp1_payoff(200, 5, 1, .01, .01, c), 125)
        self.assertEqual(tp1_payoff(200, 5, 1, .01, .01, {'runner_enabled': False}), 195)

    def test_all_weak_layers_return_explicit_reason_and_send_nothing(self):
        m = Terminal()
        m.tick.bid, m.tick.ask = 101, 101.26
        _, c, p = execution_case()
        c['minimum_ladder_reward_risk'] = 10
        p = attach_limit_ladder(p, 101, 101.26, c)
        result = MT5Adapter(m).place_pending_limit('BUY', 'XAUUSD.c', 99, 102, .5, 5,
                                                  123, '0123456789', p, c)
        self.assertEqual(result['status'], 'blocked_layer_reward_risk')
        self.assertFalse(result['send_attempted'])
        self.assertEqual(m.sent, [])

    def test_missing_v27_layers_cannot_bypass_new_sender_validation(self):
        m = Terminal()
        _, c, p = execution_case()
        p['rule_version'] = 'independent-auto-29'
        result = MT5Adapter(m).place_pending_limit('BUY', 'XAUUSD.c', 99, 102, .5, 5,
                                                  123, '0123456789', p, c)
        self.assertEqual(result['status'], 'blocked_independent_unverified_layer')
        self.assertEqual(m.sent, [])

    def test_bridge_reproduces_confirmation_and_rejects_modified_pattern(self):
        s, live, a, c = self.independent_case()
        m = Terminal()
        m.TIMEFRAME_M1, m.TIMEFRAME_H1, m.TIMEFRAME_H4, m.TIMEFRAME_D1 = 1, 60, 240, 1440
        chart = copy.deepcopy(s['chart_data'])
        for frame, seconds in [('M1', 60), ('M5', 300), ('H1', 3600),
                               ('H4', 14400), ('D1', 86400)]:
            for i, b in enumerate(chart[frame]):
                b['time'] = m.tick.time//seconds*seconds-(len(chart[frame])-1-i)*seconds
        s['chart_data'] = chart
        live['chart_data'] = chart
        plan = build_independent_execution_decision(s, live, a, c)['entry_plan']
        m.copy_rates_from_pos = lambda sym, tf, *_: chart[
            {1: 'M1', 5: 'M5', 60: 'H1', 240: 'H4', 1440: 'D1'}[tf]]
        m.tick.bid, m.tick.ask = 123.4, 123.6
        self.assertIsNone(broker_independent_blocker(m, 'XAUUSD.c', plan, m.tick, c))
        chart['M5'][-2].update(open=123.2, close=121.8)
        self.assertEqual(broker_independent_blocker(m, 'XAUUSD.c', plan, m.tick, c),
                         'independent_confirmation_failed')


if __name__ == '__main__':
    unittest.main()
