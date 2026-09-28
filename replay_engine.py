"""Completed-candle M5 walk-forward replay with conservative fill assumptions."""
import math
from datetime import datetime, timezone

from exit_management import entry_exposure_allowed, split_exit_volume, tp1_payoff
from scan_engine import atr, floor_volume, pivots


class ReplayContract:
    """Freeze current broker cash conversion and margin scale for a historical run."""
    def __init__(self, broker, symbol, reference, tick_size):
        self.cash = {}
        self.margin_scale = {}
        for kind in (0, 1):
            value = broker.profit(kind, symbol, 1, reference, reference+tick_size)
            margin = broker.margin(kind, symbol, 1, reference)
            if (value is None or margin is None or not math.isfinite(value) or
                    not math.isfinite(margin) or value == 0 or margin < 0):
                raise ValueError('Replay requires valid broker cash and margin calibration')
            self.cash[kind] = value/tick_size
            self.margin_scale[kind] = margin/reference

    def profit(self, kind, symbol, volume, entry, exit):
        return (exit-entry)*volume*self.cash[kind]

    def margin(self, kind, symbol, volume, entry):
        return entry*volume*self.margin_scale[kind]


def replay_history(snapshot, config, broker, count=5000, auto_trailing=True):
    """Use the production evaluator on chronological M1 opens with seven-frame history.

    No historical LLM responses are fabricated. This is explicitly a deterministic
    counterfactual, even if the running account uses LLM authorization.
    """
    from bisect import bisect_right
    from scan_engine import TIMEFRAMES, evaluate, structure
    histories = snapshot['bars']
    if any(tf not in histories for tf in TIMEFRAMES):
        raise ValueError('Replay requires all seven timeframe histories')
    times = {tf: [b['time'] for b in histories[tf]] for tf in TIMEFRAMES}
    if any(any(b <= a for a, b in zip(ts, ts[1:])) for ts in times.values()):
        raise ValueError('Replay histories must be strictly chronological')
    m5 = histories['M5'][:-1]
    if not m5:
        raise ValueError('No completed replay history')
    start = m5[max(0, len(m5)-count)]['time']
    end = m5[-1]['time']+300
    events = [b for b in histories['M1'][:-1] if start <= b['time'] < end]
    if not events:
        raise ValueError('Replay has no M1 execution history for the requested period')
    symbol = snapshot['symbol']
    spec = snapshot['specifications'][symbol]
    balance = config.get('replay_initial_balance', snapshot['account']['balance'])
    initial_balance = balance
    if not math.isfinite(balance) or balance <= 0:
        raise ValueError('Replay initial balance must be positive')
    spread_fallback = config.get('replay_spread_fallback', config['max_spread'])
    slip = config.get('replay_slippage_price', 0)
    cost = config['cost_per_lot']
    offset = config['timestamp_offset_seconds']
    positions = []
    pendings = []
    entry_records = {}
    used = set()
    trades, skipped, processed, cancelled_pending = [], 0, 0, 0
    context_cache = {}

    def replay_guard(timestamp):
        active = {entry_records[p['ticket']]['setup_id'] for p in positions}
        active.update(p['setup_id'] for p in pendings)
        grouped = {}
        for trade in trades:
            group = grouped.setdefault(trade['setup_id'], {
                'setup_id': trade['setup_id'], 'exit_time': 0, 'net_result': 0})
            group['net_result'] += trade['net_result']
            group['exit_time'] = max(group['exit_time'], trade['exit_time'])
        completed = sorted((g for ref, g in grouped.items() if ref not in active),
                           key=lambda item: item['exit_time'])
        hour = datetime.fromtimestamp(timestamp, timezone.utc).hour
        start_hour = 0 if hour < 7 else 7 if hour < 12 else 12 if hour < 16 else 16 if hour < 21 else 21
        session_start = datetime.fromtimestamp(timestamp, timezone.utc).replace(
            hour=start_hour, minute=0, second=0, microsecond=0).timestamp()
        entries = {trade['setup_id'] for trade in trades+list(entry_records.values())
                   if trade['entry_time'] >= session_start}
        if len(entries) >= config['max_session_entries']:
            return 'session_entry_limit'
        consecutive = 0
        for trade in reversed(completed):
            if trade['exit_time'] < session_start:
                break
            if trade['net_result'] < 0:
                consecutive += 1
            else:
                break
        if consecutive >= config['max_consecutive_losses']:
            return 'consecutive_loss_limit'
        if (completed and completed[-1]['net_result'] < 0 and
                timestamp-completed[-1]['exit_time'] < config['loss_cooldown_seconds']):
            return 'loss_cooldown'
        return None

    def close_position(position, price, timestamp, outcome, gap=False):
        nonlocal balance
        record = entry_records.pop(position['ticket'])
        gross = broker.profit(position['type'], symbol, position['volume'],
                              position['price_open'], price)
        if gross is None or not math.isfinite(gross):
            raise ValueError('Replay exit calculation failed')
        net = gross-cost*position['volume']
        balance += net
        trades.append({**record, 'exit': price, 'exit_time': timestamp-offset,
                       'outcome': outcome, 'gap_through_stop': gap,
                       'final_stop': position['sl'], 'gross_result': gross,
                       'estimated_cost': cost*position['volume'], 'net_result': net,
                       'balance_after': balance})
        positions.remove(position)

    def fill_pending(pending, price, timestamp):
        kind = pending['type_direction']
        direction = pending['direction']
        account_fields = dict(account_login=snapshot['account']['login'],
                              account_server=snapshot['account']['server'],
                              account_mode=snapshot['account_mode'],
                              account_currency=snapshot['account']['currency'])
        ticket = pending['ticket']
        position = dict(ticket=ticket, symbol=symbol, type=kind,
                        magic=config['execution_magic'],
                        volume=pending['volume_current'], price_open=price,
                        sl=pending['sl'], tp=pending['tp'], profit=0,
                        comment=pending.get('comment', ''),
                        original_setup={**account_fields, 'symbol': symbol,
                                        'direction': direction,
                                        'original_entry': price,
                                        'original_invalidation': pending['sl'],
                                        'original_targets': [pending.get(
                                            'planned_target', pending['tp'])]})
        actual_loss = broker.profit(kind, symbol, pending['volume_current'],
                                    price, pending['sl'])
        if actual_loss is None or actual_loss >= 0:
            raise ValueError('Replay pending fill loss calculation failed')
        entry_records[ticket] = dict(
            setup_id=pending['setup_id'], direction=direction,
            setup_family=pending.get('setup_family'),
            setup_score=pending.get('setup_score'),
            chart_confirmation=pending.get('chart_confirmation', []),
            market_context=pending.get('market_context', {}),
            entry_method='broker_pending_limit', pending_placed_time=pending['placed_time'],
            entry_time=timestamp-offset, entry=price, stop=pending['sl'],
            target=pending.get('planned_target', pending['tp']),
            management_role=pending.get('management_role', 'legacy'),
            volume=pending['volume_current'],
            balance_before=balance, initial_cash_risk=-actual_loss+cost*pending['volume_current'],
            planned_cash_risk=pending['planned_cash_risk'],
            entry_zone=pending['entry_zone'], stop_changes=[])
        positions.append(position)
        pendings.remove(pending)

    for bar in events:
        raw_time = bar['time']
        now = raw_time-offset
        spread = _spread(bar, spec['point'], spread_fallback)
        bid, ask = bar['open'], bar['open']+spread
        filled_this_bar = set()
        expired = [pending for pending in pendings if now >= pending['expires_at']]
        for pending in expired:
            pendings.remove(pending)
            skipped += 1
        for pending in list(pendings):
            gap_fill = (ask <= pending['price_open'] if pending['direction'] == 'BUY'
                        else bid >= pending['price_open'])
            if gap_fill:
                price = (min(pending['price_open'], ask) if pending['direction'] == 'BUY'
                         else max(pending['price_open'], bid))
                ticket = pending['ticket']
                fill_pending(pending, price, raw_time)
                filled_this_bar.add(ticket)
        # Existing protection executes across gaps before a new decision at the open.
        for position in list(positions):
            sign = 1 if position['type'] == 0 else -1
            executable = bid if sign == 1 else ask
            if sign*(executable-position['sl']) <= 0:
                close_position(position, executable-sign*slip, raw_time, 'STOP', True)
            elif position['tp'] > 0 and sign*(executable-position['tp']) >= 0:
                close_position(position, position['tp']-sign*slip, raw_time, 'TARGET')
        frames, contexts = {}, {}
        for tf, seconds in TIMEFRAMES.items():
            cutoff = bisect_right(times[tf], raw_time-seconds)
            completed = histories[tf][max(0, cutoff-199):cutoff]
            if len(completed) < 160:
                break
            frames[tf] = completed + [dict(time=raw_time//seconds*seconds,
                                           open=bid, high=bid, low=bid, close=bid)]
            key = (tf, completed[-1]['time'])
            if key not in context_cache:
                context_cache[key] = structure(completed, config['structural_tolerance'], config)
            contexts[tf] = context_cache[key]
        if len(frames) != len(TIMEFRAMES):
            skipped += 1
            continue
        # The live supervisor runs after a snapshot. Model the same boundary:
        # an opening gap may fill broker-side first, otherwise a newly completed
        # M5 reclaim or structural break cancels before this M1 bar can touch it.
        for pending in list(pendings):
            sign = 1 if pending['direction'] == 'BUY' else -1
            completed_m5 = frames['M5'][:-1]
            invalid = sign*(bid-pending['structural_invalidation']) <= 0
            reclaimed = any(
                item['time'] > pending['confirmation_time'] and
                sign*(item['close']-pending['level']) <= 0
                for item in completed_m5)
            if invalid or reclaimed:
                pendings.remove(pending)
                skipped += 1
                cancelled_pending += 1
        # Bound cached structures during large runs.
        if len(context_cache) > 1000:
            context_cache = {(tf, frames[tf][-2]['time']): ctx for tf, ctx in contexts.items()}
        position_values = [(
            position,
            broker.profit(position['type'], symbol, position['volume'],
                          position['price_open'], bid if position['type'] == 0 else ask),
            broker.margin(position['type'], symbol, position['volume'],
                          position['price_open'])) for position in positions]
        if any(value is None or not math.isfinite(value)
               for _, profit, margin_value in position_values
               for value in (profit, margin_value)):
            raise ValueError('Replay floating profit or margin calculation failed')
        floating = sum(profit for _, profit, _ in position_values)
        margin = sum(margin_value for _, _, margin_value in position_values)
        for position, profit, _ in position_values:
            position['profit'] = profit
        account = {**snapshot['account'], 'balance': balance, 'equity': balance+floating,
                   'margin_free': max(0, balance+floating-margin)}
        tick = dict(time=raw_time, bid=bid, ask=ask)
        historical = {**snapshot, 'snapshot_id': f'replay:{raw_time}',
                      'retrieval_start_utc': now, 'retrieval_end_utc': now,
                      'account': account, 'positions': positions,
                      'pending_orders': [{key: pending[key] for key in
                                          ('ticket', 'symbol', 'type', 'volume_current',
                                           'price_open', 'sl', 'tp')}
                                         for pending in pendings],
                      'bars': frames, 'tick': tick, 'initial_tick': tick,
                      'gold_ticks': {symbol: tick}, 'errors': []}
        decision = evaluate(historical, config, broker, contexts=contexts)
        processed += 1
        if decision['data_quality']:
            skipped += 1
        if not decision['data_quality'] and positions and auto_trailing:
            for change in decision['proposed_management_changes']:
                if change['action'] not in ('REDUCE RISK', 'TRAIL SL', 'MOVE SL TO BE'):
                    continue
                position = next((item for item in positions
                                 if item['ticket'] == change.get('ticket')), None)
                if position is None:
                    continue
                stop = change['proposed_stop']
                sign = 1 if position['type'] == 0 else -1
                current = bid if sign == 1 else ask
                distance = max(spec['trade_stops_level'], spec['trade_freeze_level'])*spec['point']
                if sign*(stop-position['sl']) > 0 and sign*(current-stop) > distance:
                    position['sl'] = stop
                    entry_records[position['ticket']]['stop_changes'].append(
                        {'time': now, 'stop': stop, 'action': change['action']})
        if (not decision['data_quality'] and
              entry_exposure_allowed(positions, pendings, config['execution_magic']) and
              decision.get('setup_state') == 'ARMED_LIMIT' and
              decision.get('setup_id') not in used):
            if replay_guard(now):
                skipped += 1
                continue
            plan = decision['entry_plan']
            direction = decision['planned_direction']
            if plan.get('rule_version') in ('structure-research-19',
                                             'structure-research-20',
                                             'structure-research-21',
                                             'structure-research-22',
                                             'structure-research-23',
                                             'structure-research-24',
                                             'structure-research-25',
                                             'structure-research-26', 'structure-research-27',
                                             'structure-research-28', 'structure-research-29',
                                             'structure-research-30'):
                from limit_ladder import ladder_quality_acceptable
                acceptable, _, _ = ladder_quality_acceptable(
                    plan, bid, ask, config)
                if not acceptable:
                    skipped += 1
                    continue
            kind = 0 if direction == 'BUY' else 1
            sign = 1 if kind == 0 else -1
            tick_size = spec['trade_tick_size']
            minimum_distance = max(spec['trade_stops_level'],
                                   spec['trade_freeze_level'])*spec['point']
            protected_stop_distance = max(
                minimum_distance, spread*2,
                float(plan.get('m5_atr') or 0)*config['minimum_layer_stop_atr_fraction'])
            budget = balance*config['balance_risk_fraction']
            layers = plan.get('limit_layers') or [{
                'index': 1, 'quality': 'single', 'risk_weight': 1.0,
                'price': (plan['zone']['high']+spread if direction == 'BUY'
                          else plan['zone']['low']),
                'target': plan['targets'][0],
            }]
            staged = []
            reference_volume = None
            for layer in layers:
                price = round(round(layer['price']/tick_size)*tick_size,
                              int(spec.get('digits', 2)))
                target = layer['target']
                pending_distance = ask-price if direction == 'BUY' else price-bid
                loss_per_lot = broker.profit(kind, symbol, 1, price, plan['stop'])
                cash_per_lot = (-loss_per_lot+cost if loss_per_lot is not None else None)
                layer_budget = budget*layer['risk_weight']
                volume = (floor_volume(min(layer_budget/cash_per_lot, spec['volume_max']),
                                       spec['volume_step'])
                          if cash_per_lot and cash_per_lot > 0 else 0)
                if reference_volume is not None:
                    volume = floor_volume(
                        min(volume, reference_volume*config['max_ladder_volume_ratio']),
                        spec['volume_step'])
                layer_margin = broker.margin(kind, symbol, volume, price) if volume else None
                reward_per_lot = broker.profit(kind, symbol, 1, price, target)
                banked_reward = (tp1_payoff(reward_per_lot, cost, volume, spec['volume_min'],
                                          spec['volume_step'], config)
                                 if reward_per_lot is not None and volume >= spec['volume_min'] else 0)
                if (pending_distance <= 0 or pending_distance < minimum_distance-1e-9 or
                        sign*(price-plan['stop']) <= 0 or sign*(target-price) <= 0 or
                        abs(price-plan['stop']) < protected_stop_distance or
                        volume < spec['volume_min'] or layer_margin is None or
                        layer_margin < 0 or reward_per_lot is None or
                        reward_per_lot-cost <= 0 or
                        banked_reward/(cash_per_lot*volume) < config['minimum_ladder_reward_risk']):
                    continue
                if reference_volume is None:
                    reference_volume = volume
                staged.append((layer, price, target, volume, cash_per_lot,
                               layer_margin, reward_per_lot-cost))
            if (not staged or sum(item[5] for item in staged) >
                    account['margin_free']*config['max_margin_fraction'] or
                    sum(item[6]*item[3] for item in staged) /
                    sum(item[4]*item[3] for item in staged) <
                    config['minimum_ladder_reward_risk']):
                skipped += 1
            else:
                for layer, price, target, volume, cash_per_lot, _, _ in staged:
                    take_volume, runner_volume = (
                        split_exit_volume(
                            volume, spec['volume_min'], spec['volume_step'],
                            config['take_profit_close_fraction'])
                        if config.get('runner_enabled') else (volume, 0.0))
                    runner_enabled = bool(config.get('runner_enabled'))
                    children = [(('take_profit' if runner_enabled else 'legacy'),
                                 take_volume, target, ('T' if runner_enabled else ''))]
                    if runner_volume >= spec['volume_min']:
                        children.append(('runner', runner_volume, 0.0, 'R'))
                    for child_number, (role, child_volume, child_target, suffix) in enumerate(
                            children, 1):
                        child_ticket = (-((len(used)+1)*100+layer['index']*10+child_number)
                                        if runner_enabled else
                                        -(len(used)*10+layer['index']))
                        pendings.append(dict(
                            ticket=child_ticket,
                            symbol=symbol, type=2 if direction == 'BUY' else 3,
                            magic=config['execution_magic'],
                            type_direction=kind, direction=direction,
                            volume_current=child_volume, price_open=price,
                            sl=plan['stop'], tp=child_target, planned_target=target,
                            comment=(f"Tradew S:replay00000 L{layer['index']}{suffix}"
                                     if runner_enabled else ''),
                            management_role=role, expires_at=plan['expires_at'],
                            level=plan['level'], layer_index=layer['index'],
                            layer_quality=layer.get('quality'),
                            structural_invalidation=plan['structural_invalidation'],
                            confirmation_time=plan['confirmation_time'],
                            setup_id=decision['setup_id'], placed_time=now,
                            setup_family=decision['setup_id'].split(':')[1],
                            setup_score=decision.get(
                                'buy_setup_score' if direction == 'BUY' else 'sell_setup_score'),
                            chart_confirmation=decision.get('chart_confirmation') or [],
                            market_context={
                                'session': decision.get('session'),
                                'volatility_regime': (decision.get('structure', {}).get('M5') or {}).get('volatility_regime'),
                                'trends': {frame: (decision.get('structure', {}).get(frame) or {}).get('trend')
                                           for frame in ('D1', 'H4', 'H1', 'M30', 'M15')},
                            },
                            planned_cash_risk=cash_per_lot*child_volume,
                            entry_zone=decision['planned_entry_zone']))
                used.add(decision['setup_id'])
        elif (not decision['data_quality'] and
              entry_exposure_allowed(positions, pendings, config['execution_magic']) and
              decision['action'] in ('BUY', 'SELL') and
              decision['setup_id'] not in used):
            if replay_guard(now):
                skipped += 1
                continue
            direction = decision['action']
            kind = 0 if direction == 'BUY' else 1
            sign = 1 if kind == 0 else -1
            price = decision['entry']+sign*slip
            stop, target = decision['stop'], decision['targets'][0]
            volume = decision['volume']
            actual_loss = broker.profit(kind, symbol, volume, price, stop)
            if actual_loss is None or actual_loss >= 0:
                raise ValueError('Replay fill loss calculation failed')
            account_fields = dict(account_login=account['login'], account_server=account['server'],
                                  account_mode=snapshot['account_mode'], account_currency=account['currency'])
            used.add(decision['setup_id'])
            take_volume, runner_volume = (
                split_exit_volume(volume, spec['volume_min'], spec['volume_step'],
                                  config['take_profit_close_fraction'])
                if config.get('runner_enabled') else (volume, 0.0))
            runner_enabled = bool(config.get('runner_enabled'))
            children = [(('take_profit' if runner_enabled else 'legacy'),
                         take_volume, target, ('T' if runner_enabled else ''))]
            if runner_volume >= spec['volume_min']:
                children.append(('runner', runner_volume, 0.0, 'R'))
            for role, child_volume, child_target, suffix in children:
                ticket = len(trades)+len(positions)+1
                child_loss = broker.profit(kind, symbol, child_volume, price, stop)
                position = dict(
                    ticket=ticket, symbol=symbol, type=kind, volume=child_volume,
                    magic=config['execution_magic'],
                    price_open=price, sl=stop, tp=child_target, profit=0,
                    comment=(f'Tradew S:replay00000 L1{suffix}'
                             if runner_enabled else ''),
                    original_setup={**account_fields, 'symbol': symbol,
                                    'direction': direction, 'original_entry': price,
                                    'original_invalidation': stop,
                                    'original_targets': [target]})
                positions.append(position)
                entry_records[ticket] = dict(
                    setup_id=decision['setup_id'], direction=direction,
                    setup_family=decision['setup_id'].split(':')[1],
                    setup_score=decision.get(
                        'buy_setup_score' if direction == 'BUY' else 'sell_setup_score'),
                    chart_confirmation=decision.get('chart_confirmation') or [],
                    market_context={
                        'session': decision.get('session'),
                        'volatility_regime': (decision.get('structure', {}).get('M5') or {}).get('volatility_regime'),
                        'trends': {frame: (decision.get('structure', {}).get(frame) or {}).get('trend')
                                   for frame in ('D1', 'H4', 'H1', 'M30', 'M15')},
                    },
                    entry_method='market_after_zone_contact', entry_time=now,
                    entry=price, stop=stop, target=target,
                    management_role=role, volume=child_volume,
                    balance_before=balance,
                    initial_cash_risk=-child_loss+cost*child_volume,
                    planned_cash_risk=(decision['cash_risk']*child_volume/volume),
                    entry_zone=decision['planned_entry_zone'], stop_changes=[])
        # A native pending order can fill anywhere inside this M1 bar. Limit
        # fills are capped at the requested price; an opening gap may improve it.
        for pending in list(pendings):
            touched = (bar['low']+spread <= pending['price_open']
                       if pending['direction'] == 'BUY'
                       else bar['high'] >= pending['price_open'])
            if touched:
                ticket = pending['ticket']
                fill_pending(pending, pending['price_open'], raw_time+60)
                filled_this_bar.add(ticket)
        # All market entries are at the observed open, before this bar's extremes.
        for position in list(positions):
            sign = 1 if position['type'] == 0 else -1
            stopped = (bar['low'] <= position['sl'] if sign == 1 else
                       bar['high']+spread >= position['sl'])
            targeted = (position['tp'] > 0 and
                        (bar['high'] >= position['tp'] if sign == 1 else
                         bar['low']+spread <= position['tp']))
            if stopped:
                close_position(position, position['sl']-sign*slip, raw_time+60, 'STOP')
            elif targeted and position['ticket'] not in filled_this_bar:
                close_position(position, position['tp']-sign*slip, raw_time+60, 'TARGET')
    if not processed:
        raise ValueError('Insufficient synchronized seven-timeframe warmup history')
    if positions:
        last = events[-1]
        for position in list(positions):
            sign = 1 if position['type'] == 0 else -1
            price = last['close'] + (0 if sign == 1 else
                                     _spread(last, spec['point'], spread_fallback))
            close_position(position, price-sign*slip, last['time']+60, 'HORIZON')
    if pendings:
        skipped += len(pendings)
    return {
        'method': 'Production deterministic evaluator; seven timeframes; sampled M1 opens',
        'authorization_mode': 'deterministic_counterfactual', 'auto_trailing': auto_trailing,
        'initial_balance': initial_balance, 'final_balance': balance,
        'processed_events': processed, 'skipped_events': skipped,
        'cancelled_pending_orders': cancelled_pending,
        'fill_policy': ('Authorized armed plans place risk-split limit ladders; '
                        'broker-valid volume is split into TP1 and runner children; '
                        'limit-price-or-better fills; '
                        'completed-M5/structure cancellation; M1 market fallback; '
                        'worse-open gap stops; stop first if ambiguous'),
        'limitations': [
            'M1 OHLC sampling cannot reconstruct the exact intraminute path around a pending fill.',
            'Historical LLM authorization is not reconstructed; no LLM is called.',
            'Current broker contract conversion, margin, and specifications approximate historical costs.',
            'Spread is a bar estimate; partial broker fills, latency and order-book liquidity are unavailable.',
            'TP1/runner allocation is modeled at broker volume-step precision; child-order rejection sequencing is unavailable.',
        ],
        'summary': _summary(trades, skipped), 'trades': trades,
    }


