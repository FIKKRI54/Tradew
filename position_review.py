"""Advisory portfolio protection with exact tickets, volumes, and risk states."""
import math
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from exit_management import position_role


def _step(value, step, rounding):
    units = (Decimal(str(max(0, value))) / Decimal(str(step))).to_integral_value(rounding=rounding)
    return float(units * Decimal(str(step)))


def _breakeven_price(position, broker, cost_per_lot, tick_size):
    kind, volume, entry = position['type'], position['volume'], position['price_open']
    sign = 1 if kind == 0 else -1
    tick_result = broker.profit(kind, position['symbol'], volume, entry, entry + sign*tick_size)
    if tick_result is None or tick_result <= 0:
        return None
    ticks = math.ceil((cost_per_lot*volume) / tick_result)
    return entry + sign*ticks*tick_size


def _staged_stop(entry, invalidation, sign, remaining_risk_fraction, tick_size):
    """Return a tick-aligned stop retaining a fraction of initial price risk."""
    distance = abs(entry-invalidation)
    raw = entry-sign*distance*remaining_risk_fraction
    units = raw/tick_size
    return ((math.floor(units) if sign == 1 else math.ceil(units))*tick_size)


def _runner_tp1_reached(snapshot, thesis, sign, current):
    targets = (thesis or {}).get('original_targets') or []
    if not targets:
        return False
    target = targets[0]
    if isinstance(target, (int, float)) and sign*(current-target) >= 0:
        return True
    linked = set((thesis or {}).get('linked_tickets') or [])
    return any(
        deal.get('position_id') in linked and
        deal.get('entry_role') in ('OUT', 'OUT_BY') and
        deal.get('reason') == 5
        for deal in (snapshot.get('recent_deals') or []))


