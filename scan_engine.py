"""Deterministic advisory rules. No terminal access or execution side effects."""
import math
from decimal import Decimal, ROUND_FLOOR

RULE_VERSION = 'structure-research-29'
TIMEFRAMES = {'M1': 60, 'M5': 300, 'M15': 900, 'M30': 1800, 'H1': 3600,
              'H4': 14400, 'D1': 86400}
TIMEFRAME_WEIGHTS = {'H4': 15, 'H1': 15, 'M30': 10, 'M15': 10, 'M5': 25, 'M1': 10}
ZONE_WEIGHTS = {'M5': 1, 'M15': 2, 'M30': 2, 'H1': 3, 'H4': 4, 'D1': 5}


def true_ranges(bars):
    return [max(b['high'] - b['low'], abs(b['high'] - a['close']),
                abs(b['low'] - a['close'])) for a, b in zip(bars, bars[1:])]


def atr(bars, period=14):
    ranges = true_ranges(bars)
    return sum(ranges[-period:]) / period if len(ranges) >= period else None


def pivots(bars, tolerance=0):
    """Rightmost equal extreme wins; pivot available only after two later closes."""
    result = {'high': [], 'low': []}
    for i in range(2, len(bars) - 2):
        for side, field, extreme in [('high', 'high', max), ('low', 'low', min)]:
            value = bars[i][field]
            window = [b[field] for b in bars[i-2:i+3]]
            if value == extreme(window) and value not in window[3:]:
                point = {'price': value, 'time': bars[i]['time'],
                         'confirmed_bar': bars[i+2]['time']}
                if result[side] and abs(result[side][-1]['price'] - value) <= tolerance:
                    result[side][-1] = point
                else:
                    result[side].append(point)
    return result


def structure(bars, tolerance, config=None):
    p = pivots(bars, tolerance)
    trend = 'mixed'
    if len(p['high']) >= 2 and len(p['low']) >= 2:
        dh = p['high'][-1]['price'] - p['high'][-2]['price']
        dl = p['low'][-1]['price'] - p['low'][-2]['price']
        if dh > tolerance and dl > tolerance:
            trend = 'bullish'
        elif dh < -tolerance and dl < -tolerance:
            trend = 'bearish'
    short_atr, long_atr = atr(bars), atr(bars, 50)
    ratio = short_atr / long_atr if short_atr is not None and long_atr else None
    low = (config or {}).get('volatility_low_ratio', .75)
    high = (config or {}).get('volatility_high_ratio', 1.5)
    regime = 'unknown' if ratio is None else ('quiet' if ratio < low else 'high' if ratio > high else 'normal')
    return {'trend': trend, 'pivots': p, 'atr14_simple': short_atr,
            'atr50_simple': long_atr, 'volatility_ratio': ratio,
            'volatility_regime': regime}


def _classify_zone(zone, bars_by_timeframe, as_of_time, invalidation_tolerance):
    members = zone.pop('members', [])
    if bars_by_timeframe is None:
        zone.update(status='unknown', active=False, active_sources=[],
                    broken_sources=[], unknown_sources=list(zone['sources']),
                    source_states={timeframe: 'unknown' for timeframe in zone['sources']},
                    invalidation_time=None, active_strength=0,
                    activity_checked=False)
        return zone

    per_source = {}
    for member in members:
        timeframe = member['timeframe']
        seconds = TIMEFRAMES.get(timeframe)
        source_bars = bars_by_timeframe.get(timeframe) or []
        confirmed_bar = member.get('confirmed_bar')
        if (seconds is None or not source_bars or
                not isinstance(confirmed_bar, (int, float)) or
                source_bars[0]['time'] > confirmed_bar):
            state, broken_time = 'unknown', None
        else:
            cutoff = as_of_time
            if cutoff is None:
                cutoff = max((bar['time'] + seconds for bar in source_bars),
                             default=float('-inf'))
            crossed = next((bar for bar in source_bars
                            if bar['time'] >= confirmed_bar and
                            bar['time'] + seconds <= cutoff and
                            ((zone['kind'] == 'DEMAND' and
                              bar['close'] < zone['low']-invalidation_tolerance) or
                             (zone['kind'] == 'SUPPLY' and
                              bar['close'] > zone['high']+invalidation_tolerance))), None)
            state = 'broken' if crossed else 'active'
            broken_time = crossed['time'] + seconds if crossed else None
        per_source.setdefault(timeframe, []).append({
            'status': state, 'weight': member['weight'],
            'broken_time': broken_time,
        })

    source_states = {}
    active_strength = 0
    invalidation_times = []
    for timeframe, entries in per_source.items():
        states = {entry['status'] for entry in entries}
        state = ('active' if 'active' in states else
                 'unknown' if 'unknown' in states else 'broken')
        source_states[timeframe] = state
        active_strength += sum(entry['weight'] for entry in entries
                               if entry['status'] == 'active')
        if state == 'broken':
            invalidation_times.extend(entry['broken_time'] for entry in entries
                                      if entry['broken_time'] is not None)

    active_sources = sorted((tf for tf, state in source_states.items()
                             if state == 'active'),
                            key=lambda name: list(ZONE_WEIGHTS).index(name))
    broken_sources = sorted((tf for tf, state in source_states.items()
                             if state == 'broken'),
                            key=lambda name: list(ZONE_WEIGHTS).index(name))
    unknown_sources = sorted((tf for tf, state in source_states.items()
                              if state == 'unknown'),
                             key=lambda name: list(ZONE_WEIGHTS).index(name))
    status = ('active' if active_sources else
              'unknown' if unknown_sources else 'broken')
    zone.update(status=status, active=status == 'active',
                active_sources=active_sources, broken_sources=broken_sources,
                unknown_sources=unknown_sources, source_states=source_states,
                active_strength=active_strength,
                invalidation_time=(max(invalidation_times)
                                   if status == 'broken' and invalidation_times else None),
                activity_checked=True)
    return zone


