"""Price-only validation of independent plans, without deterministic scores/actions.

v29 requires a completed M5 pattern at a grounded level, a reachable structural entry,
and a stop beyond the thesis invalidation. No prices are repaired or widened.
"""
import math

from scan_engine import atr
from setups import candle_patterns


VERSION = 'independent-auto-29'
FRAMES = ('M5', 'M15', 'M30', 'H1', 'H4', 'D1')
INTRADAY_TARGET_FRAMES = ('M5', 'M15', 'M30', 'H1')


def _percentile(values, fraction=.8):
    values = sorted(value for value in values if math.isfinite(value) and value >= 0)
    if not values:
        return 0.0
    return values[min(len(values)-1, math.ceil(len(values)*fraction)-1)]


def entry_reachability(decision, direction, entry, expiry_minutes, atr_value, config):
    """Bound a short-lived limit by volatility and observed M1 pullbacks."""
    if direction not in ('BUY', 'SELL'):
        raise ValueError('Independent reachability requires BUY or SELL')
    if (isinstance(expiry_minutes, bool) or not isinstance(expiry_minutes, int) or
            not 5 <= expiry_minutes <= 30):
        raise ValueError('Independent reachability requires a 5-30 minute expiry')
    if not isinstance(atr_value, (int, float)) or not math.isfinite(atr_value) or atr_value <= 0:
        raise ValueError('Independent reachability requires M5 ATR')
    bid, ask = decision.get('bid'), decision.get('ask')
    if not all(isinstance(value, (int, float)) and math.isfinite(value)
               for value in (bid, ask, entry)) or not 0 < bid <= ask:
        raise ValueError('Independent reachability requires a fresh quote')
    quote = ask if direction == 'BUY' else bid
    distance = quote-entry if direction == 'BUY' else entry-quote
    if distance <= 0:
        raise ValueError('Independent limit is no longer on the valid side of market')

    completed_m1 = ((decision.get('chart_data') or {}).get('M1') or [])[:-1]
    lookback = int(config.get('independent_reachability_m1_lookback', 120))
    horizon = expiry_minutes
    sample = completed_m1[-(lookback+horizon+1):]
    excursions = []
    for index in range(max(0, len(sample)-lookback-horizon), len(sample)-horizon):
        start = sample[index].get('close')
        future = sample[index+1:index+1+horizon]
        if not isinstance(start, (int, float)) or len(future) < horizon:
            continue
        if direction == 'SELL':
            excursions.append(max(0.0, max(bar['high'] for bar in future)-start))
        else:
            excursions.append(max(0.0, start-min(bar['low'] for bar in future)))
    if len(excursions) < 5:
        raise ValueError('Independent M1 reachability history is unavailable')

    base = float(config.get('independent_reachability_base_atr', .75))
    per_minute = float(config.get('independent_reachability_per_minute_atr', .025))
    floor = float(config.get('independent_reachability_floor_atr', .85))
    empirical_multiplier = float(
        config.get('independent_reachability_empirical_multiplier', 1.25))
    spread = ask-bid
    hard_cap = atr_value*(base+per_minute*expiry_minutes)
    empirical = _percentile(excursions)
    allowed = min(hard_cap, max(atr_value*floor, spread*2,
                                empirical*empirical_multiplier))
    result = {
        'distance': distance, 'maximum_distance': allowed,
        'distance_atr': distance/atr_value,
        'maximum_distance_atr': allowed/atr_value,
        'recent_m1_pullback_p80': empirical,
        'expiry_minutes': expiry_minutes,
    }
    if distance > allowed+1e-9:
        raise ValueError(
            'independent_entry_not_reachable: limit distance '
            f'{distance:.2f} exceeds {allowed:.2f} for {expiry_minutes} minutes')
    return result


def validate_market_freshness(decision, atr_value, config):
    """Reject rollover and a newly opened M5 gap until fresh bars accumulate."""
    if decision.get('session') == 'Rollover / closed-market window':
        raise ValueError('independent_market_not_fresh: rollover or closed-market window')
    completed = ((decision.get('chart_data') or {}).get('M5') or [])[:-1]
    cooldown = int(config.get('independent_gap_cooldown_m5_bars', 3))
    multiple = float(config.get('independent_gap_atr_multiple', 1.5))
    if len(completed) < cooldown+2:
        raise ValueError('Independent M5 gap history is unavailable')
    for index in range(max(1, len(completed)-cooldown), len(completed)):
        gap = abs(completed[index]['open']-completed[index-1]['close'])
        bars_after = len(completed)-1-index
        if gap > atr_value*multiple and bars_after < cooldown:
            raise ValueError(
                'independent_market_not_fresh: recent M5 gap requires fresh completed bars')
    return True


