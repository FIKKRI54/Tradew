"""MT5 market-data, calculation, and authenticated execution adapters."""
import json
import math
import os
import re
import time
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_FLOOR
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from exit_management import (entry_exposure_allowed, exposure_signature,
                             split_exit_volume, tp1_payoff)


def _text(value):
    return str(value or '').upper()


def is_gold_symbol(symbol):
    """Recognize broker gold instruments without assuming the quote currency."""
    return (_text(getattr(symbol, 'currency_base', None)) == 'XAU' or
            _text(getattr(symbol, 'name', None)).startswith('XAU') or
            'GOLD' in _text(getattr(symbol, 'description', None)))


def is_gold_usd_symbol(symbol):
    return (is_gold_symbol(symbol) and
            _text(getattr(symbol, 'currency_base', None)) == 'XAU' and
            _text(getattr(symbol, 'currency_profit', None)) in ('USD', 'USC'))


def pending_exposure_signature(orders, gold_names):
    """Compare fixed broker order terms, not the mark-to-market order price."""
    keys = ('ticket', 'symbol', 'type', 'volume_current', 'price_open',
            'sl', 'tp', 'magic', 'comment', 'type_time', 'time_expiration')
    return sorted(tuple(getattr(order, key, None) for key in keys)
                  for order in orders if order.symbol in gold_names)


def _session_start(timestamp):
    """Return the current UTC market-session bucket start."""
    moment = datetime.fromtimestamp(timestamp, timezone.utc)
    hour = moment.hour
    start_hour = 0 if hour < 7 else 7 if hour < 12 else 12 if hour < 16 else 16 if hour < 21 else 21
    return moment.replace(hour=start_hour, minute=0, second=0, microsecond=0).timestamp()


def _execution_history_blocker(m, account, magic, constraints, now=None):
    """Apply cooldown, consecutive-loss, and session-entry brakes."""
    if not hasattr(m, 'history_deals_get'):
        return None
    now = now or time.time()
    offset = float(constraints.get('timestamp_offset_seconds') or 0)
    deals = m.history_deals_get(
        datetime.fromtimestamp(now+offset-30*86400, timezone.utc),
        datetime.fromtimestamp(now+offset+60, timezone.utc))
    if deals is None:
        return 'execution_history_unavailable'
    def deal_time(deal):
        return float(getattr(deal, 'time', 0) or 0)-offset
    deals = [deal for deal in deals if deal_time(deal) <= now+1]
    open_positions = m.positions_get() if hasattr(m, 'positions_get') else ()
    pending_orders = m.orders_get() if hasattr(m, 'orders_get') else ()
    if open_positions is None or pending_orders is None:
        return 'execution_history_unavailable'
    open_ids = {int(getattr(p, 'identifier', None) or getattr(p, 'ticket', 0))
                for p in open_positions}
    pending_refs = set()
    for order in pending_orders:
        match = re.search(r'(?:^|\s)S:([0-9a-f]{10})(?:\s|$)',
                          str(getattr(order, 'comment', '') or ''))
        if match and int(getattr(order, 'magic', 0) or 0) == int(magic):
            pending_refs.add(match.group(1))
    entry_in = getattr(m, 'DEAL_ENTRY_IN', 0)
    exit_roles = {getattr(m, 'DEAL_ENTRY_OUT', 1), getattr(m, 'DEAL_ENTRY_OUT_BY', 3)}
    openings = [deal for deal in deals
                if getattr(deal, 'entry', None) == entry_in and
                int(getattr(deal, 'magic', 0) or 0) == int(magic)]
    positions = {int(getattr(deal, 'position_id', 0) or 0) for deal in openings}
    positions.discard(0)
    linked = [deal for deal in deals
              if int(getattr(deal, 'position_id', 0) or 0) in positions]
    ref_by_position = {}
    for deal in openings:
        match = re.search(r'(?:^|\s)S:([0-9a-f]{10})(?:\s|$)',
                          str(getattr(deal, 'comment', '') or ''))
        if match:
            ref_by_position[int(getattr(deal, 'position_id', 0) or 0)] = match.group(1)
    session_refs = {
        ref_by_position.get(int(getattr(deal, 'position_id', 0) or 0),
                            str(getattr(deal, 'position_id', '')))
        for deal in openings
        if deal_time(deal) >= _session_start(now)
    }
    if len(session_refs) >= int(constraints['max_session_entries']):
        return 'session_entry_limit'
    grouped = {}
    for deal in linked:
        position_id = int(getattr(deal, 'position_id', 0) or 0)
        ref = ref_by_position.get(position_id, str(position_id))
        group = grouped.setdefault(ref, {'net': 0.0, 'exit': 0.0, 'has_exit': False,
                                        'remaining': 0.0, 'open': False})
        group['open'] |= position_id in open_ids or ref in pending_refs
        volume = float(getattr(deal, 'volume', 1.0))
        if getattr(deal, 'entry', None) == entry_in:
            group['remaining'] += volume
        group['net'] += sum(float(getattr(deal, field, 0) or 0)
                            for field in ('profit', 'commission', 'swap', 'fee'))
        if getattr(deal, 'entry', None) in exit_roles:
            group['remaining'] -= volume
            group['has_exit'] = True
            group['exit'] = max(group['exit'], deal_time(deal))
    closed = sorted((group for group in grouped.values() if group['has_exit']
                     and not group['open'] and group['remaining'] <= 1e-8),
                    key=lambda group: group['exit'], reverse=True)
    consecutive = 0
    for group in closed:
        if group['exit'] < _session_start(now):
            break
        if group['net'] < 0:
            consecutive += 1
        else:
            break
    if consecutive >= int(constraints['max_consecutive_losses']):
        return 'consecutive_loss_limit'
    if closed and closed[0]['net'] < 0 and now-closed[0]['exit'] < constraints['loss_cooldown_seconds']:
        return 'loss_cooldown'
    return None


def _retest_blocker(m, symbol, plan, direction):
    """Reproduce the rejection candle from broker M1 history."""
    if plan.get('rule_version') not in ('structure-research-18',
                                        'structure-research-19',
                                        'structure-research-20',
                                        'structure-research-21',
                                        'structure-research-22',
                                        'structure-research-23',
                                        'structure-research-24',
                                        'structure-research-25',
                                        'structure-research-26', 'structure-research-27',
                                        'structure-research-28', 'structure-research-29'):
        return None
    retest_time = plan.get('retest_confirmation_time')
    if (not isinstance(retest_time, (int, float)) or isinstance(retest_time, bool) or
            retest_time <= plan['confirmation_time']):
        return 'retest_unconfirmed'
    m1_rates = m.copy_rates_from_pos(symbol, m.TIMEFRAME_M1, 0, 90)
    if m1_rates is None or len(m1_rates) < 2:
        return 'retest_unavailable'
    from scan_engine import confirmed_retest
    completed_m1 = [{key: float(bar[key]) if key != 'time' else int(bar[key])
                     for key in ('time', 'open', 'high', 'low', 'close')}
                    for bar in m1_rates[:-1]]
    live_retest = confirmed_retest(
        completed_m1, plan['zone'], direction, plan['confirmation_time'])
    return None if live_retest == retest_time else 'retest_not_reproduced'