def structural_zones(contexts, width, minimum_strength=2, bars_by_timeframe=None,
                     as_of_time=None, invalidation_tolerance=0):
    """Cluster confirmed pivots and track whether each source still holds."""
    width = max(float(width), 1e-12)
    zones = []
    for side, kind in (('high', 'SUPPLY'), ('low', 'DEMAND')):
        points = []
        for timeframe, weight in ZONE_WEIGHTS.items():
            pivots_for_side = (contexts.get(timeframe, {}).get('pivots') or {}).get(side) or []
            points.extend({'price': point['price'], 'time': point.get('time'),
                           'confirmed_bar': point.get('confirmed_bar'),
                           'timeframe': timeframe, 'weight': weight}
                          for point in pivots_for_side[-12:])
        for point in sorted(points, key=lambda item: item['price']):
            matching = next((zone for zone in zones if zone['kind'] == kind and
                             abs(zone['price']-point['price']) <= width), None)
            if matching is None:
                matching = {'kind': kind, 'price': point['price'], 'strength': 0,
                            'touches': 0, 'sources': [], 'members': [],
                            'latest_time': point['time']}
                zones.append(matching)
            total = matching['strength'] + point['weight']
            matching['price'] = ((matching['price']*matching['strength'] +
                                  point['price']*point['weight']) / total)
            matching['strength'] = total
            matching['touches'] += 1
            matching['sources'].append(point['timeframe'])
            matching['members'].append({
                'price': point['price'], 'time': point.get('time'),
                'confirmed_bar': point.get('confirmed_bar'),
                'timeframe': point['timeframe'], 'weight': point['weight'],
            })
            observed_times = [value for value in
                              (matching['latest_time'], point['time'])
                              if value is not None]
            matching['latest_time'] = max(observed_times) if observed_times else None
    result = []
    for zone in zones:
        if zone['strength'] < minimum_strength:
            continue
        zone['low'] = zone['price']-width/2
        zone['high'] = zone['price']+width/2
        zone['sources'] = sorted(set(zone['sources']),
                                 key=lambda name: list(ZONE_WEIGHTS).index(name))
        result.append(_classify_zone(zone, bars_by_timeframe, as_of_time,
                                     invalidation_tolerance))
    return sorted(result, key=lambda item: item['price'])


def opposing_targets(zones, direction, entry, buffer, current_price=None, spread=0):
    """Return execution-aware objectives before the opposing Bid-price zone."""
    kind = 'SUPPLY' if direction == 'BUY' else 'DEMAND'
    sign = 1 if direction == 'BUY' else -1
    candidates = []
    for zone in zones:
        if zone.get('status') != 'active' or zone.get('kind') != kind:
            continue
        low = zone.get('low', zone.get('price'))
        high = zone.get('high', zone.get('price'))
        if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
            continue
        target = (low-buffer if direction == 'BUY' else
                  high+max(0, spread)+buffer)
        if (sign*(target-entry) > buffer and
                (current_price is None or sign*(target-current_price) > 0)):
            candidates.append((sign*(target-entry), target))
    return [target for _, target in sorted(candidates)[:2]]