def confirmation_evidence(bars, direction, level, tolerance, countertrend=False):
    """bars contains completed M5 candles only; never accept an M1-only trigger."""
    if len(bars) < 3:
        raise ValueError('Independent confirmation requires completed M5 history')
    sign = 1 if direction == 'BUY' else -1
    previous, latest = bars[-2:]
    patterns = candle_patterns(bars, direction)
    if sign*(latest['close']-level) <= 0 or sign*(latest['close']-latest['open']) <= 0:
        raise ValueError('Independent latest completed M5 does not confirm direction')
    touched = any(b['low']-tolerance <= level <= b['high']+tolerance for b in bars[-2:])
    crossed = sign*(previous['close']-level) <= 0 < sign*(latest['close']-level)
    if not patterns or not (touched or crossed):
        raise ValueError('Independent M5 pattern is missing or detached from confirmation level')
    reversal = ('bullish_engulfing' if sign == 1 else 'bearish_engulfing') in patterns or (
        'bullish_rejection' if sign == 1 else 'bearish_rejection') in patterns
    reversal_break = sign*(latest['close']-(previous['high'] if sign == 1 else previous['low'])) > 0
    if countertrend and not (reversal and reversal_break):
        raise ValueError('Independent countertrend plan requires M5 reversal and prior-bar break')
    return {'time': int(latest['time']), 'patterns': patterns, 'countertrend': countertrend,
            'pattern_extreme': min(b['low'] for b in bars[-2:]) if sign == 1
            else max(b['high'] for b in bars[-2:])}


def context_conflict(chart_data, direction):
    """Use raw completed higher-timeframe price movement, not scanner bias labels."""
    sign = 1 if direction == 'BUY' else -1
    conflicts = []
    for frame in ('H1', 'H4', 'D1'):
        bars = (chart_data.get(frame) or [])[:-1]
        if len(bars) < 3:
            raise ValueError('Independent higher-timeframe history is unavailable')
        if sign*(bars[-1]['close']-bars[-3]['close']) < 0:
            conflicts.append(frame)
    return conflicts


def supported_entry(decision, direction, price, tolerance, higher_only=False):
    """Executable BUY prices map back to bid-chart structure using spread."""
    bid_price = price-(decision.get('spread') or 0) if direction == 'BUY' else price
    frames = FRAMES[1:] if higher_only else FRAMES
    kind, side = ('DEMAND', 'low') if direction == 'BUY' else ('SUPPLY', 'high')
    for zone in (decision.get('chart_analysis') or {}).get('zones', []):
        sources = zone.get('active_sources') or zone.get('sources') or []
        if (zone.get('status') == 'active' and zone.get('kind') == kind and
                any(frame in sources for frame in frames) and
                zone['low']-tolerance <= bid_price <= zone['high']+tolerance):
            return True
    for frame in frames:
        points = (((decision.get('structure') or {}).get(frame) or {}).get('pivots') or {}).get(side, [])
        bars = ((decision.get('chart_data') or {}).get(frame) or [])[:-1]
        sign = 1 if direction == 'BUY' else -1
        for point in points[-6:]:
            confirmed = point.get('confirmed_bar')
            if (confirmed is not None and bars and bars[0]['time'] <= confirmed <= bars[-1]['time'] and
                    abs(bid_price-point['price']) <= tolerance and
                    not any(sign*(b['close']-point['price']) < -tolerance for b in bars
                            if b['time'] >= confirmed)):
                return True
    return False


def validate_independent_geometry(plan, spread, atr_value, config):
    sign = 1 if plan['direction'] == 'BUY' else -1
    stop, invalid = plan['stop'], plan['structural_invalidation']
    if sign*(invalid-stop) < -1e-9:
        raise ValueError('Independent stop is inside structural invalidation')
    if not isinstance(atr_value, (int, float)) or not math.isfinite(atr_value) or atr_value <= 0:
        raise ValueError('Independent M5 volatility evidence is unavailable')
    minimum = max(spread*2, atr_value*max(.5, config.get('minimum_layer_stop_atr_fraction', .25)))
    if sign*(plan['pending_price']-stop) < minimum-1e-9:
        raise ValueError('Independent stop distance is too small for M5 volatility/spread')
    return minimum