def _spread(bar, point, fallback):
    recorded = bar.get('spread')
    if isinstance(recorded, (int, float)) and math.isfinite(recorded) and recorded >= 0:
        return recorded * point
    return fallback


def _summary(trades, rejected):
    results = [trade['net_result'] for trade in trades]
    equity, peak, drawdown = 0.0, 0.0, 0.0
    for result in results:
        equity += result
        peak = max(peak, equity)
        drawdown = max(drawdown, peak-equity)
    wins = [value for value in results if value > 0]
    losses = [value for value in results if value < 0]
    return {
        'trade_count': len(trades), 'rejected_setups': rejected,
        'wins': len(wins), 'losses': len(losses),
        'timeouts': sum(trade['outcome'] == 'HORIZON' for trade in trades),
        'win_rate': len(wins)/len(results) if results else None,
        'net_result': sum(results),
        'expectancy': sum(results)/len(results) if results else None,
        'profit_factor': sum(wins)/abs(sum(losses)) if losses else None,
        'max_closed_trade_drawdown': drawdown,
    }


def replay_m5(bars, config, broker, symbol, specification):
    """Replay setup confirmations without look-ahead or overlapping positions.

    Bars are broker bid-chart M5 candles. Entry waits for a bounded virtual-limit
    retest during the setup expiry window. A BUY pays spread; SELL target/stop
    tests use reconstructed ask prices. If a single bar touches stop and target,
    the stop is selected first.
    """
    if len(bars) < 80:
        raise ValueError('At least 80 completed M5 bars are required for replay')
    if any(b['time'] <= a['time'] for a, b in zip(bars, bars[1:])):
        raise ValueError('Replay bars must be strictly chronological')
    point = specification['point']
    tick_size = specification['trade_tick_size']
    fallback_spread = config.get('replay_spread_fallback', config['max_spread'])
    slippage = config.get('replay_slippage_price', 0)
    horizon = int(config.get('replay_horizon_bars', 48))
    cost = config['cost_per_lot']
    balance = config.get('replay_initial_balance',
                         config['per_trade_cash_budget']/config.get('balance_risk_fraction', 1))
    tolerance = config['structural_tolerance']
    trades, rejected = [], 0
    cursor = 60
    from setups import detect
    while cursor < len(bars)-1:
        known = bars[:cursor+1]
        current_atr = atr(known)
        if current_atr is None:
            cursor += 1
            continue
        confirmation_spread = _spread(known[-1], point, fallback_spread)
        buffer = max(confirmation_spread,
                     current_atr*config['buffer_atr_fraction'], tolerance)
        setup_config = {**config, '_m5_atr': current_atr}
        fresh = [setup for setup in detect(
            known, buffer, tolerance, config['confirmation_expiry_bars'], setup_config)
            if setup['state'] == 'CONFIRMED' and
            setup['confirmation_time'] == known[-1]['time']]
        if not fresh:
            cursor += 1
            continue
        setup = sorted(fresh, key=lambda item: (item['direction'], item['family']))[0]
        direction = setup['direction']
        kind = 0 if direction == 'BUY' else 1
        sign = 1 if kind == 0 else -1
        from setups import limit_entry_zone
        limit_width = max(buffer, current_atr*config['limit_entry_atr_fraction'])
        zone = setup.get('entry_zone') or limit_entry_zone(
            setup['level'], direction, limit_width)
        expiry = int(config['confirmation_expiry_bars'])
        entry_index = None
        entry_spread = None
        for index in range(cursor+1, min(len(bars), cursor+expiry+2)):
            candidate = bars[index]
            spread = _spread(candidate, point, fallback_spread)
            if index > cursor+1 and sign*(bars[index-1]['close']-setup['level']) <= 0:
                break
            if spread > config['max_spread']:
                continue
            if (candidate['low'] <= zone['high'] and candidate['high'] >= zone['low'] and
                    (candidate['high'] > setup['level'] if kind == 0 else
                     candidate['low'] < setup['level'])):
                entry_index, entry_spread = index, spread
                break
        if entry_index is None:
            rejected += 1
            cursor += expiry+1
            continue
        entry_bar = bars[entry_index]
        # A virtual limit executes on first zone contact. Apply adverse slippage
        # because the replay has no tick path inside the bar.
        entry = ((zone['high']+entry_spread+slippage) if kind == 0 else
                 (zone['low']-slippage))
        stop_raw = setup['invalidation'] - sign*buffer + (entry_spread if kind == 1 else 0)
        stop = (math.floor(stop_raw/tick_size) if kind == 0 else
                math.ceil(stop_raw/tick_size))*tick_size
        known_pivots = pivots(known, tolerance)
        side = 'high' if kind == 0 else 'low'
        levels = sorted({p['price'] for p in known_pivots[side]
                         if sign*(p['price']-entry) > buffer}, reverse=kind == 1)
        if not levels or sign*(entry-stop) <= 0:
            rejected += 1
            cursor += 1
            continue
        target = levels[0]
        loss_per_lot = broker.profit(kind, symbol, 1, entry, stop)
        reward_per_lot = broker.profit(kind, symbol, 1, entry, target)
        if loss_per_lot is None or reward_per_lot is None or loss_per_lot >= 0:
            rejected += 1
            cursor += 1
            continue
        cash_per_lot = -loss_per_lot+cost
        from risk_budget import cash_risk_budgets
        budgets = cash_risk_budgets({'account': {'balance': balance}}, config)
        budget = min(budgets[:2])
        volume = floor_volume(min(budget/cash_per_lot, specification['volume_max']),
                              specification['volume_step'])
        risk = cash_per_lot*volume
        reward = reward_per_lot*volume-cost*volume
        if volume < specification['volume_min'] or risk <= 0:
            rejected += 1
            cursor += 1
            continue
        end = min(len(bars)-1, entry_index+horizon)
        exit_index, exit_price, outcome = end, None, 'HORIZON'
        gap_through_stop = False
        for index in range(entry_index, end+1):
            bar = bars[index]
            spread = _spread(bar, point, fallback_spread)
            if kind == 0:
                stopped = bar['low'] <= stop
                targeted = bar['high'] >= target
            else:
                stopped = bar['high']+spread >= stop
                targeted = bar['low']+spread <= target
            if stopped:
                exit_index = index
                executable_open = bar['open'] if kind == 0 else bar['open']+spread
                stop_fill = min(stop, executable_open) if kind == 0 else max(stop, executable_open)
                gap_through_stop = stop_fill != stop
                exit_price = stop_fill-sign*slippage
                outcome = 'STOP'
                break
            if targeted and index != entry_index:
                exit_index = index
                exit_price = target-sign*slippage
                outcome = 'TARGET'
                break
        if exit_price is None:
            final_spread = _spread(bars[exit_index], point, fallback_spread)
            exit_price = bars[exit_index]['close'] - slippage if kind == 0 else (
                bars[exit_index]['close']+final_spread+slippage)
        gross = broker.profit(kind, symbol, volume, entry, exit_price)
        if gross is None:
            rejected += 1
            cursor = exit_index+1
            continue
        trades.append({
            'setup_family': setup['family'], 'direction': direction,
            'entry_method': 'virtual_limit_retest', 'entry_zone': zone,
            'confirmation_time': setup['confirmation_time'],
            'entry_time': entry_bar['time'], 'exit_time': bars[exit_index]['time'],
            'entry': entry, 'stop': stop, 'target': target, 'exit': exit_price,
            'volume': volume, 'initial_cash_risk': risk, 'outcome': outcome,
            'gap_through_stop': gap_through_stop,
            'gross_result': gross, 'estimated_cost': cost*volume,
            'net_result': gross-cost*volume,
        })
        balance += gross-cost*volume
        cursor = exit_index+1
    return {
        'method': ('Legacy M5 setup-only diagnostic; not the production strategy replay'),
        'fill_policy': ('recorded bar spread or configured fallback; adverse slippage; '
                        'gap-through stops fill at the worse executable open; stop first on ambiguous bars'),
        'limitations': [
            'Current broker contract settings are applied to historical prices.',
            'Intrabar path is unavailable; stop-first is deliberately conservative.',
            'Entry-bar targets are not credited because they may precede the retest.',
            'This setup replay does not recreate the six-timeframe live score or multi-timeframe zones.',
            'Results are research evidence, not prospective validation.',
        ],
        'summary': _summary(trades, rejected), 'trades': trades,
    }