def context_gate(contexts, direction, setup, score, config):
    """Hard eligibility gate; heuristic context is no longer display-only."""
    expected = 'bullish' if direction == 'BUY' else 'bearish'
    opposite = 'bearish' if direction == 'BUY' else 'bullish'
    higher = [contexts[name].get('trend') for name in ('H4', 'H1', 'M30')]
    aligned = sum(value == expected for value in higher)
    failures = []
    minimum = int(config.get('minimum_higher_timeframe_alignment', 0))
    if aligned < minimum:
        failures.append(
            f'higher-timeframe alignment {aligned}/3 is below {minimum}/3')
    if score < config.get('minimum_setup_score', 0):
        failures.append(
            f'setup score {score:.0f} is below {config.get("minimum_setup_score", 0):.0f}')
    if contexts['M15'].get('trend') == opposite:
        failures.append('M15 development opposes the setup')
    patterns = (setup or {}).get('chart_patterns') or []
    named_reversal = any(label.endswith(('engulfing', 'rejection')) or
                         label in ('two_soldiers', 'two_crows',
                                   'three_soldiers', 'three_crows')
                         for label in patterns)
    if contexts['M5'].get('trend') == opposite and not named_reversal:
        failures.append('M5 trend opposes the setup without a named reversal')
    # D1 is a macro filter, never an entry trigger. A trade may oppose it only
    # when M5 supplies explicit reversal evidence; M5 still owns confirmation.
    if contexts['D1'].get('trend') == opposite and not named_reversal:
        failures.append('D1 macro trend opposes the setup without M5 reversal evidence')
    return failures


def confirmed_retest(bars, zone, direction, after_time):
    """Find a completed M1 touch-and-rejection after the M5 setup confirmed."""
    sign = 1 if direction == 'BUY' else -1
    for bar in bars:
        if bar.get('time', 0) <= after_time:
            continue
        touched = bar['low'] <= zone['high'] and bar['high'] >= zone['low']
        directional = sign*(bar['close']-bar['open']) > 0
        reclaimed = (bar['close'] > zone['high'] if sign == 1
                     else bar['close'] < zone['low'])
        candle_range = max(bar['high']-bar['low'], 1e-12)
        close_location = (bar['close']-bar['low'])/candle_range
        strong_close = close_location >= .6 if sign == 1 else close_location <= .4
        if touched and directional and reclaimed and strong_close:
            return bar['time']
    return None


def planned_ladder_reward_risk(plan):
    """Gross price-distance R:R across risk-weighted pending layers."""
    sign = 1 if plan['direction'] == 'BUY' else -1
    values = []
    for layer in plan.get('limit_layers') or []:
        risk = sign*(layer['price']-plan['stop'])
        reward = sign*(layer['target']-layer['price'])
        if risk <= 0 or reward <= 0:
            return None
        values.append(float(layer['risk_weight'])*reward/risk)
    return sum(values) if values else None


def market_session(epoch):
    if epoch is None:
        return 'unresolved'
    from datetime import datetime, timezone
    hour = datetime.fromtimestamp(epoch, timezone.utc).hour
    if 7 <= hour < 12:
        return 'London'
    if 12 <= hour < 16:
        return 'London / New York overlap'
    if 16 <= hour < 21:
        return 'New York'
    if 0 <= hour < 7:
        return 'Asia'
    return 'Rollover / closed-market window'