def validate_independent_evidence(source, live, raw, config):
    direction = raw['direction']
    sign = 1 if direction == 'BUY' else -1
    tolerance = max(config.get('structural_tolerance', .1), source.get('spread') or 0, .01)
    bars = (source.get('chart_data') or {}).get('M5', [])[:-1][-24:]
    atr_value = atr(bars)
    validate_market_freshness(source, atr_value, config)
    validate_market_freshness(live, atr_value, config)
    conflicts = context_conflict(source.get('chart_data') or {}, direction)
    conflicts = sorted(set(conflicts + context_conflict(live.get('chart_data') or {}, direction)))
    proof = confirmation_evidence(bars, direction, raw['confirmation_level'], tolerance, bool(conflicts))
    proof['conflicting_timeframes'] = conflicts
    # Compare the confirmed candle with the fresh dataset; later closes cannot reclaim it.
    live_bars = (live.get('chart_data') or {}).get('M5', [])[:-1]
    original = next((b for b in live_bars if b['time'] == proof['time']), None)
    if original != bars[-1]:
        raise ValueError('Independent confirmation candle changed or is unavailable')
    if any(sign*(b['close']-raw['confirmation_level']) <= 0 for b in live_bars
           if b['time'] > proof['time']):
        raise ValueError('Independent confirmation has been reclaimed on completed M5')
    def supported(price):
        return all(supported_entry(d, direction, price, tolerance, bool(conflicts)) for d in (source, live))
    if not supported(raw['entry_price']):
        raise ValueError('Independent entry lacks active M5+ structure (M15+ for countertrend)')
    reachability = entry_reachability(
        live, direction, raw['entry_price'], raw['expires_in_minutes'], atr_value, config)
    plan = {'direction': direction, 'pending_price': raw['entry_price'],
            'stop': raw['stop_loss'], 'structural_invalidation': raw['structural_invalidation']}
    minimum = validate_independent_geometry(plan, live['ask']-live['bid'], atr_value, config)
    pattern_boundary = proof['pattern_extreme']+(live['ask']-live['bid'] if sign == -1 else 0)
    if sign*(pattern_boundary-raw['stop_loss']) < -1e-9:
        raise ValueError('Independent stop is inside its completed M5 confirmation pattern')
    # Additional layers require distinct confirmed prices, never quote interpolation.
    candidates = [float(raw['entry_price'])]
    side = 'low' if direction == 'BUY' else 'high'
    spread = live['ask']-live['bid'] if direction == 'BUY' else 0
    for frame in FRAMES:
        points = (((source.get('structure') or {}).get(frame) or {}).get('pivots') or {}).get(side, [])
        for point in points[-6:]:
            price = float(point['price'])+spread
            if (0 < sign*(raw['entry_price']-price) <= atr_value and
                    sign*(price-raw['stop_loss']) >= minimum and supported(price)):
                candidates.append(price)
    candidates = sorted(set(candidates), reverse=direction == 'BUY')
    proof['entry_reachability'] = reachability
    return proof, candidates, atr_value


def broker_independent_blocker(m, symbol, plan, tick, config):
    """Reproduce source confirmation and current HTF context immediately before send."""
    if plan.get('rule_version') != VERSION:
        return None
    try:
        chart = {}
        for frame in ('M1', 'M5', 'H1', 'H4', 'D1'):
            rates = m.copy_rates_from_pos(symbol, getattr(m, 'TIMEFRAME_'+frame), 0, 80)
            if rates is None or len(rates) < 16:
                return 'independent_confirmation_unavailable'
            chart[frame] = [{k: float(b[k]) if k != 'time' else int(b[k])
                             for k in ('time', 'open', 'high', 'low', 'close')} for b in rates]
        if chart['M5'][-1]['time']//300 != tick.time//300:
            return 'unsynchronized_structure'
        confirmed = [b for b in chart['M5'][:-1] if b['time'] <= plan['confirmation_time']]
        if not confirmed or confirmed[-1]['time'] != plan['confirmation_time']:
            return 'independent_confirmation_unavailable'
        sign = 1 if plan['direction'] == 'BUY' else -1
        if any(b['time'] > plan['confirmation_time'] and sign*(b['close']-plan['level']) <= 0
               for b in chart['M5'][:-1]):
            return 'completed_m5_reclaim'
        tolerance = max(config.get('structural_tolerance', .1), tick.ask-tick.bid, .01)
        conflicts = context_conflict(chart, plan['direction'])
        proof = confirmation_evidence(confirmed, plan['direction'], plan['level'], tolerance,
                                      bool(conflicts or plan['confirmation_evidence']['countertrend']))
        validate_market_freshness({
            'session': plan.get('source_session'), 'chart_data': chart},
            atr(chart['M5'][:-1]), config)
        entry_reachability(
            {'bid': tick.bid, 'ask': tick.ask, 'chart_data': chart},
            plan['direction'], plan['pending_price'], plan['expiry_minutes'],
            atr(chart['M5'][:-1]), config)
        validate_independent_geometry(plan, tick.ask-tick.bid, atr(chart['M5'][:-1]), config)
        pattern_boundary = proof['pattern_extreme']+(tick.ask-tick.bid if sign == -1 else 0)
        if sign*(pattern_boundary-plan['stop']) < -1e-9:
            return 'independent_stop_inside_confirmation'
        if not plan.get('verified_entry_prices') or any(
                layer['price'] not in plan['verified_entry_prices'] for layer in plan.get('limit_layers', [])):
            return 'independent_unverified_layer'
    except (KeyError, TypeError, ValueError, AttributeError):
        return 'independent_confirmation_failed'
    return None
