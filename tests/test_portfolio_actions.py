import copy
import time
import unittest
from collections import namedtuple
from types import SimpleNamespace

from mt5_connector import MT5Adapter
from portfolio_actions import build_half_close_plan


class HalfCloseTerminal:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_REAL = 2
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    SYMBOL_FILLING_FOK = 1
    SYMBOL_FILLING_IOC = 2
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    TRADE_ACTION_DEAL = 1
    ORDER_TIME_GTC = 0
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_DONE_PARTIAL = 10010

    def __init__(self):
        self.positions = [SimpleNamespace(
            ticket=index, identifier=index, symbol='XAUUSD.c', type=1,
            volume=.03, price_open=110+index, profit=3*index,
            magic=0, comment='', time=index)
            for index in range(1, 5)]
        self.sent = []

    def account_info(self):
        return SimpleNamespace(login=7, server='demo', currency='USC', trade_mode=0,
                               trade_allowed=True)

    def terminal_info(self):
        return SimpleNamespace(connected=True, trade_allowed=True, tradeapi_disabled=False)

    def symbol_info(self, symbol):
        return SimpleNamespace(volume_min=.01, volume_step=.01, digits=2,
                               filling_mode=1)

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(bid=100, ask=100.2, time=int(time.time()))

    def positions_get(self):
        return tuple(self.positions)

    def order_check(self, request):
        return namedtuple('Check', 'retcode request comment')(0, request, 'Done')

    def order_send(self, request):
        self.sent.append(copy.deepcopy(request))
        position = next(item for item in self.positions
                        if item.ticket == request['position'])
        remaining = round(position.volume-request['volume'], 2)
        if remaining:
            position.volume = remaining
        else:
            self.positions.remove(position)
        return namedtuple('Sent', 'retcode request order deal volume comment')(
            10009, request, request['position'], request['position']+100,
            request['volume'], 'Done')


def constraints():
    return {
        'symbol': 'XAUUSD.c', 'expected_login': 7, 'expected_server': 'demo',
        'expected_currency': 'USC', 'expected_mode': 'demo',
        'execution_magic': 26092026, 'order_deviation_points': 50,
        'timestamp_offset_seconds': 0, 'max_tick_age_seconds': 10,
    }


class PortfolioActionTests(unittest.TestCase):
    def test_plan_includes_external_positions_and_exact_half_volume(self):
        terminal = HalfCloseTerminal()
        plan = build_half_close_plan(
            terminal.account_info(), 'XAUUSD.c', terminal.positions, .01, .01,
            constraints()['execution_magic'])
        self.assertEqual(plan['position_count'], 4)
        self.assertEqual(plan['external_position_count'], 4)
        self.assertEqual(plan['tradew_position_count'], 0)
        self.assertAlmostEqual(plan['total_volume'], .12)
        self.assertAlmostEqual(plan['close_target_volume'], .06)
        self.assertEqual(plan['full_position_closes'], 2)
        self.assertEqual([item['ticket'] for item in plan['instructions']], [1, 3])

    def test_preview_never_sends_and_confirmed_snapshot_closes_half(self):
        terminal = HalfCloseTerminal()
        adapter = MT5Adapter(terminal)
        preview = adapter.preview_half_close('XAUUSD.c', constraints())
        self.assertTrue(preview['accepted'])
        self.assertEqual(terminal.sent, [])
        result = adapter.close_half_positions(
            'XAUUSD.c', preview['snapshot_token'], constraints())
        self.assertTrue(result['complete'])
        self.assertAlmostEqual(result['verified_closed_volume'], .06)
        self.assertAlmostEqual(result['remaining_volume'], .06)
        self.assertEqual(result['remaining_position_count'], 2)
        self.assertEqual([item['position'] for item in terminal.sent], [1, 3])
        self.assertTrue(all(item['type'] == terminal.ORDER_TYPE_BUY
                            for item in terminal.sent))

    def test_changed_position_snapshot_blocks_all_orders(self):
        terminal = HalfCloseTerminal()
        adapter = MT5Adapter(terminal)
        preview = adapter.preview_half_close('XAUUSD.c', constraints())
        terminal.positions[0].volume = .02
        result = adapter.close_half_positions(
            'XAUUSD.c', preview['snapshot_token'], constraints())
        self.assertEqual(result['status'], 'blocked_position_snapshot_changed')
        self.assertFalse(result['send_attempted'])
        self.assertEqual(terminal.sent, [])


if __name__ == '__main__':
    unittest.main()