def directional_score(contexts, completed, direction, setup, has_target, spread, config):
    bullish = direction == 'BUY'
    components = {}
    for tf in ('H4', 'H1', 'M30'):
        trend = contexts[tf]['trend']
        expected = 'bullish' if bullish else 'bearish'
        components[tf + '_context'] = TIMEFRAME_WEIGHTS[tf] if trend == expected else (
            TIMEFRAME_WEIGHTS[tf] // 2 if trend == 'mixed' else 0)
    sign = 1 if bullish else -1
    m15 = completed['M15'][-1]
    recent_ranges = [b['high'] - b['low'] for b in completed['M15'][-21:-1]]
    normal_range = sum(recent_ranges) / len(recent_ranges) if recent_ranges else 0
    displacement = normal_range > 0 and (m15['high'] - m15['low']) >= normal_range * 1.25
    close_location = (m15['close'] - m15['low']) / max(m15['high'] - m15['low'], 1e-12)
    directional_close = close_location >= .7 if bullish else close_location <= .3
    development = 4 if contexts['M15']['trend'] == ('bullish' if bullish else 'bearish') else 2 if contexts['M15']['trend'] == 'mixed' else 0
    development += 4 if sign * (m15['close'] - m15['open']) > 0 and displacement and directional_close else 0
    development += 2 if contexts['M15'].get('volatility_regime') in ('normal', 'high') else 0
    components['M15_development'] = development
    confirmed = setup is not None
    components['M5_confirmation'] = 20 if confirmed else (
        5 if contexts['M5']['trend'] == ('bullish' if bullish else 'bearish') else 2 if contexts['M5']['trend'] == 'mixed' else 0)
    patterns = (setup or {}).get('chart_patterns') or []
    components['price_action_confirmation'] = 5 if patterns else (
        2 if (setup or {}).get('order_block') else 0)
    m1a, m1b = completed['M1'][-2:]
    progress = sign * (m1b['close'] - m1a['close']) > 0
    rejection = (m1b['close'] - m1b['low']) > (m1b['high'] - m1b['close']) if bullish else (
        m1b['high'] - m1b['close']) > (m1b['close'] - m1b['low'])
    components['M1_timing'] = 10 if progress and rejection else 5 if progress else 0
    components['available_room'] = 5 if has_target else 0
    maximum = config['max_spread']
    components['execution_cost'] = max(0, round(5 * (1 - spread / maximum))) if maximum else 0
    components['data_quality'] = 5
    return components


def validate(snapshot, config):
    flags = list(snapshot.get('errors', []))
    required = ('expected_login expected_server expected_mode expected_currency timestamp_offset_seconds '
                'max_tick_age_seconds max_collection_seconds max_price_drift max_spread per_trade_cash_budget '
                'portfolio_cash_budget balance_risk_fraction max_margin_fraction cost_per_lot structural_tolerance buffer_atr_fraction '
                'trailing_risk_reduction_trigger_r trailing_risk_reduction_fraction trailing_break_even_trigger_r '
                'runner_enabled take_profit_close_fraction '
                'confirmation_expiry_bars volatility_low_ratio volatility_high_ratio '
                'zone_atr_fraction limit_entry_atr_fraction pattern_body_min_fraction '
                'order_block_lookback_bars minimum_zone_strength minimum_setup_score '
                'minimum_higher_timeframe_alignment minimum_ladder_reward_risk '
                'minimum_ladder_span_atr_fraction minimum_ladder_span_spread_multiple '
                'minimum_layer_stop_atr_fraction max_ladder_volume_ratio '
                'loss_cooldown_seconds max_consecutive_losses '
                'max_session_entries').split()
    for key in required:
        if config.get(key) is None:
            flags.append('Configure ' + key)
    reduction_trigger = config.get('trailing_risk_reduction_trigger_r')
    reduction_fraction = config.get('trailing_risk_reduction_fraction')
    break_even_trigger = config.get('trailing_break_even_trigger_r')
    if not isinstance(reduction_trigger, (int, float)) or not 0 < reduction_trigger < 1:
        flags.append('Invalid trailing_risk_reduction_trigger_r')
    if not isinstance(reduction_fraction, (int, float)) or not 0 < reduction_fraction < 1:
        flags.append('Invalid trailing_risk_reduction_fraction')
    if (not isinstance(break_even_trigger, (int, float)) or
            break_even_trigger < 1 or
            (isinstance(reduction_trigger, (int, float)) and
             break_even_trigger <= reduction_trigger)):
        flags.append('Invalid trailing_break_even_trigger_r')
    if not isinstance(config.get('runner_enabled'), bool):
        flags.append('Invalid runner_enabled')
    close_fraction = config.get('take_profit_close_fraction')
    if (not isinstance(close_fraction, (int, float)) or isinstance(close_fraction, bool) or
            not 0 < close_fraction < 1):
        flags.append('Invalid take_profit_close_fraction')
    alignment = config.get('minimum_higher_timeframe_alignment')
    if (not isinstance(alignment, int) or isinstance(alignment, bool) or
            not 0 <= alignment <= 3):
        flags.append('Invalid minimum_higher_timeframe_alignment')
    a = snapshot.get('account', {})
    for key, actual in [('login', a.get('login')), ('server', a.get('server')),
                        ('currency', a.get('currency')), ('mode', snapshot.get('account_mode'))]:
        if config.get('expected_' + key) != actual:
            flags.append('Account mismatch: ' + key)
    offset = config.get('timestamp_offset_seconds')
    tick = snapshot.get('tick', {})
    if not snapshot.get('terminal', {}).get('connected'):
        flags.append('Terminal disconnected')
    if not all(math.isfinite(tick.get(k, 0)) for k in ('bid', 'ask')) or not (0 < tick.get('bid', 0) <= tick.get('ask', 0)):
        flags.append('Invalid quote')
    spec = snapshot.get('specifications', {}).get(snapshot.get('symbol'), {})
    for key in ('trade_tick_size', 'volume_min', 'volume_step', 'volume_max', 'point'):
        if not isinstance(spec.get(key), (int, float)) or not math.isfinite(spec.get(key, 0)) or spec.get(key, 0) <= 0:
            flags.append('Invalid broker specification: ' + key)
    if offset is not None and tick:
        normalized = tick['time'] - offset
        age = snapshot['retrieval_end_utc'] - normalized
        if age < 0 or age > (config.get('max_tick_age_seconds') or 0):
            flags.append('Quote stale or future dated')
        snapshot['tick_time_normalized'] = normalized
    for tf, seconds in TIMEFRAMES.items():
        bars = snapshot.get('bars', {}).get(tf, [])
        if len(bars) < 161:
            flags.append(tf + ': insufficient bars (160 completed required)')
            continue
        if any(b['time'] <= a['time'] for a, b in zip(bars, bars[1:])):
            flags.append(tf + ': nonmonotonic bars')
        if any(not all(math.isfinite(b[k]) for k in ('open', 'high', 'low', 'close')) or
               not (0 < b['low'] <= min(b['open'], b['close']) <=
                    max(b['open'], b['close']) <= b['high']) for b in bars):
            flags.append(tf + ': inconsistent OHLC')
        if tick and bars[-1]['time'] // seconds != tick['time'] // seconds:
            flags.append(tf + ': history not synchronized with quote')
    if tick and tick.get('ask', 0) - tick.get('bid', 0) > (config.get('max_spread') or 0):
        flags.append('Spread exceeds configured limit')
    if snapshot['retrieval_end_utc'] - snapshot['retrieval_start_utc'] > (config.get('max_collection_seconds') or 0):
        flags.append('Collection exceeded time limit')
    initial = snapshot.get('initial_tick', {})
    if tick and initial and abs(tick['bid'] - initial['bid']) > (config.get('max_price_drift') or 0):
        flags.append('Price moved during collection; refresh required')
    return list(dict.fromkeys(flags))


def floor_volume(raw, step):
    if step <= 0 or raw < 0 or not math.isfinite(raw):
        raise ValueError('Invalid volume or broker step')
    return float((Decimal(str(raw)) / Decimal(str(step))).to_integral_value(rounding=ROUND_FLOOR) * Decimal(str(step)))


def evaluate(snapshot, config, broker, contexts=None):
    flags = validate(snapshot, config)
    tick = snapshot.get('tick', {})
    result = {
        'symbol': snapshot.get('symbol'), 'account_mode': snapshot.get('account_mode'),
        'snapshot_time': snapshot['retrieval_end_utc'], 'snapshot_id': snapshot['snapshot_id'],
        'session': market_session(snapshot.get('tick_time_normalized')), 'action': 'WAIT',
        'setup_id': None, 'setup_state': 'NO_SETUP', 'bid': tick.get('bid'), 'ask': tick.get('ask'),
        'spread': tick.get('ask', 0) - tick.get('bid', 0), 'buy_setup_score': 0,
        'sell_setup_score': 0, 'score_method': RULE_VERSION + '; heuristic setup score; not win probability',
        'score_components': {}, 'risk_currency': snapshot.get('account', {}).get('currency'),
        'positions_summary': {}, 'proposed_management_changes': [], 'reasons': [],
        'invalidation': None, 'next_trigger': 'Resolve configuration and data flags, then rescan',
        'data_quality': flags, 'news_status': 'unavailable',
        'execution_status': ('awaiting_authorization' if config.get('automatic_execution')
                             else 'advisory_only'),
    }
    positions = snapshot.get('positions')
    orders = snapshot.get('pending_orders')
    result['positions_summary'] = {'positions': positions, 'pending_orders': orders,
                                   'coverage': 'all discovered gold symbols'}
    if positions is None or orders is None:
        result['reasons'] = ['Exposure unavailable; inspect broker terminal immediately']
        return result
    groups = {}
    for direction, kind in [('BUY', 0), ('SELL', 1)]:
        group = [p for p in positions if p['type'] == kind]
        volume = sum(p['volume'] for p in group)
        groups[direction] = {'count': len(group), 'total_lots': volume,
            'weighted_entry': sum(p['volume'] * p['price_open'] for p in group) / volume if volume else None,
            'floating_profit': sum(p['profit'] for p in group),
            'unprotected_tickets': [p['ticket'] for p in group if p['sl'] <= 0]}
    result['positions_summary']['by_direction'] = groups
    if groups['BUY']['count'] and groups['SELL']['count']:
        flags.append('Unexpected hedge: BUY and SELL exposure')
    if flags:
        result['reasons'] = ['Entry blocked by data/account/configuration checks']
        if positions:
            result['reasons'].append('Existing exposure requires manual review while data is untrusted')
        return result
    tolerance = config['structural_tolerance']
    completed = {tf: bars[:-1] for tf, bars in snapshot['bars'].items()}
    contexts = contexts or {tf: structure(b, tolerance, config) for tf, b in completed.items()}
    result['structure'] = contexts
    buffer = max(result['spread'], contexts['M5']['atr14_simple'] * config['buffer_atr_fraction'], tolerance)
    zone_width = max(tolerance, contexts['M5']['atr14_simple']*config['zone_atr_fraction'])
    zone_as_of = tick.get('time')
    if not isinstance(zone_as_of, (int, float)):
        zone_as_of = completed['M5'][-1]['time'] + TIMEFRAMES['M5']
    zones = structural_zones(
        contexts, zone_width, config['minimum_zone_strength'], completed,
        zone_as_of, tolerance)
    active_target_zones = {
        'BUY': sorted((zone for zone in zones
                       if zone['status'] == 'active' and zone['kind'] == 'SUPPLY' and
                       zone['price'] > tick['ask']),
                      key=lambda zone: zone['price'])[:6],
        'SELL': sorted((zone for zone in zones
                        if zone['status'] == 'active' and zone['kind'] == 'DEMAND' and
                        zone['price'] < tick['bid']),
                       key=lambda zone: zone['price'], reverse=True)[:6],
    }
    result['chart_analysis'] = {
        'method': 'completed-candle naked-chart zones and patterns',
        'zone_width': zone_width,
        'zones': sorted(zones, key=lambda zone: abs(zone['price']-tick['bid']))[:12],
        # Keep target candidates separate from the chart's nearest-zone window;
        # an opposing zone may be farther away than the visible chart levels.
        'active_target_zones': active_target_zones,
        'daily_reference': {
            'time': completed['D1'][-1]['time'],
            'high': completed['D1'][-1]['high'],
            'low': completed['D1'][-1]['low'],
            'close': completed['D1'][-1]['close'],
            'role': 'macro context and major boundaries; never an entry trigger',
        },
    }
    # A protected runner can coexist with discovery of the next setup. Any
    # unprotected, foreign, TP1, legacy, or pending exposure still takes priority.
    if positions or orders:
        if any(order.get('magic') == config.get('execution_magic') and
               ('S:' in (order.get('comment') or '')) for order in orders):
            result['execution_status'] = 'broker_pending'
        from position_review import review
        management = review(snapshot, config, broker, contexts, buffer)
        result['positions_summary']['risk_by_ticket'] = management['risk_by_ticket']
        result['positions_summary']['actual_portfolio_cash_risk'] = management['actual_portfolio_cash_risk']
        result['positions_summary']['known_cash_risk'] = management['known_cash_risk']
        result['positions_summary']['planned_cash_risk'] = management['planned_cash_risk']
        result['positions_summary']['pending_cash_risk'] = management['pending_cash_risk']
        result['proposed_management_changes'] = management['changes']
        result['data_quality'].extend(management['alerts'])
        from exit_management import entry_exposure_allowed
        runner_entry_allowed = entry_exposure_allowed(
            positions, orders, config['execution_magic'])
        result['positions_summary']['new_entries_allowed'] = runner_entry_allowed
        result['positions_summary']['entry_exposure_state'] = (
            'protected_runners_only' if runner_entry_allowed else 'blocked')
        priority = {'CLOSE': 0, 'PARTIAL CLOSE': 1, 'MOVE SL TO BE': 2,
                    'REDUCE RISK': 3, 'MOVE SL': 4, 'TRAIL SL': 5}
        changes = management['changes']
        if not runner_entry_allowed:
            result['action'] = min(changes, key=lambda c: priority[c['action']])['action'] if changes else 'WAIT'
            result['reasons'] = ['Existing exposure takes priority; new entries blocked',
                                 'All management changes are proposals; actual protection is reported separately']
            result['next_trigger'] = (
                'Exposure change or next completed M5 candle after '
                f"{completed['M5'][-1]['time']}")
            return result
    bars = completed['M5']
    from setups import detect
    setup_config = {**config, '_m5_atr': contexts['M5']['atr14_simple'],
                    '_contexts': contexts, '_bars_by_timeframe': completed,
                    '_zone_invalidation_tolerance': tolerance}
    setups = detect(bars, buffer, tolerance, config['confirmation_expiry_bars'], setup_config)
    result['observed_setups'] = setups
    # Levels must have been confirmed before the two confirmation candles.
    prior = pivots(bars[:-2], tolerance)
    candidates = []
    for direction, side, sign, kind in [('BUY', 'high', 1, 0), ('SELL', 'low', -1, 1)]:
        previous, last = bars[-2:]
        matches = [s for s in setups if s['direction'] == direction and s['state'] == 'CONFIRMED']
        setup = matches[-1] if matches else None
        confirmed = setup is not None
        if not setup and not prior[side]:
            continue
        level = setup['level'] if setup else prior[side][-1]['price']
        entry = tick['ask'] if kind == 0 else tick['bid']
        invalidation = min(previous['low'], last['low']) if kind == 0 else max(previous['high'], last['high'])
        if setup:
            invalidation = setup['invalidation']
        spec = snapshot['specifications'][snapshot['symbol']]
        step = spec['trade_tick_size']
        stop_raw = invalidation - sign * buffer + (result['spread'] if kind == 1 else 0)
        stop = (math.floor(stop_raw / step) if kind == 0 else math.ceil(stop_raw / step)) * step
        planned_zone = (setup or {}).get('entry_zone') or {}
        conservative_entry = (planned_zone.get('high') if kind == 0 else
                              planned_zone.get('low'))
        target_entry = (conservative_entry + (result['spread'] if kind == 0 else 0)
                        if conservative_entry is not None else entry)
        targets = opposing_targets(
            zones, direction, target_entry, buffer, entry, result['spread'])
        components = directional_score(contexts, completed, direction, setup,
                                       bool(targets), result['spread'], config)
        score = sum(components.values())
        result['score_components'][direction] = components
        result['buy_setup_score' if kind == 0 else 'sell_setup_score'] = score
        if confirmed and targets and sign * (entry - stop) > 0:
            gated = {**setup, '_context_failures': context_gate(
                contexts, direction, setup, score, config)}
            candidates.append((direction, kind, level, entry, stop, targets, score, gated))
    result['setup_state'] = setups[-1]['state'] if setups and not candidates else 'WATCH_LEVEL'
    buy_level = prior['high'][-1]['price'] + buffer if prior['high'] else None
    sell_level = prior['low'][-1]['price'] - buffer if prior['low'] else None
    triggers = []
    if buy_level is not None:
        triggers.append(f"BUY watch: completed M5 close above {buy_level:.2f}, then a second close holds above it")
    if sell_level is not None:
        triggers.append(f"SELL watch: completed M5 close below {sell_level:.2f}, then a second close holds below it")
    result['reasons'] = ['Current M5 setup lacks fresh confirmation or structural room']
    chart_trigger = ('Alternative: completed M5 rejection/engulfing at confirmed supply or '
                     'demand, two soldiers/crows from that zone, a valid order-block retest, '
                     'or a pivot liquidity sweep')
    result['next_trigger'] = ('; '.join([*triggers, chart_trigger]) if triggers else
                              chart_trigger)
    if not candidates:
        return result
    evaluated = [_evaluate_candidate(snapshot, config, broker, dict(result), candidate, buffer)
                 for candidate in sorted(candidates, key=lambda c: c[6], reverse=True)]
    state_rank = {'ACTIONABLE': 6, 'ARMED_LIMIT': 5, 'WAIT_RETEST': 4,
                  'CONFIRMED': 3, 'WATCH_LEVEL': 2, 'FILTERED': 1,
                  'INVALIDATED': 0, 'EXPIRED': 0}
    chosen = max(evaluated, key=lambda item: (
        item['action'] in ('BUY', 'SELL'), state_rank.get(item.get('setup_state'), 0),
        item.get('buy_setup_score', 0) if item.get('planned_direction') == 'BUY'
        else item.get('sell_setup_score', 0)))
    chosen['candidate_checks'] = [dict(setup_id=item['setup_id'], state=item['setup_state'],
                                     action=item['action'], reasons=item['reasons'],
                                     metrics=item.get('entry_checks', {})) for item in evaluated]
    return chosen


def _evaluate_candidate(snapshot, config, broker, result, candidate, buffer):
    direction, kind, level, entry, stop, targets, score, setup = candidate
    sign = 1 if kind == 0 else -1
    result['setup_id'] = f"{snapshot['symbol']}:{setup['family']}:{direction}:{setup['confirmation_time']}:{level}"
    result['setup_family'] = setup['family']
    result['setup_state'] = 'CONFIRMED'
    result['invalidation'] = stop
    result['chart_confirmation'] = setup.get('chart_patterns') or []
    result['order_block'] = setup.get('order_block')
    result['structural_zone'] = setup.get('structural_zone')
    from setups import limit_entry_zone
    limit_width = max(
        buffer,
        (result.get('structure', {}).get('M5', {}).get('atr14_simple') or 0) *
        config['limit_entry_atr_fraction'])
    result['planned_entry_zone'] = (setup.get('entry_zone') or
                                    limit_entry_zone(level, direction, limit_width))
    result['planned_targets'] = targets
    result['planned_direction'] = direction
    result['entry_plan'] = {
        'rule_version': RULE_VERSION, 'setup_id': result['setup_id'],
        'symbol': snapshot['symbol'], 'direction': direction,
        'account': {key: config['expected_' + key] for key in
                    ('login', 'server', 'mode', 'currency')},
        'zone': result['planned_entry_zone'], 'level': level,
        'setup_family': setup['family'],
        'structural_invalidation': setup['invalidation'],
        'stop': stop, 'targets': targets,
        'confirmation_time': setup['confirmation_time'],
        'expires_at': (setup['confirmation_time'] +
                       (config['confirmation_expiry_bars']+2)*300 -
                       config['timestamp_offset_seconds']),
        'risk_fraction': config['balance_risk_fraction'],
        'cost_per_lot': config['cost_per_lot'],
        'm5_atr': (result.get('structure', {}).get('M5', {}).get('atr14_simple') or 0),
    }
    from limit_ladder import attach_limit_ladder
    result['entry_plan'] = attach_limit_ladder(
        result['entry_plan'], snapshot['tick']['bid'], snapshot['tick']['ask'], config)
    # Structure is measured on bid candles; executable entry still uses ask for BUY.
    structural_price = snapshot['tick']['bid']
    if sign * (structural_price - setup['invalidation']) <= 0:
        result['setup_state'] = 'INVALIDATED'
        result['reasons'] = [f"Live bid {structural_price:.2f} breached setup invalidation {setup['invalidation']:.2f}"]
        result['next_trigger'] = 'Wait for a new completed M5 setup'
        return result
    if sign * (structural_price - level) <= 0:
        result['setup_state'] = 'WATCH_LEVEL'
        result['reasons'] = [f"Live bid {structural_price:.2f} no longer holds the confirmed side of {level:.2f}"]
        result['next_trigger'] = f"Require renewed completed M5 confirmation {'above' if sign == 1 else 'below'} {level:.2f}"
        return result
    # A completed reclaim cannot be undone by an intrabar bounce alone.
    if any(b['time'] > setup['confirmation_time'] and sign * (b['close'] - level) <= 0
           for b in snapshot['bars']['M5'][:-1]):
        result['setup_state'] = 'INVALIDATED'
        result['reasons'] = [f"Completed M5 candle reclaimed setup level {level:.2f}"]
        result['next_trigger'] = f"Wait for a new M5 confirmation at {level:.2f}"
        return result
    context_failures = setup.get('_context_failures') or []
    if context_failures:
        result['setup_state'] = 'FILTERED'
        result['reasons'] = ['Context gate: ' + failure for failure in context_failures[:3]]
        result['next_trigger'] = 'Wait for a new setup with aligned higher-timeframe structure and sufficient quality'
        return result
    ladder_span = result['entry_plan'].get('limit_ladder_span')
    required_span = result['entry_plan'].get('minimum_limit_ladder_span')
    if (not isinstance(ladder_span, (int, float)) or
            not isinstance(required_span, (int, float)) or
            ladder_span+1e-9 < required_span):
        result['setup_state'] = 'FILTERED'
        result['reasons'] = [
            f'Limit ladder span {ladder_span or 0:.2f} is below the required {required_span or 0:.2f}']
        result['next_trigger'] = 'Wait for a setup with a wider grounded entry range'
        return result
    planned_rr = planned_ladder_reward_risk(result['entry_plan'])
    result['entry_checks'] = {
        'planned_ladder_reward_risk': planned_rr,
        'minimum_ladder_reward_risk': config['minimum_ladder_reward_risk'],
    }
    if planned_rr is None or planned_rr < config['minimum_ladder_reward_risk']:
        result['setup_state'] = 'FILTERED'
        result['reasons'] = [
            ('Limit ladder reward/risk is unavailable' if planned_rr is None else
             f'Limit ladder reward/risk {planned_rr:.2f} is below '
             f'{config["minimum_ladder_reward_risk"]:.2f}')]
        result['next_trigger'] = 'Wait for a fresh setup with more structural room to an active target'
        return result
    zone = result['planned_entry_zone']
    result['entry_checks'].update({
        'structural_price': structural_price, 'setup_level': level,
        'entry_method': 'broker_limit_retest', 'entry_zone': zone,
        'setup_family': setup['family'],
        'chart_patterns': result['chart_confirmation'],
        'order_block': result['order_block'],
    })
    retest_time = confirmed_retest(
        snapshot['bars']['M1'][:-1], zone, direction, setup['confirmation_time'])
    if retest_time is None:
        result['setup_state'] = 'WAIT_RETEST'
        result['reasons'] = [
            f'{setup["family"]}: completed M5 setup passed context filters',
            f'Waiting for a completed M1 retest and rejection inside {zone["low"]:.2f}-{zone["high"]:.2f}',
            'No broker order is submitted before the rejection candle closes',
        ]
        result['next_trigger'] = (
            f'Completed M1 {direction} rejection at {zone["low"]:.2f}-{zone["high"]:.2f}; '
            f'cancel on M5 reclaim or invalidation {stop:.2f}')
        return result
    result['entry_plan']['retest_confirmation_time'] = retest_time
    result['entry_checks']['retest_confirmation_time'] = retest_time
    from entry_plan import quote_blocker
    blocked = quote_blocker(result['entry_plan'], snapshot['tick']['bid'],
                            snapshot['tick']['ask'], snapshot['retrieval_end_utc'],
                            config['max_spread'])
    if blocked and blocked != 'outside_entry_zone':
        result['setup_state'] = 'EXPIRED' if blocked == 'plan_expired' else 'INVALIDATED'
        result['reasons'] = ['Entry plan blocked: ' + blocked]
        return result
    result['setup_state'] = 'ARMED_LIMIT'
    pattern_text = ', '.join(label.replace('_', ' ') for label in result['chart_confirmation'])
    result.update(
        action='WAIT', stop=stop, targets=targets,
        reasons=[setup['family'] + ': completed M5 setup and M1 retest rejection confirmed',
                 ('Chart confirmation: ' + pattern_text if pattern_text else
                  'Confirmed structural retest at support/resistance zone'),
                 'Only still-pending, adequately protected ladder layers may be submitted'],
        next_trigger=(f"Broker {direction}_LIMIT ladder at {zone['low']:.2f}-{zone['high']:.2f}; "
                      f"cancel on M5 reclaim or invalidation {stop:.2f}"))
    return result
