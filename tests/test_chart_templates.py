"""Completed-candle chart templates and their information boundaries."""
import unittest

from setups import detect


def bars(count=30):
    return [dict(time=index*300, open=100, high=101, low=99, close=100)
            for index in range(count)]


def higher_timeframe_zone(price=99, confirmed_bar=3600, side='low'):
    return {'H1': {'pivots': {
        side: [{'price': price, 'time': 0, 'confirmed_bar': confirmed_bar}],
        'high' if side == 'low' else 'low': [],
    }}}


def higher_timeframe_bars(broken=False):
    closes = [100, 98.5 if broken else 100, 100]
    return {'H1': [dict(time=index*3600, open=close, high=close+.4,
                        low=close-.4, close=close)
                    for index, close in enumerate(closes)]}


class ChartTemplateTests(unittest.TestCase):
    def find(self, candles, contexts=None, **overrides):
        config = {'_contexts': contexts or {}, 'zone_atr_fraction': .25,
                  'limit_entry_atr_fraction': .25, 'minimum_zone_strength': 2,
                  'pattern_body_min_fraction': .5,
                  '_bars_by_timeframe': higher_timeframe_bars(), **overrides}
        return [item for item in detect(candles, .1, .01, 2, config)
                if item['state'] == 'CONFIRMED' and
                item['confirmation_time'] == candles[-1]['time']]

    def test_one_completed_sweep_candle_can_confirm_buy_and_sell(self):
        candles = bars()
        candles[15]['high'] = 104
        candles[18]['low'] = 98
        candles[-1].update(open=98.4, high=99.2, low=97.5, close=98.9)
        buy = next(item for item in self.find(candles)
                   if item['direction'] == 'BUY')
        self.assertEqual(buy['family'], 'sweep_reversal')
        self.assertEqual(buy['level'], 98)
        self.assertEqual(buy['invalidation'], 97.5)
        mirrored = [{**bar, 'open': 200-bar['open'], 'high': 200-bar['low'],
                     'low': 200-bar['high'], 'close': 200-bar['close']}
                    for bar in candles]
        sell = next(item for item in self.find(mirrored)
                    if item['direction'] == 'SELL')
        self.assertEqual(sell['family'], 'sweep_reversal')
        self.assertEqual(sell['level'], 102)

    def test_engulfing_at_known_demand_zone_is_a_setup(self):
        candles = bars()
        candles[-2].update(open=100.3, high=100.5, low=99.3, close=99.5)
        candles[-1].update(open=99.2, high=100.8, low=98.95, close=100.5)
        setup = next(item for item in self.find(candles, higher_timeframe_zone())
                     if item['direction'] == 'BUY')
        self.assertEqual(setup['family'], 'demand_rejection')
        self.assertIn('bullish_engulfing', setup['chart_patterns'])
        self.assertLessEqual(setup['entry_zone']['low'], 99)
        self.assertGreaterEqual(setup['entry_zone']['high'], 99)

    def test_zone_not_yet_confirmed_cannot_create_rejection_setup(self):
        candles = bars()
        candles[-2].update(open=100.3, high=100.5, low=99.3, close=99.5)
        candles[-1].update(open=99.2, high=100.8, low=98.95, close=100.5)
        future = higher_timeframe_zone(confirmed_bar=7200)
        self.assertFalse(any(item['family'] == 'demand_rejection'
                             for item in self.find(candles, future)))

    def test_zone_confirmed_only_at_signal_close_was_not_previously_known(self):
        candles = bars()
        candles[-2].update(open=100.3, high=100.5, low=99.3, close=99.5)
        candles[-1].update(open=99.2, high=100.8, low=98.95, close=100.5)
        confirmation = candles[-1]['time'] + 300 - 3600
        zone = higher_timeframe_zone(confirmed_bar=confirmation)
        self.assertFalse(any(item['family'] == 'demand_rejection'
                             for item in self.find(candles, zone)))

    def test_lower_timeframe_close_does_not_break_higher_timeframe_zone(self):
        candles = bars()
        candles[-5].update(open=100, high=100.2, low=98.2, close=98.5)
        candles[-2].update(open=100.3, high=100.5, low=99.3, close=99.5)
        candles[-1].update(open=99.2, high=100.8, low=98.95, close=100.5)
        setup = next(item for item in self.find(candles, higher_timeframe_zone())
                     if item['direction'] == 'BUY')
        self.assertEqual(setup['family'], 'demand_rejection')

    def test_source_timeframe_close_breaks_higher_timeframe_zone(self):
        candles = bars()
        candles[-2].update(open=100.3, high=100.5, low=99.3, close=99.5)
        candles[-1].update(open=99.2, high=100.8, low=98.95, close=100.5)
        self.assertFalse(any(item['family'] == 'demand_rejection'
                             for item in self.find(
                                 candles, higher_timeframe_zone(),
                                 _bars_by_timeframe=higher_timeframe_bars(broken=True))))

    def test_two_soldiers_from_demand_zone_are_explicit_confirmation(self):
        candles = bars()
        candles[-2].update(open=99.4, high=100.6, low=99, close=100.5)
        candles[-1].update(open=100.4, high=101.5, low=100.2, close=101.3)
        setup = next(item for item in self.find(candles, higher_timeframe_zone())
                     if item['direction'] == 'BUY')
        self.assertEqual(setup['family'], 'demand_two_soldiers')
        self.assertIn('two_soldiers', setup['chart_patterns'])

    def test_retest_of_previously_displaced_order_block_is_setup(self):
        candles = bars()
        candles[-4].update(open=100.5, high=100.6, low=98.8, close=99)
        candles[-3].update(open=99, high=101.3, low=98.9, close=101)
        candles[-2].update(open=100.6, high=101, low=100, close=100.4)
        candles[-1].update(open=100.1, high=101.5, low=99.4, close=101.3)
        setup = next(item for item in self.find(candles)
                     if item['direction'] == 'BUY')
        self.assertEqual(setup['family'], 'order_block_retest')
        self.assertEqual(setup['order_block']['time'], candles[-4]['time'])
        self.assertEqual(setup['entry_zone']['high'], 100.6)

    def test_broken_order_block_cannot_create_retest_setup(self):
        candles = bars()
        candles[-4].update(open=100.5, high=100.6, low=98.8, close=99)
        candles[-3].update(open=99, high=101.3, low=98.9, close=101)
        candles[-2].update(open=100.6, high=100.7, low=98.4, close=98.5)
        candles[-1].update(open=100.1, high=101.5, low=99.4, close=101.3)
        self.assertFalse(any(item['family'] == 'order_block_retest'
                             for item in self.find(candles)))

    def test_wick_only_displacement_is_not_an_order_block_retest(self):
        candles = bars()
        candles[-4].update(open=100.5, high=100.6, low=98.8, close=99)
        candles[-3].update(open=99, high=101.3, low=98.9, close=99.8)
        candles[-2].update(open=100.2, high=100.4, low=99.9, close=100.1)
        candles[-1].update(open=100.1, high=101.5, low=99.4, close=101.3)
        self.assertFalse(any(item['family'] == 'order_block_retest'
                             for item in self.find(candles)))

    def test_evaluator_accepts_sweep_as_context_reversal_evidence(self):
        from scan_engine import evaluate
        from test_scan_engine import RulesTest
        from test_entry_gates import Broker
        snapshot, config = RulesTest().fixture()
        for candle in snapshot['bars']['M5'][-31:-1]:
            candle.update(open=100, high=101, low=99, close=100)
        snapshot['bars']['M5'][-15]['low'] = 98
        snapshot['bars']['M5'][-2].update(open=98.4, high=99.2, low=97.5, close=98.9)
        decision = evaluate(snapshot, config, Broker())
        self.assertEqual(decision['setup_family'], 'sweep_reversal')
        self.assertNotEqual(decision['setup_state'], 'FILTERED')
        self.assertEqual(decision['planned_direction'], 'BUY')
        self.assertFalse(any('Context gate:' in reason
                             for reason in decision['reasons']))
        self.assertIsNotNone(decision.get('watch_entry_zone'))

    def test_two_rising_candles_without_structure_are_not_a_setup(self):
        candles = bars()
        candles[-2].update(open=99.6, high=100.6, low=99.5, close=100.5)
        candles[-1].update(open=100.4, high=101.5, low=100.3, close=101.3)
        self.assertFalse(self.find(candles))


if __name__ == '__main__':
    unittest.main()