def review(snapshot, config, broker, contexts, buffer):
    changes, alerts, records = [], [], []
    cost = config['cost_per_lot']
    from risk_budget import cash_risk_budgets
    per_trade_budget, portfolio_budget, _ = cash_risk_budgets(snapshot, config)
    total_risk = 0
    actual_unknown = False
    for position in snapshot['positions']:
        p = position
        quote = snapshot['gold_ticks'].get(p['symbol'])
        if quote is None:
            actual_unknown = True
            alerts.append('Missing executable quote for ticket ' + str(p['ticket']))
            records.append({'ticket': p['ticket'], 'cash_risk': None, 'calculation_status': 'quote_unavailable'})
            continue
        sign = 1 if p['type'] == 0 else -1
        direction = 'BUY' if sign == 1 else 'SELL'
        current = quote['bid'] if sign == 1 else quote['ask']
        actual = p['sl']
        thesis = p.get('original_setup')
        if thesis:
            from setup_tracker import SetupTracker
            binding_error = SetupTracker.binding_error(thesis, p, snapshot)
            if binding_error:
                alerts.append(binding_error + ' for ticket ' + str(p['ticket']))
                thesis = None
        elif p.get('setup_binding_error'):
            alerts.append(p['setup_binding_error'] + ' for ticket ' + str(p['ticket']))
        thesis_invalidation = thesis.get('original_invalidation') if thesis else None
        thesis_direction = thesis.get('direction') if thesis else None
        role = position_role(p.get('comment'))
        runner_active = (role == 'runner' and
                         _runner_tp1_reached(snapshot, thesis, sign, current))
        structural = None
        if p['symbol'] == snapshot['symbol']:
            points = contexts['M5']['pivots']['low' if sign == 1 else 'high']
            if points:
                structural = points[-1]['price'] - sign*buffer + (
                    quote['ask']-quote['bid'] if sign == -1 else 0)
                step = snapshot['specifications'][p['symbol']]['trade_tick_size']
                structural = (math.floor(structural/step) if sign == 1 else
                              math.ceil(structural/step))*step
        sensitivity = broker.profit(p['type'], p['symbol'], p['volume'], current, current+1)
        loss = broker.profit(p['type'], p['symbol'], p['volume'], current, actual) if actual > 0 else None
        cash_risk = max(0, -loss)+cost*p['volume'] if loss is not None else None
        record = {
            'ticket': p['ticket'], 'symbol': p['symbol'], 'direction': direction,
            'volume': p['volume'], 'actual_stop': actual, 'actual_tp': p['tp'],
            'cash_risk': cash_risk, 'cash_sensitivity_plus_1': sensitivity,
            'setup_ref': thesis.get('setup_ref') if thesis else None,
            'original_invalidation': thesis_invalidation,
            'management_role': role,
            'runner_active': runner_active if role == 'runner' else None,
            'calculation_status': 'complete' if cash_risk is not None and sensitivity is not None else 'unknown',
        }
        records.append(record)
        if actual <= 0:
            actual_unknown = True
        elif cash_risk is None:
            actual_unknown = True
            alerts.append('Broker risk calculation failed for ticket ' + str(p['ticket']))
        else:
            total_risk += cash_risk
        if sensitivity is None:
            alerts.append('Monetary sensitivity calculation failed for ticket ' + str(p['ticket']))
        if thesis is None:
            alerts.append('Original setup is not bound to ticket ' + str(p['ticket']))
        elif thesis_direction not in (None, direction):
            alerts.append('Bound setup direction conflicts with ticket ' + str(p['ticket']))
        thesis_failed = thesis_invalidation is not None and sign*(current-thesis_invalidation) <= 0
        actual_failed = actual > 0 and sign*(current-actual) <= 0
        if thesis_failed or actual_failed:
            reason = (f"Original {direction} thesis invalidated at {thesis_invalidation}"
                      if thesis_failed else 'Executable quote crossed actual stop')
            changes.append({'ticket': p['ticket'], 'action': 'CLOSE', 'volume': p['volume'],
                            'remaining_volume': 0, 'remaining_risk': 0, 'actual_stop': actual,
                            'original_invalidation': thesis_invalidation, 'reason': reason})
            continue
        spec = snapshot['specifications'][p['symbol']]
        distance = max(spec['trade_stops_level'], spec['trade_freeze_level'])*spec['point']
        proposed_loss = (broker.profit(p['type'], p['symbol'], p['volume'], current, structural)
                         if structural is not None else None)
        proposed_risk = (max(0, -proposed_loss)+cost*p['volume']
                         if proposed_loss is not None else None)
        # Staged protection does not wait for a pivot: first reduce the initial
        # loss allowance, then cover configured costs at break-even. Confirmed
        # M5 pivots remain responsible for the structural trailing stage below.
        if thesis and thesis.get('original_entry') is not None and thesis_invalidation is not None:
            # Every ladder child owns its fill price; a shared setup-level entry
            # would misstate R progress for deeper layers.
            entry = p.get('price_open', thesis['original_entry'])
            original_distance = abs(entry-thesis_invalidation)
            favorable = sign*(current-entry)
            progress_r = favorable/original_distance if original_distance > 0 else 0
            be = _breakeven_price(p, broker, cost, spec['trade_tick_size'])
            improves = be is not None and (actual <= 0 or sign*(be-actual) > 0)
            if (progress_r >= config.get('trailing_break_even_trigger_r', 1.0) and improves and
                    sign*(current-be) > distance):
                be_risk_result = broker.profit(p['type'], p['symbol'], p['volume'], current, be)
                be_risk = max(0, -be_risk_result)+cost*p['volume'] if be_risk_result is not None else None
                changes.append({'ticket': p['ticket'], 'action': 'MOVE SL TO BE',
                                'actual_stop': actual, 'proposed_stop': be,
                                'remaining_volume': p['volume'], 'remaining_risk': be_risk,
                                'management_stage': 'break_even', 'progress_r': progress_r,
                                'reason': 'One original risk unit gained; protect entry and configured costs'})
                continue
            reduction_trigger = config.get('trailing_risk_reduction_trigger_r', .5)
            remaining_fraction = config.get('trailing_risk_reduction_fraction', .5)
            reduced = _staged_stop(
                entry, thesis_invalidation, sign, remaining_fraction,
                spec['trade_tick_size'])
            improves = actual <= 0 or sign*(reduced-actual) > 0
            if (progress_r >= reduction_trigger and improves and
                    sign*(current-reduced) > distance):
                reduced_risk_result = broker.profit(
                    p['type'], p['symbol'], p['volume'], current, reduced)
                reduced_risk = (max(0, -reduced_risk_result)+cost*p['volume']
                                if reduced_risk_result is not None else None)
                changes.append({'ticket': p['ticket'], 'action': 'REDUCE RISK',
                                'actual_stop': actual, 'proposed_stop': reduced,
                                'remaining_volume': p['volume'],
                                'remaining_risk': reduced_risk,
                                'management_stage': 'half_risk',
                                'progress_r': progress_r,
                                'reason': (f'{reduction_trigger:g}R gained; retain '
                                           f'{remaining_fraction:g} of initial price risk')})
                continue
        # Runner children retain their broker SL before TP1. Only after TP1 has
        # actually traded (or the TP child closed at TP) may M5 pivots trail it.
        if role == 'runner' and not runner_active:
            continue
        if structural is not None and sign*(current-structural) > distance:
            if actual <= 0:
                if proposed_risk is None:
                    alerts.append('Proposed protection calculation failed for ticket ' + str(p['ticket']))
                elif proposed_risk > per_trade_budget:
                    allowed = _step(
                        p['volume']*per_trade_budget/proposed_risk,
                        spec['volume_step'], ROUND_FLOOR)
                    if allowed >= spec['volume_min']:
                        close_volume = _step(p['volume']-allowed, spec['volume_step'], ROUND_CEILING)
                        remaining = max(0, p['volume']-close_volume)
                        changes.append({'ticket': p['ticket'], 'action': 'PARTIAL CLOSE',
                                        'volume': close_volume, 'remaining_volume': remaining,
                                        'remaining_risk': proposed_risk*(remaining/p['volume']),
                                        'proposed_stop_after_reduction': structural,
                                        'reason': 'Reduce exposure before applying structural protection'})
                    else:
                        changes.append({'ticket': p['ticket'], 'action': 'CLOSE',
                                        'volume': p['volume'], 'remaining_volume': 0,
                                        'remaining_risk': 0,
                                        'reason': 'Structural protection exceeds per-trade budget at minimum volume'})
                else:
                    changes.append({'ticket': p['ticket'], 'action': 'MOVE SL',
                                    'actual_stop': actual, 'proposed_stop': structural,
                                    'remaining_volume': p['volume'], 'remaining_risk': proposed_risk,
                                    'reason': 'Missing protection; use confirmed structural invalidation'})
            elif sign*(structural-actual) > 0:
                changes.append({'ticket': p['ticket'], 'action': 'TRAIL SL',
                                'actual_stop': actual, 'proposed_stop': structural,
                                'remaining_volume': p['volume'], 'remaining_risk': proposed_risk,
                                'management_stage': ('runner_structure' if role == 'runner'
                                                     else 'structure'),
                                'reason': ('TP1 reached; trail runner behind the latest confirmed M5 swing'
                                           if role == 'runner' else
                                           'New confirmed swing permits less risk without widening')})
        elif actual <= 0:
            alerts.append('No valid structural protection is available for ticket ' + str(p['ticket']))
    pending_risk = 0
    pending_unknown = False
    for order in snapshot['pending_orders']:
        kind = 0 if order['type'] in (2, 4, 6) else 1
        volume = order.get('volume_current', order.get('volume_initial', 0))
        loss = broker.profit(kind, order['symbol'], volume, order['price_open'], order['sl']) if order['sl'] > 0 else None
        if loss is None:
            actual_unknown = True
            pending_unknown = True
            alerts.append('Pending order has unknown stop risk: ' + str(order['ticket']))
        else:
            pending_risk += max(0, -loss)+cost*volume
    total_known = total_risk+pending_risk
    # Actual risk can be unknown while proposed protection is fully calculable.
    # Keep those states separate so unprotected exposure cannot bypass the
    # aggregate budget after concrete stop proposals have been produced.
    def planned_state():
        by_ticket = {change['ticket']: change for change in changes}
        total = pending_risk
        unknown = pending_unknown
        rows = []
        for record in records:
            change = by_ticket.get(record['ticket'])
            if change and change['action'] == 'CLOSE':
                risk = 0
            elif change:
                risk = change.get('remaining_risk')
            else:
                risk = record['cash_risk']
            if risk is None:
                unknown = True
            else:
                total += risk
            rows.append((record, change, risk))
        return total, unknown, rows

    planned_risk, planned_unknown, plan_rows = planned_state()
    if not planned_unknown and planned_risk > portfolio_budget:
        excess = planned_risk-portfolio_budget
        for record, existing, current_risk in sorted(
                plan_rows, key=lambda row: row[2] or 0, reverse=True):
            if excess <= 1e-9:
                break
            ticket = record['ticket']
            if existing and existing['action'] == 'CLOSE':
                continue
            position = next(p for p in snapshot['positions'] if p['ticket'] == ticket)
            current_volume = existing.get('remaining_volume') if existing else position['volume']
            if not current_risk or not current_volume:
                continue
            spec = snapshot['specifications'][position['symbol']]
            per_lot = current_risk/current_volume
            additional_close = _step(min(current_volume, excess/per_lot),
                                     spec['volume_step'], ROUND_CEILING)
            already_closing = max(0, position['volume']-current_volume)
            close_volume = min(position['volume'], _step(
                already_closing+additional_close, spec['volume_step'], ROUND_CEILING))
            remaining = max(0, position['volume']-close_volume)
            if remaining < spec['volume_min']:
                close_volume, remaining, action = position['volume'], 0, 'CLOSE'
            else:
                action = 'PARTIAL CLOSE'
            remaining_risk = per_lot*remaining
            replacement = {'ticket': ticket, 'action': action, 'volume': close_volume,
                           'remaining_volume': remaining, 'remaining_risk': remaining_risk,
                           'reason': 'Deterministic reduction to fit aggregate cash-risk budget'}
            if action != 'CLOSE' and existing:
                required_stop = existing.get(
                    'proposed_stop_after_reduction', existing.get('proposed_stop'))
                if required_stop is not None:
                    replacement['proposed_stop_after_reduction'] = required_stop
                    replacement['required_stop_action'] = existing.get(
                        'required_stop_action', existing['action'])
            changes = [c for c in changes if c['ticket'] != ticket]
            changes.append(replacement)
            excess -= current_risk-remaining_risk
        planned_risk, planned_unknown, plan_rows = planned_state()
        if pending_risk > portfolio_budget:
            alerts.append('Pending orders alone exceed the portfolio budget; manual cancellation review required')
    if actual_unknown:
        alerts.append('Aggregate risk is unknown; no zero-risk assumption is permitted')
    if planned_unknown:
        alerts.append('Planned portfolio risk remains unknown after proposed changes')
    return {
        'changes': changes, 'alerts': list(dict.fromkeys(alerts)),
        'risk_by_ticket': records,
        'actual_portfolio_cash_risk': None if actual_unknown else total_known,
        'known_cash_risk': total_known,
        'planned_cash_risk': None if planned_unknown else planned_risk,
        'pending_cash_risk': pending_risk,
    }