class MT5Adapter:
    def __init__(self, terminal=None):
        if terminal is None:
            import MetaTrader5 as terminal
        self.mt5 = terminal

    def profit(self, direction, symbol, volume, entry, stop):
        return self.mt5.order_calc_profit(direction, symbol, volume, entry, stop)

    def margin(self, direction, symbol, volume, entry):
        return self.mt5.order_calc_margin(direction, symbol, volume, entry)

    def _half_close_state(self, symbol, constraints):
        """Capture and validate the exact account/symbol state for a manual scale-out."""
        from portfolio_actions import build_half_close_plan
        m = self.mt5
        if hasattr(m, 'initialize'):
            terminal_path = os.environ.get('TRADEW_MT5_TERMINAL_PATH')
            if not (m.initialize(path=terminal_path) if terminal_path else m.initialize()):
                raise RuntimeError('MT5 initialization failed before half-close preview')
        if symbol != constraints.get('symbol'):
            return {'accepted': False, 'status': 'blocked_symbol_mismatch'}
        account, terminal = m.account_info(), m.terminal_info()
        info, positions = m.symbol_info(symbol), m.positions_get()
        if any(value is None for value in (account, terminal, info, positions)):
            raise RuntimeError('Fresh account, terminal, symbol, and positions are required')
        mode = {m.ACCOUNT_TRADE_MODE_DEMO: 'demo',
                m.ACCOUNT_TRADE_MODE_REAL: 'live'}.get(account.trade_mode)
        actual = dict(login=account.login, server=account.server,
                      currency=account.currency, mode=mode)
        expected = {key: constraints['expected_'+key] for key in actual}
        if actual != expected:
            return {'accepted': False, 'status': 'blocked_account_changed'}
        if (not terminal.connected or not terminal.trade_allowed or
                terminal.tradeapi_disabled or not account.trade_allowed):
            return {'accepted': False, 'status': 'blocked_algo_trading'}
        try:
            plan = build_half_close_plan(
                account, symbol, positions, info.volume_min, info.volume_step,
                constraints['execution_magic'])
        except ValueError as exc:
            return {'accepted': False, 'status': 'blocked_half_close_unavailable',
                    'error': str(exc)}
        return {'accepted': True, 'status': 'preview_ready', 'plan': plan,
                'account': account, 'info': info}

    def preview_half_close(self, symbol, constraints):
        """Return an exact-snapshot preview; this method never sends an order."""
        state = self._half_close_state(symbol, constraints)
        if not state.get('accepted'):
            return state
        return {'accepted': True, **state['plan']}

    def close_half_positions(self, symbol, snapshot_token, constraints):
        """Close 50% aggregate symbol volume, including explicitly confirmed external tickets."""
        m = self.mt5
        state = self._half_close_state(symbol, constraints)
        if not state.get('accepted'):
            return state
        plan, info = state['plan'], state['info']
        if (not isinstance(snapshot_token, str) or
                snapshot_token != plan['snapshot_token']):
            return {'accepted': False, 'status': 'blocked_position_snapshot_changed',
                    'send_attempted': False}

        fill_flags = info.filling_mode
        if fill_flags & getattr(m, 'SYMBOL_FILLING_FOK', 1):
            filling = m.ORDER_FILLING_FOK
        elif fill_flags & getattr(m, 'SYMBOL_FILLING_IOC', 2):
            filling = m.ORDER_FILLING_IOC
        else:
            filling = m.ORDER_FILLING_RETURN
        accepted_codes = {m.TRADE_RETCODE_DONE, m.TRADE_RETCODE_DONE_PARTIAL}
        results = []
        verified_closed = 0.0
        stopped_reason = None
        for instruction in plan['instructions']:
            current_positions = m.positions_get()
            current = None if current_positions is None else next((position for position in current_positions
                if int(position.ticket) == instruction['ticket']), None)
            if current is None:
                stopped_reason = 'position_disappeared_before_close'
                break
            if (current.symbol != symbol or int(current.type) != instruction['position_type'] or
                    abs(float(current.volume)-instruction['original_volume']) > info.volume_step/2):
                stopped_reason = 'position_changed_before_close'
                break
            tick = m.symbol_info_tick(symbol)
            if tick is None:
                stopped_reason = 'quote_unavailable'
                break
            age = time.time()-(tick.time-constraints['timestamp_offset_seconds'])
            if not 0 <= age <= constraints['max_tick_age_seconds']:
                stopped_reason = 'stale_quote'
                break
            close_kind = (m.ORDER_TYPE_SELL if current.type == m.ORDER_TYPE_BUY
                          else m.ORDER_TYPE_BUY)
            price = tick.bid if close_kind == m.ORDER_TYPE_SELL else tick.ask
            request = {
                'action': m.TRADE_ACTION_DEAL, 'symbol': symbol,
                'volume': instruction['close_volume'], 'type': close_kind,
                'position': instruction['ticket'], 'price': round(float(price), info.digits),
                'deviation': int(constraints['order_deviation_points']),
                'magic': int(constraints['execution_magic']),
                'comment': 'Tradew close 50pct', 'type_time': m.ORDER_TIME_GTC,
                'type_filling': filling,
            }
            checked = m.order_check(request)
            if checked is None:
                stopped_reason = 'order_check_unavailable'
                break
            check = checked._asdict()
            check.pop('request', None)
            if checked.retcode != 0:
                results.append({'ticket': instruction['ticket'], 'accepted': False,
                                'status': 'order_check_rejected', 'check': check})
                stopped_reason = 'order_check_rejected'
                break
            sent = m.order_send(request)
            if sent is None:
                stopped_reason = 'order_send_unavailable'
                break
            response = sent._asdict()
            response.pop('request', None)
            if sent.retcode not in accepted_codes:
                results.append({'ticket': instruction['ticket'], 'accepted': False,
                                'status': 'order_send_rejected', 'check': check,
                                'result': response})
                stopped_reason = 'order_send_rejected'
                break

            after_volume = instruction['original_volume']
            for _ in range(20):
                refreshed = m.positions_get()
                if refreshed is None:
                    break
                after = next((position for position in refreshed
                              if int(position.ticket) == instruction['ticket']), None)
                after_volume = float(after.volume) if after else 0.0
                if instruction['original_volume']-after_volume > info.volume_step/2:
                    break
                time.sleep(.05)
            actual_closed = max(0.0, instruction['original_volume']-after_volume)
            results.append({
                'ticket': instruction['ticket'], 'accepted': actual_closed > 0,
                'status': 'close_verified' if actual_closed > 0 else 'close_not_verified',
                'requested_volume': instruction['close_volume'],
                'closed_volume': actual_closed, 'remaining_ticket_volume': after_volume,
                'check': check, 'result': response,
            })
            verified_closed += actual_closed
            if abs(actual_closed-instruction['close_volume']) > info.volume_step/2:
                stopped_reason = 'partial_fill_did_not_match_request'
                break

        final_positions = m.positions_get()
        final_symbol_positions = ([] if final_positions is None else
                                  [position for position in final_positions
                                   if position.symbol == symbol])
        final_volume = sum(float(position.volume) for position in final_symbol_positions)
        complete = abs(verified_closed-plan['close_target_volume']) <= info.volume_step/2
        return {
            'accepted': verified_closed > 0,
            'complete': complete,
            'status': ('half_close_complete' if complete else
                       'half_close_partial' if verified_closed > 0 else
                       'half_close_rejected'),
            'send_attempted': bool(results), 'symbol': symbol,
            'initial_position_count': plan['position_count'],
            'initial_volume': plan['total_volume'],
            'target_close_volume': plan['close_target_volume'],
            'verified_closed_volume': verified_closed,
            'remaining_position_count': len(final_symbol_positions),
            'remaining_volume': final_volume,
            'external_position_count': plan['external_position_count'],
            'stopped_reason': stopped_reason,
            'results': results,
        }

    def execute_market_order(self, direction, symbol, stop, target, risk_fraction,
                             cost_per_lot, deviation_points, magic, setup_ref, plan, constraints):
        """Size against the latest balance/quote, pre-check, then submit one market order."""
        m = self.mt5
        from entry_plan import quote_blocker
        if hasattr(m, 'initialize'):
            terminal_path = os.environ.get('TRADEW_MT5_TERMINAL_PATH')
            if not (m.initialize(path=terminal_path) if terminal_path else m.initialize()):
                raise RuntimeError('MT5 initialization failed before submission')
        if direction not in ('BUY', 'SELL'):
            raise ValueError('direction must be BUY or SELL')
        if (not plan or plan['direction'] != direction or plan['symbol'] != symbol or
                plan['stop'] != stop or plan['targets'][0] != target or
                plan['risk_fraction'] != risk_fraction or plan['cost_per_lot'] != cost_per_lot):
            return {'accepted': False, 'status': 'blocked_plan_mismatch', 'send_attempted': False}
        account, info, tick = m.account_info(), m.symbol_info(symbol), m.symbol_info_tick(symbol)
        positions, orders, symbols = m.positions_get(), m.orders_get(), m.symbols_get()
        terminal = m.terminal_info()
        if any(value is None for value in (account, info, tick, positions, orders, symbols, terminal)):
            raise RuntimeError('Fresh account, symbol, quote, and exposure are required for execution')
        mode = {m.ACCOUNT_TRADE_MODE_DEMO: 'demo', m.ACCOUNT_TRADE_MODE_REAL: 'live'}.get(account.trade_mode)
        actual = dict(login=account.login, server=account.server, currency=account.currency, mode=mode)
        expected = {key: constraints['expected_' + key] for key in actual}
        if actual != expected or plan['account'] != expected:
            return {'accepted': False, 'status': 'blocked_account_changed'}
        if not terminal.connected or not terminal.trade_allowed or terminal.tradeapi_disabled or not account.trade_allowed:
            return {'accepted': False, 'status': 'blocked_algo_trading'}
        gold_names = {item.name for item in symbols if is_gold_symbol(item)} | {symbol}
        gold_positions = [item for item in positions if item.symbol in gold_names]
        gold_orders = [item for item in orders if item.symbol in gold_names]
        if not entry_exposure_allowed(gold_positions, gold_orders, magic):
            return {'accepted': False, 'status': 'blocked_existing_exposure'}
        initial_exposure = exposure_signature(gold_positions)
        history_blocker = _execution_history_blocker(
            m, account, magic, constraints)
        if history_blocker:
            return {'accepted': False, 'status': 'blocked_' + history_blocker,
                    'send_attempted': False}
        now = time.time()
        age = now - (tick.time - constraints['timestamp_offset_seconds'])
        if age < 0 or age > constraints['max_tick_age_seconds']:
            return {'accepted': False, 'status': 'blocked_stale_quote'}
        blocked = quote_blocker(plan, tick.bid, tick.ask, now, constraints['max_spread'])
        if blocked:
            return {'accepted': False, 'status': 'blocked_' + blocked}
        rates = m.copy_rates_from_pos(symbol, m.TIMEFRAME_M5, 0,
                                      constraints['confirmation_expiry_bars']+4)
        if rates is None or len(rates) < 2 or int(rates[-1]['time']) // 300 != tick.time // 300:
            return {'accepted': False, 'status': 'blocked_unsynchronized_structure'}
        sign = 1 if direction == 'BUY' else -1
        if any(int(bar['time']) > plan['confirmation_time'] and
               sign*(float(bar['close'])-plan['level']) <= 0 for bar in rates[:-1]):
            return {'accepted': False, 'status': 'blocked_completed_reclaim'}
        retest_blocker = _retest_blocker(m, symbol, plan, direction)
        if retest_blocker:
            return {'accepted': False, 'status': 'blocked_' + retest_blocker,
                    'send_attempted': False}
        kind = m.ORDER_TYPE_BUY if direction == 'BUY' else m.ORDER_TYPE_SELL
        sign = 1 if direction == 'BUY' else -1
        price = tick.ask if direction == 'BUY' else tick.bid
        stop = round(float(stop), info.digits)
        target = round(float(target), info.digits)
        if sign * (price-stop) <= 0 or sign * (target-price) <= 0:
            return {'accepted': False, 'status': 'blocked_price_moved',
                    'price': price, 'stop': stop, 'target': target}
        minimum_distance = max(info.trade_stops_level, info.trade_freeze_level) * info.point
        if abs(price-stop) < minimum_distance or abs(target-price) < minimum_distance:
            return {'accepted': False, 'status': 'blocked_broker_distance',
                    'minimum_distance': minimum_distance}
        loss_per_lot = m.order_calc_profit(kind, symbol, 1.0, price, stop)
        if loss_per_lot is None or not math.isfinite(loss_per_lot) or loss_per_lot >= 0:
            raise RuntimeError('Fresh broker loss calculation is unavailable or invalid')
        risk_budget = account.balance * risk_fraction
        cash_per_lot = -loss_per_lot + cost_per_lot
        raw_volume = min(risk_budget / cash_per_lot, info.volume_max)
        volume = float((Decimal(str(raw_volume)) / Decimal(str(info.volume_step))).to_integral_value(
            rounding=ROUND_FLOOR) * Decimal(str(info.volume_step)))
        if volume < info.volume_min:
            return {'accepted': False, 'status': 'blocked_minimum_volume',
                    'risk_budget': risk_budget}
        estimated_risk = cash_per_lot * volume
        margin = m.order_calc_margin(kind, symbol, volume, price)
        if (margin is None or not math.isfinite(margin) or margin < 0 or
                margin > account.margin_free*constraints['max_margin_fraction']):
            return {'accepted': False, 'status': 'blocked_margin'}
        fill_flags = info.filling_mode
        if fill_flags & getattr(m, 'SYMBOL_FILLING_FOK', 1):
            filling = m.ORDER_FILLING_FOK
        elif fill_flags & getattr(m, 'SYMBOL_FILLING_IOC', 2):
            filling = m.ORDER_FILLING_IOC
        else:
            filling = m.ORDER_FILLING_RETURN
        runner_enabled = bool(constraints.get('runner_enabled'))
        hedging_mode = getattr(m, 'ACCOUNT_MARGIN_MODE_RETAIL_HEDGING', 2)
        if (runner_enabled and
                getattr(account, 'margin_mode', hedging_mode) != hedging_mode):
            return {'accepted': False, 'status': 'blocked_runner_requires_hedging',
                    'send_attempted': False}
        take_volume, runner_volume = (
            split_exit_volume(volume, info.volume_min, info.volume_step,
                              constraints['take_profit_close_fraction'])
            if runner_enabled else (volume, 0.0))
        child_specs = [(('take_profit' if runner_enabled else 'legacy'),
                        take_volume, target, ('T' if runner_enabled else ''))]
        if runner_volume >= info.volume_min:
            child_specs.append(('runner', runner_volume, 0.0, 'R'))
        preflight = []
        for role, child_volume, child_target, suffix in child_specs:
            request = {
                'action': m.TRADE_ACTION_DEAL, 'symbol': symbol, 'volume': child_volume,
                'type': kind, 'price': price, 'sl': stop, 'tp': child_target,
                'deviation': int(deviation_points), 'magic': int(magic),
                'comment': (f'Tradew S:{setup_ref} L1{suffix}' if runner_enabled
                            else 'Tradew S:' + setup_ref),
                'type_time': m.ORDER_TIME_GTC, 'type_filling': filling,
            }
            checked = m.order_check(request)
            if checked is None:
                raise RuntimeError('MT5 order_check returned no result: ' + str(m.last_error()))
            check = checked._asdict()
            check.pop('request', None)
            if checked.retcode != 0:
                return {'accepted': False, 'status': 'order_check_rejected',
                        'check': check, 'risk_budget': risk_budget,
                        'estimated_cash_risk': estimated_risk,
                        'failed_role': role}
            preflight.append({'role': role, 'volume': child_volume,
                              'target': child_target, 'planned_target': target,
                              'request': request, 'check': check,
                              'estimated_cash_risk': cash_per_lot*child_volume})
        # Do not submit the old request if a new quote arrived during broker precheck.
        final_tick = m.symbol_info_tick(symbol)
        if final_tick is None or (final_tick.bid, final_tick.ask) != (tick.bid, tick.ask):
            return {'accepted': False, 'status': 'blocked_quote_changed_before_send'}
        if final_tick.time//300 != tick.time//300:
            return {'accepted': False, 'status': 'blocked_new_m5_before_send'}
        if time.time() >= plan['expires_at']:
            return {'accepted': False, 'status': 'blocked_plan_expired'}
        final_age = time.time() - (final_tick.time-constraints['timestamp_offset_seconds'])
        if not 0 <= final_age <= constraints['max_tick_age_seconds']:
            return {'accepted': False, 'status': 'blocked_stale_quote'}
        final_account = m.account_info()
        if final_account is None or any(getattr(final_account, key) != getattr(account, key)
                                       for key in ('login', 'server', 'currency', 'trade_mode', 'balance')):
            return {'accepted': False, 'status': 'blocked_account_changed'}
        if margin > final_account.margin_free*constraints['max_margin_fraction']:
            return {'accepted': False, 'status': 'blocked_margin'}
        final_positions, final_orders = m.positions_get(), m.orders_get()
        if final_positions is None or final_orders is None:
            return {'accepted': False, 'status': 'blocked_exposure_unavailable'}
        final_gold_positions = [item for item in final_positions if item.symbol in gold_names]
        final_gold_orders = [item for item in final_orders if item.symbol in gold_names]
        if (not entry_exposure_allowed(final_gold_positions, final_gold_orders, magic) or
                exposure_signature(final_gold_positions) != initial_exposure):
            return {'accepted': False, 'status': 'blocked_existing_exposure'}
        accepted_codes = {m.TRADE_RETCODE_DONE, m.TRADE_RETCODE_DONE_PARTIAL,
                          m.TRADE_RETCODE_PLACED}
        submitted, rejected = [], []
        for child in preflight:
            sent = m.order_send(child['request'])
            if sent is None:
                rejected.append({'role': child['role'], 'status': 'order_send_unavailable',
                                 'error': str(m.last_error()) if hasattr(m, 'last_error') else ''})
                if child['role'] == 'take_profit':
                    break
                continue
            response = sent._asdict()
            response.pop('request', None)
            record = {key: value for key, value in child.items() if key != 'request'}
            record['result'] = response
            if sent.retcode in accepted_codes:
                submitted.append(record)
            else:
                rejected.append({**record, 'status': 'order_send_rejected'})
                if child['role'] == 'take_profit':
                    break
        accepted = bool(submitted)
        split_complete = len(submitted) == len(preflight)
        first = submitted[0] if submitted else None
        return {
            'accepted': accepted,
            'send_attempted': True,
            'status': (('submitted_split' if len(preflight) > 1 else 'submitted')
                       if accepted and split_complete else
                       'submitted_partial' if accepted else 'order_send_rejected'),
            'execution_type': ('MARKET_TP_RUNNER' if len(preflight) > 1 else 'MARKET'),
            'direction': direction, 'symbol': symbol,
            'volume': sum(item['volume'] for item in submitted),
            'requested_price': price, 'stop': stop, 'target': target,
            'balance': account.balance, 'risk_fraction': risk_fraction,
            'risk_budget': risk_budget,
            'estimated_cash_risk': sum(item['estimated_cash_risk'] for item in submitted),
            'take_profit_close_fraction': constraints.get('take_profit_close_fraction'),
            'runner_enabled': len(preflight) > 1,
            'orders': submitted, 'rejected_orders': rejected,
            'check': first['check'] if first else preflight[0]['check'],
            'result': first['result'] if first else (rejected[0].get('result') or {}),
        }

    def place_pending_limit(self, direction, symbol, stop, target, risk_fraction,
                            cost_per_lot, magic, setup_ref, plan, constraints):
        """Place one protected, risk-sized native MT5 limit with server expiry."""
        if (plan and plan.get('rule_version') in ('independent-auto-27', 'independent-auto-28',
                                                  'independent-auto-29')
                and not plan.get('limit_layers')):
            return {'accepted': False, 'status': 'blocked_independent_unverified_layer', 'send_attempted': False}
        if plan and plan.get('limit_layers'):
            return self._place_pending_ladder(
                direction, symbol, stop, target, risk_fraction, cost_per_lot,
                magic, setup_ref, plan, constraints)
        m = self.mt5
        if hasattr(m, 'initialize'):
            terminal_path = os.environ.get('TRADEW_MT5_TERMINAL_PATH')
            if not (m.initialize(path=terminal_path) if terminal_path else m.initialize()):
                raise RuntimeError('MT5 initialization failed before pending submission')
        if direction not in ('BUY', 'SELL'):
            raise ValueError('direction must be BUY or SELL')
        if (not plan or plan['direction'] != direction or plan['symbol'] != symbol or
                plan['stop'] != stop or plan['targets'][0] != target or
                plan['risk_fraction'] != risk_fraction or plan['cost_per_lot'] != cost_per_lot):
            return {'accepted': False, 'status': 'blocked_plan_mismatch', 'send_attempted': False}
        account, info, tick = m.account_info(), m.symbol_info(symbol), m.symbol_info_tick(symbol)
        positions, orders, symbols = m.positions_get(), m.orders_get(), m.symbols_get()
        terminal = m.terminal_info()
        if any(value is None for value in (account, info, tick, positions, orders, symbols, terminal)):
            raise RuntimeError('Fresh account, symbol, quote, and exposure are required for pending submission')
        mode = {m.ACCOUNT_TRADE_MODE_DEMO: 'demo', m.ACCOUNT_TRADE_MODE_REAL: 'live'}.get(account.trade_mode)
        actual = dict(login=account.login, server=account.server, currency=account.currency, mode=mode)
        expected = {key: constraints['expected_' + key] for key in actual}
        if actual != expected or plan['account'] != expected:
            return {'accepted': False, 'status': 'blocked_account_changed'}
        if not terminal.connected or not terminal.trade_allowed or terminal.tradeapi_disabled or not account.trade_allowed:
            return {'accepted': False, 'status': 'blocked_algo_trading'}
        gold_names = {item.name for item in symbols if is_gold_symbol(item)} | {symbol}
        gold_positions = [item for item in positions if item.symbol in gold_names]
        gold_orders = [item for item in orders if item.symbol in gold_names]
        if not entry_exposure_allowed(gold_positions, gold_orders, magic):
            return {'accepted': False, 'status': 'blocked_existing_exposure'}
        initial_exposure = exposure_signature(gold_positions)
        now = time.time()
        age = now - (tick.time - constraints['timestamp_offset_seconds'])
        if age < 0 or age > constraints['max_tick_age_seconds']:
            return {'accepted': False, 'status': 'blocked_stale_quote'}
        spread = tick.ask-tick.bid
        if spread < 0 or spread > constraints['max_spread']+1e-9:
            return {'accepted': False, 'status': 'blocked_spread_exceeded'}
        if plan.get('rule_version') in ('structure-research-19',
                                        'independent-auto-19',
                                        'structure-research-20',
                                        'independent-auto-20',
                                        'structure-research-21',
                                        'independent-auto-21',
                                        'structure-research-22',
                                        'independent-auto-22',
                                        'structure-research-23',
                                        'independent-auto-23',
                                        'structure-research-24',
                                        'independent-auto-24',
                                        'structure-research-25',
                                        'structure-research-26',
                                        'independent-auto-25',
                                        'independent-auto-26', 'structure-research-27',
                                        'independent-auto-27', 'structure-research-28',
                                        'independent-auto-28', 'structure-research-29',
                                        'independent-auto-29'):
            from limit_ladder import ladder_quality_acceptable
            acceptable, actual_span, required_span = ladder_quality_acceptable(
                plan, tick.bid, tick.ask, constraints)
            if not acceptable:
                return {
                    'accepted': False,
                    'status': 'blocked_ladder_span_too_narrow',
                    'send_attempted': False,
                    'ladder_span': actual_span,
                    'minimum_ladder_span': required_span,
                }
        if now >= plan['expires_at']:
            return {'accepted': False, 'status': 'blocked_plan_expired'}
        sign = 1 if direction == 'BUY' else -1
        if sign*(tick.bid-plan['structural_invalidation']) <= 0:
            return {'accepted': False, 'status': 'blocked_structure_invalidated'}
        if sign*(tick.bid-plan['level']) <= 0:
            return {'accepted': False, 'status': 'blocked_level_reclaimed'}
        zone = plan['zone']
        # Chart zones are BID based. BUY_LIMIT triggers on ASK, so translate the
        # upper BID boundary by the observed spread. SELL_LIMIT triggers on BID.
        direct_price = plan.get('pending_price')
        raw_price = (direct_price if direct_price is not None else
                     zone['high']+spread if direction == 'BUY' else zone['low'])
        tick_size = getattr(info, 'trade_tick_size', None) or info.point
        price = round(round(raw_price/tick_size)*tick_size, info.digits)
        stop = round(float(stop), info.digits)
        target = round(float(target), info.digits)
        minimum_distance = max(info.trade_stops_level, info.trade_freeze_level)*info.point
        pending_distance = tick.ask-price if direction == 'BUY' else price-tick.bid
        if pending_distance < minimum_distance-1e-9 or pending_distance <= 0:
            return {'accepted': False, 'status': 'blocked_pending_price_too_close',
                    'pending_price': price, 'minimum_distance': minimum_distance}
        implied_bid = price-spread if direction == 'BUY' else price
        if (direct_price is None and
                not zone['low']-tick_size <= implied_bid <= zone['high']+tick_size):
            return {'accepted': False, 'status': 'blocked_pending_price_outside_zone'}
        if sign*(price-stop) <= 0 or sign*(target-price) <= 0:
            return {'accepted': False, 'status': 'blocked_price_moved',
                    'price': price, 'stop': stop, 'target': target}
        if abs(price-stop) < minimum_distance or abs(target-price) < minimum_distance:
            return {'accepted': False, 'status': 'blocked_broker_distance',
                    'minimum_distance': minimum_distance}
        rates = m.copy_rates_from_pos(symbol, m.TIMEFRAME_M5, 0,
                                      constraints['confirmation_expiry_bars']+4)
        if rates is None or len(rates) < 2 or int(rates[-1]['time'])//300 != tick.time//300:
            return {'accepted': False, 'status': 'blocked_unsynchronized_structure'}
        if any(int(bar['time']) > plan['confirmation_time'] and
               sign*(float(bar['close'])-plan['level']) <= 0 for bar in rates[:-1]):
            return {'accepted': False, 'status': 'blocked_completed_reclaim'}
        history_blocker = _execution_history_blocker(m, account, magic, constraints, now)
        if history_blocker:
            return {'accepted': False, 'status': 'blocked_' + history_blocker,
                    'send_attempted': False}
        retest_blocker = _retest_blocker(m, symbol, plan, direction)
        if retest_blocker:
            return {'accepted': False, 'status': 'blocked_' + retest_blocker,
                    'send_attempted': False}
        specified = getattr(m, 'SYMBOL_EXPIRATION_SPECIFIED', 4)
        if not getattr(info, 'expiration_mode', 0) & specified:
            return {'accepted': False, 'status': 'blocked_pending_expiration_unsupported'}
        kind = m.ORDER_TYPE_BUY if direction == 'BUY' else m.ORDER_TYPE_SELL
        pending_kind = m.ORDER_TYPE_BUY_LIMIT if direction == 'BUY' else m.ORDER_TYPE_SELL_LIMIT
        loss_per_lot = m.order_calc_profit(kind, symbol, 1.0, price, stop)
        if loss_per_lot is None or not math.isfinite(loss_per_lot) or loss_per_lot >= 0:
            raise RuntimeError('Fresh broker pending-order loss calculation is unavailable or invalid')
        risk_budget = account.balance*risk_fraction
        cash_per_lot = -loss_per_lot+cost_per_lot
        raw_volume = min(risk_budget/cash_per_lot, info.volume_max)
        volume = float((Decimal(str(raw_volume))/Decimal(str(info.volume_step))).to_integral_value(
            rounding=ROUND_FLOOR)*Decimal(str(info.volume_step)))
        if volume < info.volume_min:
            return {'accepted': False, 'status': 'blocked_minimum_volume',
                    'risk_budget': risk_budget}
        estimated_risk = cash_per_lot*volume
        margin = m.order_calc_margin(kind, symbol, volume, price)
        if (margin is None or not math.isfinite(margin) or margin < 0 or
                margin > account.margin_free*constraints['max_margin_fraction']):
            return {'accepted': False, 'status': 'blocked_margin'}
        # Scanner timestamps are normalized to UTC. MetaTrader's Python API
        # expects the terminal/broker epoch for ORDER_TIME_SPECIFIED.
        # This broker stores ORDER_TIME_SPECIFIED at one-minute precision. Send
        # the exact value we will later supervise instead of treating its
        # harmless seconds truncation as external order tampering.
        broker_expiration = int(
            (plan['expires_at']+constraints['timestamp_offset_seconds'])//60*60)
        request = {
            'action': m.TRADE_ACTION_PENDING, 'symbol': symbol, 'volume': volume,
            'type': pending_kind, 'price': price, 'sl': stop, 'tp': target,
            'magic': int(magic), 'comment': 'Tradew S:'+setup_ref,
            'type_time': m.ORDER_TIME_SPECIFIED, 'expiration': broker_expiration,
            'type_filling': m.ORDER_FILLING_RETURN,
        }
        checked = m.order_check(request)
        if checked is None:
            return {'accepted': False, 'send_attempted': False,
                    'status': 'order_check_unavailable',
                    'error': 'MT5 pending order_check returned no result: '+str(m.last_error()),
                    'pending_price': price}
        check = checked._asdict()
        check.pop('request', None)
        if checked.retcode != 0:
            return {'accepted': False, 'status': 'order_check_rejected', 'check': check,
                    'risk_budget': risk_budget, 'estimated_cash_risk': estimated_risk,
                    'pending_price': price}
        final_tick = m.symbol_info_tick(symbol)
        final_account = m.account_info()
        final_positions, final_orders = m.positions_get(), m.orders_get()
        if any(value is None for value in (final_tick, final_account, final_positions, final_orders)):
            return {'accepted': False, 'status': 'blocked_final_state_unavailable'}
        final_age = time.time()-(final_tick.time-constraints['timestamp_offset_seconds'])
        final_spread = final_tick.ask-final_tick.bid
        final_distance = final_tick.ask-price if direction == 'BUY' else price-final_tick.bid
        if not 0 <= final_age <= constraints['max_tick_age_seconds']:
            return {'accepted': False, 'status': 'blocked_stale_quote'}
        if final_spread < 0 or final_spread > constraints['max_spread']+1e-9:
            return {'accepted': False, 'status': 'blocked_spread_exceeded'}
        if final_distance < minimum_distance-1e-9 or final_distance <= 0:
            return {'accepted': False, 'status': 'blocked_pending_price_too_close',
                    'pending_price': price, 'minimum_distance': minimum_distance}
        if time.time() >= plan['expires_at']:
            return {'accepted': False, 'status': 'blocked_plan_expired'}
        if any(getattr(final_account, key) != getattr(account, key)
               for key in ('login', 'server', 'currency', 'trade_mode', 'balance')):
            return {'accepted': False, 'status': 'blocked_account_changed'}
        if margin > final_account.margin_free*constraints['max_margin_fraction']:
            return {'accepted': False, 'status': 'blocked_margin'}
        final_gold_positions = [item for item in final_positions if item.symbol in gold_names]
        final_gold_orders = [item for item in final_orders if item.symbol in gold_names]
        if (not entry_exposure_allowed(final_gold_positions, final_gold_orders, magic) or
                exposure_signature(final_gold_positions) != initial_exposure):
            return {'accepted': False, 'status': 'blocked_existing_exposure'}
        sent = m.order_send(request)
        if sent is None:
            raise RuntimeError('MT5 pending order_send returned no result: '+str(m.last_error()))
        result = sent._asdict()
        result.pop('request', None)
        accepted_codes = {m.TRADE_RETCODE_DONE, m.TRADE_RETCODE_DONE_PARTIAL,
                          m.TRADE_RETCODE_PLACED}
        accepted = sent.retcode in accepted_codes
        return {
            'accepted': accepted, 'send_attempted': True,
            'status': 'pending_submitted' if accepted else 'order_send_rejected',
            'execution_type': 'BROKER_LIMIT', 'order_type': direction+'_LIMIT',
            'direction': direction, 'symbol': symbol, 'volume': volume,
            'pending_price': price, 'stop': stop, 'target': target,
            'expiration': int(plan['expires_at']),
            'broker_expiration': broker_expiration, 'balance': account.balance,
            'risk_fraction': risk_fraction, 'risk_budget': risk_budget,
            'estimated_cash_risk': estimated_risk, 'check': check, 'result': result,
        }

    def _place_pending_ladder(self, direction, symbol, stop, target, risk_fraction,
                              cost_per_lot, magic, setup_ref, plan, constraints):
        """Preflight and submit protected limit layers under one total risk budget."""
        m = self.mt5
        if hasattr(m, 'initialize'):
            terminal_path = os.environ.get('TRADEW_MT5_TERMINAL_PATH')
            if not (m.initialize(path=terminal_path) if terminal_path else m.initialize()):
                raise RuntimeError('MT5 initialization failed before ladder submission')
        if direction not in ('BUY', 'SELL'):
            raise ValueError('direction must be BUY or SELL')
        if (not plan or plan.get('direction') != direction or plan.get('symbol') != symbol or
                plan.get('stop') != stop or (plan.get('targets') or [None])[0] != target or
                plan.get('risk_fraction') != risk_fraction or
                plan.get('cost_per_lot') != cost_per_lot):
            return {'accepted': False, 'status': 'blocked_plan_mismatch',
                    'send_attempted': False}
        raw_layers = plan.get('limit_layers')
        if not isinstance(raw_layers, list) or not 1 <= len(raw_layers) <= 5:
            return {'accepted': False, 'status': 'blocked_invalid_ladder',
                    'send_attempted': False}
        weights = [layer.get('risk_weight') for layer in raw_layers
                   if isinstance(layer, dict)]
        if (len(weights) != len(raw_layers) or
                any(isinstance(value, bool) or not isinstance(value, (int, float)) or
                    not math.isfinite(value) or value <= 0 for value in weights) or
                sum(weights) > 1+1e-9):
            return {'accepted': False, 'status': 'blocked_invalid_ladder',
                    'send_attempted': False}

        account, info, tick = m.account_info(), m.symbol_info(symbol), m.symbol_info_tick(symbol)
        positions, orders, symbols = m.positions_get(), m.orders_get(), m.symbols_get()
        terminal = m.terminal_info()
        if any(value is None for value in (account, info, tick, positions, orders,
                                           symbols, terminal)):
            raise RuntimeError('Fresh account, symbol, quote, and exposure are required for ladder submission')
        mode = {m.ACCOUNT_TRADE_MODE_DEMO: 'demo',
                m.ACCOUNT_TRADE_MODE_REAL: 'live'}.get(account.trade_mode)
        actual = dict(login=account.login, server=account.server,
                      currency=account.currency, mode=mode)
        expected = {key: constraints['expected_'+key] for key in actual}
        if actual != expected or plan.get('account') != expected:
            return {'accepted': False, 'status': 'blocked_account_changed'}
        if (not terminal.connected or not terminal.trade_allowed or
                terminal.tradeapi_disabled or not account.trade_allowed):
            return {'accepted': False, 'status': 'blocked_algo_trading'}
        gold_names = {item.name for item in symbols if is_gold_symbol(item)} | {symbol}
        gold_positions = [item for item in positions if item.symbol in gold_names]
        gold_orders = [item for item in orders if item.symbol in gold_names]
        if not entry_exposure_allowed(gold_positions, gold_orders, magic):
            return {'accepted': False, 'status': 'blocked_existing_exposure'}
        initial_exposure = exposure_signature(gold_positions)
        now = time.time()
        history_blocker = _execution_history_blocker(m, account, magic, constraints, now)
        if history_blocker:
            return {'accepted': False, 'status': 'blocked_' + history_blocker,
                    'send_attempted': False}
        age = now-(tick.time-constraints['timestamp_offset_seconds'])
        if age < 0 or age > constraints['max_tick_age_seconds']:
            return {'accepted': False, 'status': 'blocked_stale_quote'}
        spread = tick.ask-tick.bid
        if spread < 0 or spread > constraints['max_spread']+1e-9:
            return {'accepted': False, 'status': 'blocked_spread_exceeded'}
        if plan.get('rule_version') in ('structure-research-19',
                                        'independent-auto-19',
                                        'structure-research-20',
                                        'independent-auto-20',
                                        'structure-research-21',
                                        'independent-auto-21',
                                        'structure-research-22',
                                        'independent-auto-22',
                                        'structure-research-23',
                                        'independent-auto-23',
                                        'structure-research-24',
                                        'independent-auto-24',
                                        'structure-research-25',
                                        'structure-research-26',
                                        'independent-auto-25',
                                        'independent-auto-26', 'structure-research-27',
                                        'independent-auto-27', 'structure-research-28',
                                        'independent-auto-28', 'structure-research-29',
                                        'independent-auto-29'):
            from limit_ladder import ladder_quality_acceptable
            acceptable, actual_span, required_span = ladder_quality_acceptable(
                plan, tick.bid, tick.ask, constraints)
            if not acceptable:
                return {
                    'accepted': False,
                    'status': 'blocked_ladder_span_too_narrow',
                    'send_attempted': False,
                    'ladder_span': actual_span,
                    'minimum_ladder_span': required_span,
                }
        if now >= plan['expires_at']:
            return {'accepted': False, 'status': 'blocked_plan_expired'}
        sign = 1 if direction == 'BUY' else -1
        if sign*(tick.bid-plan['structural_invalidation']) <= 0:
            return {'accepted': False, 'status': 'blocked_structure_invalidated'}
        if sign*(tick.bid-plan['level']) <= 0:
            return {'accepted': False, 'status': 'blocked_level_reclaimed'}
        rates = m.copy_rates_from_pos(
            symbol, m.TIMEFRAME_M5, 0, constraints['confirmation_expiry_bars']+4)
        if rates is None or len(rates) < 2 or int(rates[-1]['time'])//300 != tick.time//300:
            return {'accepted': False, 'status': 'blocked_unsynchronized_structure'}
        if any(int(bar['time']) > plan['confirmation_time'] and
               sign*(float(bar['close'])-plan['level']) <= 0 for bar in rates[:-1]):
            return {'accepted': False, 'status': 'blocked_completed_reclaim'}
        retest_blocker = _retest_blocker(m, symbol, plan, direction)
        if retest_blocker:
            return {'accepted': False, 'status': 'blocked_' + retest_blocker,
                    'send_attempted': False}
        specified = getattr(m, 'SYMBOL_EXPIRATION_SPECIFIED', 4)
        if not getattr(info, 'expiration_mode', 0) & specified:
            return {'accepted': False, 'status': 'blocked_pending_expiration_unsupported'}

        tick_size = getattr(info, 'trade_tick_size', None) or info.point
        minimum_distance = max(info.trade_stops_level, info.trade_freeze_level)*info.point
        protected_stop_distance = max(
            minimum_distance, spread*2,
            float(plan.get('m5_atr') or 0)*constraints['minimum_layer_stop_atr_fraction'])
        from independent_validation import broker_independent_blocker
        independent_blocker = broker_independent_blocker(m, symbol, plan, tick, constraints)
        if independent_blocker:
            return {'accepted': False, 'status': 'blocked_'+independent_blocker, 'send_attempted': False}
        if plan.get('rule_version') in ('independent-auto-27', 'independent-auto-28',
                                        'independent-auto-29'):
            protected_stop_distance = max(protected_stop_distance, float(plan['m5_atr'])*.5)
        digits = info.digits
        stop = round(float(stop), digits)
        if (plan.get('rule_version') in ('independent-auto-27', 'independent-auto-28',
                                         'independent-auto-29') and
                sign*(plan['structural_invalidation']-stop) < -1e-9):
            return {'accepted': False, 'status': 'blocked_rounded_stop_inside_invalidation',
                    'send_attempted': False}
        kind = m.ORDER_TYPE_BUY if direction == 'BUY' else m.ORDER_TYPE_SELL
        pending_kind = (m.ORDER_TYPE_BUY_LIMIT if direction == 'BUY'
                        else m.ORDER_TYPE_SELL_LIMIT)
        broker_expiration = int(
            (plan['expires_at']+constraints['timestamp_offset_seconds'])//60*60)
        risk_budget = account.balance*risk_fraction
        runner_enabled = bool(constraints.get('runner_enabled'))
        hedging_mode = getattr(m, 'ACCOUNT_MARGIN_MODE_RETAIL_HEDGING', 2)
        if (runner_enabled and
                getattr(account, 'margin_mode', hedging_mode) != hedging_mode):
            return {'accepted': False, 'status': 'blocked_runner_requires_hedging',
                    'send_attempted': False}
        layer_checks = []
        candidates = []
        seen_prices = set()
        total_margin = 0.0
        reference_volume = None
        for position, layer in enumerate(raw_layers, 1):
            price_value, target_value = layer.get('price'), layer.get('target')
            if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and
                       math.isfinite(value) and value > 0
                       for value in (price_value, target_value)):
                layer_checks.append({'index': position, 'status': 'invalid_price'})
                continue
            price = round(round(float(price_value)/tick_size)*tick_size, digits)
            layer_target = round(float(target_value), digits)
            if price in seen_prices:
                layer_checks.append({'index': position, 'status': 'duplicate_rounded_price',
                                     'price': price})
                continue
            seen_prices.add(price)
            pending_distance = tick.ask-price if direction == 'BUY' else price-tick.bid
            if pending_distance < minimum_distance-1e-9 or pending_distance <= 0:
                layer_checks.append({'index': position, 'status': 'price_already_reached',
                                     'price': price})
                continue
            if (sign*(price-stop) <= 0 or sign*(layer_target-price) <= 0 or
                    abs(price-stop) < protected_stop_distance or
                    abs(layer_target-price) < minimum_distance):
                layer_checks.append({'index': position, 'status': 'invalid_geometry',
                                     'price': price, 'target': layer_target,
                                     'required_stop_distance': protected_stop_distance})
                continue
            loss_per_lot = m.order_calc_profit(kind, symbol, 1.0, price, stop)
            if loss_per_lot is None or not math.isfinite(loss_per_lot) or loss_per_lot >= 0:
                layer_checks.append({'index': position, 'status': 'risk_unavailable',
                                     'price': price})
                continue
            layer_budget = risk_budget*float(layer['risk_weight'])
            cash_per_lot = -loss_per_lot+cost_per_lot
            raw_volume = min(layer_budget/cash_per_lot, info.volume_max)
            volume = float((Decimal(str(raw_volume))/Decimal(str(info.volume_step))).to_integral_value(
                rounding=ROUND_FLOOR)*Decimal(str(info.volume_step)))
            if reference_volume is not None:
                volume_cap = reference_volume*constraints['max_ladder_volume_ratio']
                volume = float((Decimal(str(min(volume, volume_cap)))/
                                Decimal(str(info.volume_step))).to_integral_value(
                    rounding=ROUND_FLOOR)*Decimal(str(info.volume_step)))
            if volume < info.volume_min:
                layer_checks.append({'index': position, 'status': 'below_minimum_volume',
                                     'price': price, 'risk_budget': layer_budget})
                continue
            estimated_risk = cash_per_lot*volume
            reward_per_lot = m.order_calc_profit(kind, symbol, 1.0, price, layer_target)
            estimated_reward = ((reward_per_lot-cost_per_lot)*volume
                                if reward_per_lot is not None and math.isfinite(reward_per_lot)
                                else None)
            if estimated_reward is None or estimated_reward <= 0:
                layer_checks.append({'index': position, 'status': 'reward_unavailable',
                                     'price': price})
                continue
            # Every possible filled subset must meet the same payoff floor;
            # unfilled deep orders cannot subsidize an inferior near entry.
            banked_reward = tp1_payoff(reward_per_lot, cost_per_lot, volume,
                                      info.volume_min, info.volume_step, constraints)
            if banked_reward/estimated_risk < constraints['minimum_ladder_reward_risk']:
                layer_checks.append({'index': position, 'status': 'layer_reward_risk',
                                     'price': price, 'reward_risk': banked_reward/estimated_risk,
                                     'reward_basis': 'TP1 portion only; runner upside excluded'})
                continue
            margin = m.order_calc_margin(kind, symbol, volume, price)
            if margin is None or not math.isfinite(margin) or margin < 0:
                layer_checks.append({'index': position, 'status': 'margin_unavailable',
                                     'price': price})
                continue
            take_volume, runner_volume = (
                split_exit_volume(volume, info.volume_min, info.volume_step,
                                  constraints['take_profit_close_fraction'])
                if runner_enabled else (volume, 0.0))
            child_specs = [(('take_profit' if runner_enabled else 'legacy'),
                            take_volume, layer_target, ('T' if runner_enabled else ''))]
            if runner_volume >= info.volume_min:
                child_specs.append(('runner', runner_volume, 0.0, 'R'))
            layer_candidates = []
            for role, child_volume, child_target, suffix in child_specs:
                request = {
                    'action': m.TRADE_ACTION_PENDING, 'symbol': symbol,
                    'volume': child_volume, 'type': pending_kind, 'price': price,
                    'sl': stop, 'tp': child_target, 'magic': int(magic),
                    'comment': f'Tradew S:{setup_ref} L{position}{suffix}',
                    'type_time': m.ORDER_TIME_SPECIFIED, 'expiration': broker_expiration,
                    'type_filling': m.ORDER_FILLING_RETURN,
                }
                checked = m.order_check(request)
                if checked is None:
                    layer_checks.append({'index': position, 'role': role,
                                         'status': 'order_check_unavailable',
                                         'price': price})
                    layer_candidates = []
                    break
                check = checked._asdict()
                check.pop('request', None)
                if checked.retcode != 0:
                    layer_checks.append({'index': position, 'role': role,
                                         'status': 'order_check_rejected',
                                         'price': price, 'check': check})
                    layer_candidates = []
                    break
                child_margin = m.order_calc_margin(kind, symbol, child_volume, price)
                if child_margin is None or not math.isfinite(child_margin) or child_margin < 0:
                    layer_checks.append({'index': position, 'role': role,
                                         'status': 'margin_unavailable', 'price': price})
                    layer_candidates = []
                    break
                layer_candidates.append({
                    'index': position, 'role': role,
                    'quality': layer.get('quality'), 'request': request,
                    'pending_price': price, 'stop': stop, 'target': child_target,
                    'planned_target': layer_target, 'volume': child_volume,
                    'risk_weight': float(layer['risk_weight']),
                    'risk_budget': layer_budget,
                    'estimated_cash_risk': cash_per_lot*child_volume,
                    'estimated_cash_reward': (reward_per_lot-cost_per_lot)*child_volume,
                    'tp1_cash_reward': ((reward_per_lot if role != 'runner' else 0)-cost_per_lot)*child_volume,
                    'margin': child_margin, 'check': check,
                })
            if not layer_candidates:
                continue
            total_margin += sum(item['margin'] for item in layer_candidates)
            if reference_volume is None:
                reference_volume = volume
            candidates.extend(layer_candidates)
        if not candidates:
            return {'accepted': False, 'send_attempted': False,
                    'status': ('blocked_layer_reward_risk' if any(
                        x['status'] == 'layer_reward_risk' for x in layer_checks)
                        else 'blocked_no_valid_ladder_layers'),
                    'risk_budget': risk_budget, 'layer_checks': layer_checks}
        planned_reward_risk = (sum(item['estimated_cash_reward'] for item in candidates) /
                               sum(item['estimated_cash_risk'] for item in candidates))
        if planned_reward_risk < constraints['minimum_ladder_reward_risk']:
            return {'accepted': False, 'send_attempted': False,
                    'status': 'blocked_reward_risk',
                    'reward_risk': planned_reward_risk,
                    'minimum_reward_risk': constraints['minimum_ladder_reward_risk'],
                    'layer_checks': layer_checks}
        if total_margin > account.margin_free*constraints['max_margin_fraction']:
            return {'accepted': False, 'send_attempted': False,
                    'status': 'blocked_margin', 'required_margin': total_margin,
                    'layer_checks': layer_checks}

        final_tick, final_account = m.symbol_info_tick(symbol), m.account_info()
        final_positions, final_orders = m.positions_get(), m.orders_get()
        if any(value is None for value in
               (final_tick, final_account, final_positions, final_orders)):
            return {'accepted': False, 'status': 'blocked_final_state_unavailable'}
        final_age = time.time()-(final_tick.time-constraints['timestamp_offset_seconds'])
        final_spread = final_tick.ask-final_tick.bid
        if not 0 <= final_age <= constraints['max_tick_age_seconds']:
            return {'accepted': False, 'status': 'blocked_stale_quote'}
        if final_spread < 0 or final_spread > constraints['max_spread']+1e-9:
            return {'accepted': False, 'status': 'blocked_spread_exceeded'}
        if time.time() >= plan['expires_at']:
            return {'accepted': False, 'status': 'blocked_plan_expired'}
        if (sign*(final_tick.bid-plan['structural_invalidation']) <= 0 or
                sign*(final_tick.bid-plan['level']) <= 0):
            return {'accepted': False, 'status': 'blocked_structure_invalidated'}
        if any(getattr(final_account, key) != getattr(account, key)
               for key in ('login', 'server', 'currency', 'trade_mode', 'balance')):
            return {'accepted': False, 'status': 'blocked_account_changed'}
        final_gold_positions = [item for item in final_positions if item.symbol in gold_names]
        final_gold_orders = [item for item in final_orders if item.symbol in gold_names]
        if (not entry_exposure_allowed(final_gold_positions, final_gold_orders, magic) or
                exposure_signature(final_gold_positions) != initial_exposure):
            return {'accepted': False, 'status': 'blocked_existing_exposure'}
        independent_blocker = broker_independent_blocker(m, symbol, plan, final_tick, constraints)
        if independent_blocker:
            return {'accepted': False, 'status': 'blocked_'+independent_blocker, 'send_attempted': False}
        final_candidates = []
        for item in candidates:
            price = item['pending_price']
            distance = final_tick.ask-price if direction == 'BUY' else price-final_tick.bid
            if distance < minimum_distance-1e-9 or distance <= 0:
                layer_checks.append({'index': item['index'],
                                     'status': 'price_reached_before_send', 'price': price})
            else:
                final_candidates.append(item)
        if not final_candidates:
            return {'accepted': False, 'send_attempted': False,
                    'status': 'blocked_no_valid_ladder_layers',
                    'risk_budget': risk_budget, 'layer_checks': layer_checks}

        accepted_codes = {m.TRADE_RETCODE_DONE, m.TRADE_RETCODE_DONE_PARTIAL,
                          m.TRADE_RETCODE_PLACED}
        submitted, rejected = [], []
        for item in final_candidates:
            sent = m.order_send(item['request'])
            if sent is None:
                rejected.append({'index': item['index'], 'role': item.get('role'),
                                 'status': 'order_send_unavailable',
                                 'error': str(m.last_error()) if hasattr(m, 'last_error') else ''})
                if item.get('role') == 'take_profit':
                    break
                continue
            response = sent._asdict()
            response.pop('request', None)
            record = {key: value for key, value in item.items() if key != 'request'}
            record['result'] = response
            if sent.retcode in accepted_codes:
                submitted.append(record)
            else:
                rejected.append({**record, 'status': 'order_send_rejected'})
                if item.get('role') == 'take_profit':
                    break
        if not submitted:
            return {'accepted': False, 'send_attempted': True,
                    'status': 'order_send_rejected', 'orders': [],
                    'rejected_layers': rejected, 'layer_checks': layer_checks}
        submitted_layers = {item['index'] for item in submitted}
        complete = (len(submitted) == len(final_candidates) and
                    len(submitted_layers) == len(raw_layers))
        first = submitted[0]
        return {
            'accepted': True, 'send_attempted': True,
            'status': ('pending_ladder_submitted' if complete
                       else 'pending_ladder_partial'),
            'execution_type': ('BROKER_LIMIT_LADDER_TP_RUNNER'
                               if runner_enabled else 'BROKER_LIMIT_LADDER'),
            'order_type': direction+'_LIMIT', 'direction': direction,
            'symbol': symbol, 'layers_requested': len(raw_layers),
            'layers_submitted': len(submitted_layers),
            'orders_requested': len(final_candidates),
            'orders_submitted': len(submitted), 'orders': submitted,
            'rejected_layers': rejected, 'layer_checks': layer_checks,
            'volume': sum(item['volume'] for item in submitted),
            'total_volume': sum(item['volume'] for item in submitted),
            'pending_price': first['pending_price'], 'stop': stop,
            'target': first.get('planned_target', first['target']),
            'take_profit_close_fraction': constraints.get('take_profit_close_fraction'),
            'runner_enabled': any(item.get('role') == 'runner' for item in submitted),
            'expiration': int(plan['expires_at']),
            'broker_expiration': broker_expiration, 'balance': account.balance,
            'risk_fraction': risk_fraction, 'risk_budget': risk_budget,
            'estimated_cash_risk': sum(item['estimated_cash_risk'] for item in submitted),
            'estimated_cash_reward': sum(item['estimated_cash_reward'] for item in submitted),
            'tp1_cash_reward': sum(item['tp1_cash_reward'] for item in submitted),
            'tp1_reward_risk': (sum(item['tp1_cash_reward'] for item in submitted) /
                                sum(item['estimated_cash_risk'] for item in submitted)),
            'reward_basis': 'reward_risk assumes all volume reaches planned target; tp1_reward_risk excludes runner upside',
            'reward_risk': (sum(item['estimated_cash_reward'] for item in submitted) /
                            sum(item['estimated_cash_risk'] for item in submitted)),
            'result': first['result'],
        }

    def revalidate_pending_limit(self, ticket, symbol, magic, setup_ref, plan,
                                 expected, constraints):
        """Revalidate one owned pending order and remove it if its plan is invalid."""
        m = self.mt5
        if hasattr(m, 'initialize'):
            terminal_path = os.environ.get('TRADEW_MT5_TERMINAL_PATH')
            if not (m.initialize(path=terminal_path) if terminal_path else m.initialize()):
                raise RuntimeError('MT5 initialization failed before pending supervision')
        if (not isinstance(ticket, int) or isinstance(ticket, bool) or ticket <= 0 or
                not isinstance(setup_ref, str) or len(setup_ref) != 10):
            raise ValueError('Valid pending ticket and setup_ref are required')
        direction = plan.get('direction')
        if direction not in ('BUY', 'SELL') or plan.get('symbol') != symbol:
            return {'accepted': False, 'status': 'blocked_plan_mismatch', 'ticket': ticket}
        account, terminal = m.account_info(), m.terminal_info()
        orders, positions, symbols = m.orders_get(), m.positions_get(), m.symbols_get()
        if any(value is None for value in (account, terminal, orders, positions, symbols)):
            return {'accepted': False, 'status': 'supervision_state_unavailable',
                    'ticket': ticket}
        mode = {m.ACCOUNT_TRADE_MODE_DEMO: 'demo',
                m.ACCOUNT_TRADE_MODE_REAL: 'live'}.get(account.trade_mode)
        actual_account = dict(login=account.login, server=account.server,
                              currency=account.currency, mode=mode)
        configured_account = {key: constraints['expected_'+key] for key in actual_account}
        # Never mutate a ticket while connected to a different account.
        if actual_account != configured_account or plan.get('account') != configured_account:
            return {'accepted': False, 'status': 'blocked_account_changed', 'ticket': ticket}
        order = next((item for item in orders if int(item.ticket) == ticket), None)
        if order is None:
            return {'accepted': True, 'status': 'pending_already_absent', 'ticket': ticket}
        expected_type = (m.ORDER_TYPE_BUY_LIMIT if direction == 'BUY'
                         else m.ORDER_TYPE_SELL_LIMIT)
        comment = str(getattr(order, 'comment', '') or '')
        if (order.symbol != symbol or int(order.magic) != int(magic) or
                int(order.type) != expected_type or ('S:'+setup_ref) not in comment):
            return {'accepted': False, 'status': 'blocked_order_identity_mismatch',
                    'ticket': ticket}
        info, tick = m.symbol_info(symbol), m.symbol_info_tick(symbol)
        if info is None or tick is None:
            return {'accepted': False, 'status': 'supervision_quote_unavailable',
                    'ticket': ticket}
        now = time.time()
        age = now-(tick.time-constraints['timestamp_offset_seconds'])
        if not 0 <= age <= constraints['max_tick_age_seconds']:
            return {'accepted': False, 'status': 'supervision_deferred_stale_quote',
                    'ticket': ticket}
        if (not terminal.connected or not terminal.trade_allowed or
                terminal.tradeapi_disabled or not account.trade_allowed):
            return {'accepted': False, 'status': 'blocked_algo_trading', 'ticket': ticket}

        price_tolerance = (getattr(info, 'trade_tick_size', None) or info.point)/2+1e-9
        volume_tolerance = info.volume_step/2+1e-9
        actual_terms = {
            'pending_price': float(order.price_open), 'stop': float(order.sl),
            'target': float(order.tp),
            'volume': float(getattr(order, 'volume_current',
                                    getattr(order, 'volume_initial', 0))),
        }
        terms_changed = any((abs(actual_terms[key]-float(expected[key])) >
                             (volume_tolerance if key == 'volume' else price_tolerance))
                            for key in actual_terms if expected.get(key) is not None)
        if (int(getattr(order, 'type_time', m.ORDER_TIME_SPECIFIED)) !=
                m.ORDER_TIME_SPECIFIED):
            terms_changed = True
        if (expected.get('broker_expiration') is not None and
                int(getattr(order, 'time_expiration', 0)) !=
                int(expected['broker_expiration'])):
            terms_changed = True
        reason = 'order_terms_changed' if terms_changed else None
        sign = 1 if direction == 'BUY' else -1
        if reason is None and now >= float(plan['expires_at']):
            reason = 'plan_expired'
        if (reason is None and
                sign*(float(tick.bid)-float(plan['structural_invalidation'])) <= 0):
            reason = 'structure_invalidated'

        rates = m.copy_rates_from_pos(
            symbol, m.TIMEFRAME_M5, 0, constraints['confirmation_expiry_bars']+6)
        if reason is None:
            if (rates is None or len(rates) < 2 or
                    int(rates[-1]['time'])//300 != int(tick.time)//300):
                return {'accepted': False, 'status': 'supervision_deferred_unsynchronized_structure',
                        'ticket': ticket}
            if any(int(bar['time']) > int(plan['confirmation_time']) and
                   sign*(float(bar['close'])-float(plan['level'])) <= 0
                   for bar in rates[:-1]):
                reason = 'completed_m5_reclaim'

        gold_names = {item.name for item in symbols if is_gold_symbol(item)} | {symbol}
        def same_setup(item):
            return (int(getattr(item, 'magic', 0)) == int(magic) and
                    ('S:'+setup_ref) in str(getattr(item, 'comment', '') or ''))
        other_gold_orders = [item for item in orders
                             if int(item.ticket) != ticket and item.symbol in gold_names and
                             not same_setup(item)]
        # A filled sibling must not cancel the remaining protected ladder.
        gold_positions = [item for item in positions if item.symbol in gold_names and
                          int(getattr(item, 'magic', 0)) != int(magic)]
        if reason is None and (other_gold_orders or gold_positions):
            reason = 'conflicting_gold_exposure'
        if reason is None:
            return {'accepted': True, 'status': 'pending_still_valid', 'ticket': ticket,
                    'setup_ref': setup_ref, 'checked_at': now}

        # Re-fetch and re-identify immediately before removal to avoid cancelling
        # a replacement order that appeared during validation.
        final_orders = m.orders_get()
        if final_orders is None:
            return {'accepted': False, 'status': 'supervision_state_unavailable',
                    'ticket': ticket, 'invalidation_reason': reason}
        final = next((item for item in final_orders if int(item.ticket) == ticket), None)
        if final is None:
            return {'accepted': True, 'status': 'pending_already_absent',
                    'ticket': ticket, 'invalidation_reason': reason}
        final_comment = str(getattr(final, 'comment', '') or '')
        if (final.symbol != symbol or int(final.magic) != int(magic) or
                int(final.type) != expected_type or ('S:'+setup_ref) not in final_comment):
            return {'accepted': False, 'status': 'blocked_order_identity_mismatch',
                    'ticket': ticket, 'invalidation_reason': reason}
        request = {'action': m.TRADE_ACTION_REMOVE, 'order': ticket,
                   'magic': int(magic), 'comment': 'Tradew cancel '+setup_ref}
        sent = m.order_send(request)
        if sent is None:
            raise RuntimeError('MT5 pending cancellation returned no result: '+str(m.last_error()))
        result = sent._asdict()
        result.pop('request', None)
        accepted = sent.retcode in {m.TRADE_RETCODE_DONE, m.TRADE_RETCODE_DONE_PARTIAL,
                                    m.TRADE_RETCODE_PLACED}
        return {'accepted': accepted,
                'status': 'pending_cancelled' if accepted else 'pending_cancel_rejected',
                'ticket': ticket, 'setup_ref': setup_ref,
                'invalidation_reason': reason, 'result': result}

    def modify_position_stop(self, ticket, symbol, stop, magic):
        """Move an existing position stop only in the risk-reducing direction."""
        m = self.mt5
        positions = m.positions_get(symbol=symbol)
        info, tick = m.symbol_info(symbol), m.symbol_info_tick(symbol)
        if positions is None or info is None or tick is None:
            raise RuntimeError('Fresh position, symbol, and quote are required for trailing')
        matches = [position for position in positions if int(position.ticket) == int(ticket)]
        if len(matches) != 1:
            return {'accepted': False, 'status': 'position_not_found'}
        position = matches[0]
        sign = 1 if position.type == m.ORDER_TYPE_BUY else -1
        stop = round(float(stop), info.digits)
        current = tick.bid if sign == 1 else tick.ask
        if sign * (current-stop) <= 0:
            return {'accepted': False, 'status': 'stop_crossed_market'}
        if position.sl > 0 and sign * (stop-position.sl) <= 0:
            return {'accepted': False, 'status': 'stop_does_not_improve'}
        minimum_distance = max(info.trade_stops_level, info.trade_freeze_level) * info.point
        if abs(current-stop) < minimum_distance:
            return {'accepted': False, 'status': 'blocked_broker_distance',
                    'minimum_distance': minimum_distance}
        request = {
            'action': m.TRADE_ACTION_SLTP, 'symbol': symbol,
            'position': int(ticket), 'sl': stop, 'tp': position.tp,
            'magic': int(magic), 'comment': 'Tradew auto trail',
        }
        checked = m.order_check(request)
        if checked is None:
            raise RuntimeError('MT5 trailing order_check returned no result: ' + str(m.last_error()))
        check = checked._asdict()
        check.pop('request', None)
        if checked.retcode != 0:
            return {'accepted': False, 'status': 'order_check_rejected', 'check': check}
        sent = m.order_send(request)
        if sent is None:
            raise RuntimeError('MT5 trailing order_send returned no result: ' + str(m.last_error()))
        result = sent._asdict()
        result.pop('request', None)
        accepted = sent.retcode in {m.TRADE_RETCODE_DONE, m.TRADE_RETCODE_DONE_PARTIAL,
                                    m.TRADE_RETCODE_PLACED}
        return {'accepted': accepted,
                'status': 'stop_modified' if accepted else 'order_send_rejected',
                'ticket': int(ticket), 'symbol': symbol, 'previous_stop': position.sl,
                'stop': stop, 'tp': position.tp, 'check': check, 'result': result}

    def snapshot(self, config, m5_count=200):
        from scan_engine import TIMEFRAMES
        m = self.mt5
        s = {'snapshot_id': str(uuid.uuid4()), 'retrieval_start_utc': time.time(),
             'bars': {}, 'errors': [], 'positions': None, 'pending_orders': None,
             'specifications': {}, 'gold_ticks': {}, 'news_status': 'unavailable'}
        try:
            terminal_path = os.environ.get('TRADEW_MT5_TERMINAL_PATH')
            initialized = m.initialize(path=terminal_path) if terminal_path else m.initialize()
            if not initialized:
                raise RuntimeError('MT5 initialization failed')
            account, terminal, symbols, positions, orders = (
                m.account_info(), m.terminal_info(), m.symbols_get(), m.positions_get(), m.orders_get())
            if any(x is None for x in (account, terminal, symbols, positions, orders)):
                raise RuntimeError('MT5 returned null account, terminal, symbols, or exposure')
            s['account'], s['terminal'] = account._asdict(), terminal._asdict()
            s['discovered_symbol_count'] = len(symbols)
            s['account_mode'] = {m.ACCOUNT_TRADE_MODE_DEMO: 'demo', m.ACCOUNT_TRADE_MODE_REAL: 'live'}.get(account.trade_mode, 'contest')
            # Aggregate every gold instrument for exposure controls. Select the
            # analysis instrument separately from verified gold-vs-USD symbols.
            gold = [x for x in symbols if is_gold_symbol(x)]
            names = {x.name for x in gold}
            gold_usd = [x for x in gold if is_gold_usd_symbol(x)]
            trade_names = {x.name for x in gold_usd}
            s['positions'] = [p._asdict() for p in positions if p.symbol in names]
            s['pending_orders'] = [o._asdict() for o in orders if o.symbol in names]
            chosen = config.get('symbol')
            if chosen is None and len(trade_names) == 1:
                chosen = next(iter(trade_names))
            if chosen not in trade_names:
                raise RuntimeError('Gold/USD symbol ambiguous or unverified; configure an exact discovered symbol')
            s['symbol'] = chosen
            s['discovered_gold_symbols'] = sorted(names)
            for symbol in gold:
                s['specifications'][symbol.name] = symbol._asdict()
                quote = m.symbol_info_tick(symbol.name)
                if quote is not None:
                    s['gold_ticks'][symbol.name] = quote._asdict()
            initial = m.symbol_info_tick(chosen)
            if initial is None:
                raise RuntimeError('Missing selected symbol quote')
            s['initial_tick'] = initial._asdict()
            for tf in TIMEFRAMES:
                count = (math.ceil(m5_count*300/TIMEFRAMES[tf])+200
                         if m5_count > 200 else 200)
                rates = m.copy_rates_from_pos(chosen, getattr(m, 'TIMEFRAME_' + tf), 0, count)
                if rates is None:
                    raise RuntimeError('History unavailable: ' + tf)
                s['bars'][tf] = [{name: row[name].item() for name in rates.dtype.names} for row in rates]
            final = m.symbol_info_tick(chosen)
            if final is None:
                raise RuntimeError('Final quote unavailable')
            s['tick'] = final._asdict()
            ticks = m.copy_ticks_from(chosen, datetime.now(timezone.utc)-timedelta(seconds=60), 1000, m.COPY_TICKS_ALL)
            s['recent_ticks'] = [] if ticks is None else [
                {name: row[name].item() for name in ticks.dtype.names} for row in ticks]
            p2, o2 = m.positions_get(), m.orders_get()
            # Price/profit fluctuations are expected; compare exposure and protection only.
            def signature(items):
                return sorted((p.ticket, p.symbol, p.type, p.volume, p.sl, p.tp) for p in items if p.symbol in names)
            if p2 is None or o2 is None:
                s['errors'].append('Exposure recheck failed')
            elif (signature(p2) != signature(positions) or
                  pending_exposure_signature(o2, names) !=
                  pending_exposure_signature(orders, names)):
                s['errors'].append('Exposure changed during collection; refresh required')
            for p in s['positions']:
                if p['symbol'] not in s['gold_ticks']:
                    s['errors'].append('Missing exposure quote: ' + p['symbol'])
            broker_now = datetime.now(timezone.utc)+timedelta(seconds=config['timestamp_offset_seconds'])
            deals = m.history_deals_get(broker_now-timedelta(days=90), broker_now+timedelta(seconds=60))
            if deals is None:
                s['recent_deals'] = None
                s['errors'].append('Deal history unavailable; outcome reconciliation incomplete')
            else:
                entry_names = {
                    m.DEAL_ENTRY_IN: 'IN', m.DEAL_ENTRY_OUT: 'OUT',
                    m.DEAL_ENTRY_INOUT: 'INOUT', m.DEAL_ENTRY_OUT_BY: 'OUT_BY'}
                s['recent_deals'] = []
                for deal in deals:
                    if deal.symbol in names:
                        item = deal._asdict()
                        item['entry_role'] = entry_names.get(deal.entry, 'UNKNOWN')
                        s['recent_deals'].append(item)
        except Exception as exc:
            s['errors'].append(str(exc))
        finally:
            s['retrieval_end_utc'] = time.time()
        return s

    def close(self):
        self.mt5.shutdown()


class RemoteMT5Adapter:
    """HTTP client for the private Wine/Windows MT5 bridge."""

    def __init__(self, base_url=None, token=None, timeout=180, opener=urlopen):
        self.base_url = (base_url or os.environ.get('TRADEW_MT5_BRIDGE_URL', '')).rstrip('/')
        self.token = token or os.environ.get('TRADEW_BRIDGE_TOKEN', '')
        self.timeout = timeout
        self.opener = opener
        if not self.base_url or not self.token:
            raise RuntimeError('MT5 bridge URL and token are required')

    def _post(self, path, payload):
        request = Request(
            self.base_url + path,
            data=json.dumps(payload, allow_nan=False).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'X-Tradew-Bridge-Token': self.token,
            },
            method='POST',
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode('utf-8'))
        except HTTPError as exc:
            detail = exc.read().decode('utf-8', errors='replace')
            raise RuntimeError(f'MT5 bridge HTTP {exc.code}: {detail[-300:]}') from exc
        except (URLError, OSError, ValueError) as exc:
            raise RuntimeError('MT5 bridge unavailable: ' + str(exc)) from exc
        if isinstance(result, dict) and result.get('error'):
            raise RuntimeError('MT5 bridge error: ' + str(result['error']))
        return result

    def snapshot(self, config, m5_count=200):
        return self._post('/snapshot', {'config': config, 'm5_count': m5_count})

    def profit(self, direction, symbol, volume, entry, stop):
        return self._post('/profit', {
            'direction': direction, 'symbol': symbol, 'volume': volume,
            'entry': entry, 'stop': stop,
        }).get('value')

    def margin(self, direction, symbol, volume, entry):
        return self._post('/margin', {
            'direction': direction, 'symbol': symbol, 'volume': volume,
            'entry': entry,
        }).get('value')

    def preview_half_close(self, symbol, constraints):
        return self._post('/positions/close-half/preview', {
            'symbol': symbol, 'constraints': constraints,
        })

    def close_half_positions(self, symbol, snapshot_token, constraints):
        return self._post('/positions/close-half', {
            'symbol': symbol, 'snapshot_token': snapshot_token,
            'constraints': constraints,
        })

    def execute_market_order(self, direction, symbol, stop, target, risk_fraction,
                             cost_per_lot, deviation_points, magic, setup_ref, plan, constraints):
        return self._post('/execute', {
            'direction': direction, 'symbol': symbol, 'stop': stop, 'target': target,
            'risk_fraction': risk_fraction, 'cost_per_lot': cost_per_lot,
            'deviation_points': deviation_points, 'magic': magic,
            'setup_ref': setup_ref,
            'plan': plan, 'constraints': constraints,
        })

    def place_pending_limit(self, direction, symbol, stop, target, risk_fraction,
                            cost_per_lot, magic, setup_ref, plan, constraints):
        return self._post('/pending', {
            'direction': direction, 'symbol': symbol, 'stop': stop, 'target': target,
            'risk_fraction': risk_fraction, 'cost_per_lot': cost_per_lot,
            'magic': magic, 'setup_ref': setup_ref,
            'plan': plan, 'constraints': constraints,
        })

    def modify_position_stop(self, ticket, symbol, stop, magic):
        return self._post('/position/stop', {
            'ticket': ticket, 'symbol': symbol, 'stop': stop, 'magic': magic,
        })

    def revalidate_pending_limit(self, ticket, symbol, magic, setup_ref, plan,
                                 expected, constraints):
        return self._post('/pending/revalidate', {
            'ticket': ticket, 'symbol': symbol, 'magic': magic,
            'setup_ref': setup_ref, 'plan': plan, 'expected': expected,
            'constraints': constraints,
        })

    def close(self):
        # Closing this lightweight client must not shut down the shared bridge.
        return None


def create_mt5_adapter():
    """Select the local module or private bridge from deployment environment."""
    if os.environ.get('TRADEW_MT5_BRIDGE_URL'):
        return RemoteMT5Adapter()
    return MT5Adapter()
