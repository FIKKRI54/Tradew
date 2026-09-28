"""Deterministic naked-chart setups evaluated only on completed candles."""
from scan_engine import TIMEFRAMES, atr, pivots, structural_zones


def _direction(bar):
    return 1 if bar['close'] > bar['open'] else -1 if bar['close'] < bar['open'] else 0


def _body_fraction(bar):
    return abs(bar['close']-bar['open']) / max(bar['high']-bar['low'], 1e-12)


def candle_patterns(bars, direction, body_min_fraction=.5):
    """Return objective completed-candle pattern labels for one direction."""
    if len(bars) < 2:
        return []
    sign = 1 if direction == 'BUY' else -1
    previous, latest = bars[-2:]
    patterns = []
    previous_body = sorted((previous['open'], previous['close']))
    latest_body = sorted((latest['open'], latest['close']))
    if (_direction(previous) == -sign and _direction(latest) == sign and
            latest_body[0] <= previous_body[0] and latest_body[1] >= previous_body[1]):
        patterns.append('bullish_engulfing' if sign == 1 else 'bearish_engulfing')
    candle_range = max(latest['high']-latest['low'], 1e-12)
    upper_wick = latest['high']-max(latest['open'], latest['close'])
    lower_wick = min(latest['open'], latest['close'])-latest['low']
    rejection_wick = lower_wick if sign == 1 else upper_wick
    opposing_wick = upper_wick if sign == 1 else lower_wick
    if (_direction(latest) == sign and rejection_wick >= max(
            abs(latest['close']-latest['open']), opposing_wick)*1.5):
        patterns.append('bullish_rejection' if sign == 1 else 'bearish_rejection')
    for count, label in ((3, 'three_soldiers' if sign == 1 else 'three_crows'),
                         (2, 'two_soldiers' if sign == 1 else 'two_crows')):
        if len(bars) < count:
            continue
        sequence = bars[-count:]
        directional = all(_direction(bar) == sign and
                          _body_fraction(bar) >= body_min_fraction for bar in sequence)
        progressing = all(sign*(b['close']-a['close']) > 0
                          for a, b in zip(sequence, sequence[1:]))
        if directional and progressing:
            patterns.append(label)
            break
    recent = bars[-12:-2]
    normal_range = (sum(bar['high']-bar['low'] for bar in recent)/len(recent)
                    if recent else 0)
    close_location = (latest['close']-latest['low'])/candle_range
    directional_close = close_location >= .7 if sign == 1 else close_location <= .3
    if (normal_range > 0 and candle_range >= normal_range*1.25 and
            _direction(latest) == sign and directional_close):
        patterns.append('bullish_displacement' if sign == 1 else 'bearish_displacement')
    return patterns


def order_block(bars, direction, lookback=8):
    """Find the last opposite candle preceding the directional confirmation."""
    if len(bars) < 3:
        return None
    sign = 1 if direction == 'BUY' else -1
    end = len(bars)-1
    for index in range(end-1, max(-1, end-int(lookback)-1), -1):
        candle = bars[index]
        if _direction(candle) != -sign:
            continue
        following = bars[index+1:end+1]
        displaced = (max(bar['high'] for bar in following) > candle['high']
                     if sign == 1 else
                     min(bar['low'] for bar in following) < candle['low'])
        if displaced:
            return {
                'direction': direction, 'time': candle['time'],
                'low': candle['low'], 'high': candle['high'],
                'body_low': min(candle['open'], candle['close']),
                'body_high': max(candle['open'], candle['close']),
            }
    return None


def limit_entry_zone(level, direction, width):
    """Return the post-break retest zone on the confirmed side of a level."""
    width = max(float(width), 0)
    if direction == 'BUY':
        return {'low': level, 'high': level+width, 'trigger_side': 'BID', 'direction': direction}
    return {'low': level-width, 'high': level, 'trigger_side': 'BID', 'direction': direction}


def _zones_known_by(contexts, candle_end, width, minimum_strength,
                    bars_by_timeframe=None, invalidation_tolerance=0):
    """Use only pivots confirmed before this setup candle opened."""
    known = {}
    for timeframe, seconds in TIMEFRAMES.items():
        context = contexts.get(timeframe) or {}
        pivot_sides = context.get('pivots') or {}
        known[timeframe] = {'pivots': {
            side: [point for point in pivot_sides.get(side, [])
                   if isinstance(point.get('confirmed_bar'), (int, float)) and
                   point['confirmed_bar'] + seconds <= candle_end]
            for side in ('high', 'low')
        }}
    return structural_zones(known, width, minimum_strength, bars_by_timeframe,
                            candle_end, invalidation_tolerance)


def _touches(bar, low, high):
    return bar['low'] <= high and bar['high'] >= low


def _zone_setup(bars, index, direction, patterns, zones, tolerance):
    """Require a named candle pattern at a previously confirmed S/D zone."""
    a, b = bars[index-1:index+1]
    sign = 1 if direction == 'BUY' else -1
    kind = 'DEMAND' if sign == 1 else 'SUPPLY'
    side = 'high' if sign == 1 else 'low'
    rejecting = (('bullish_rejection' if sign == 1 else 'bearish_rejection') in patterns or
                 ('bullish_engulfing' if sign == 1 else 'bearish_engulfing') in patterns)
    soldiers = ('two_soldiers' if sign == 1 else 'two_crows') in patterns
    choices = []
    for index, zone in enumerate(zones):
        if zone['kind'] != kind:
            continue
        far_edge = zone['low'] if sign == 1 else zone['high']
        if zone.get('status') != 'active':
            continue
        latest_time = zone.get('latest_time')
        if (not zone.get('activity_checked') and latest_time is not None and
                any(sign*(bar['close']-far_edge) < -tolerance for bar in bars
                    if latest_time <= bar['time'] < b['time'])):
            # A completed close through the far edge consumes this zone. A
            # newly confirmed pivot is needed before it can support a plan.
            continue
        edge = zone[side]
        if sign*(b['close']-edge) <= tolerance or _direction(b) != sign:
            continue
        if rejecting and _touches(b, zone['low'], zone['high']):
            family = 'demand_rejection' if sign == 1 else 'supply_rejection'
        elif (soldiers and _touches(a, zone['low'], zone['high']) and
              sign*(a['close']-edge) > 0):
            family = 'demand_two_soldiers' if sign == 1 else 'supply_two_crows'
        else:
            continue
        choices.append((abs(b['close']-edge), -zone['strength'], index, family, zone))
    if not choices:
        return None
    _, _, _, family, zone = min(choices)
    invalidation = (min(a['low'], b['low'], zone['low']) if sign == 1 else
                    max(a['high'], b['high'], zone['high']))
    return {
        'family': family, 'level': zone[side], 'invalidation': invalidation,
        'entry_zone': {'low': zone['low'], 'high': zone['high'],
                       'trigger_side': 'BID', 'direction': direction},
        'structural_zone': zone,
    }


def _order_block_setup(bars, index, direction, patterns, lookback, tolerance,
                       body_min):
    """Retest a block displaced before the confirmation candle."""
    b = bars[index]
    sign = 1 if direction == 'BUY' else -1
    block = order_block(bars[:index], direction, lookback)
    if not block or not _touches(b, block['low'], block['high']):
        return None
    far_edge = block['low'] if sign == 1 else block['high']
    near_edge = block['high'] if sign == 1 else block['low']
    prior_move = [bar for bar in bars if block['time'] < bar['time'] < b['time']]
    if not any(sign*(bar['close']-near_edge) > tolerance and
               _direction(bar) == sign and _body_fraction(bar) >= body_min
               for bar in prior_move):
        # A wick through an opposite candle is not a displaced order block.
        return None
    if any(sign*(bar['close']-far_edge) < -tolerance for bar in bars
           if block['time'] < bar['time'] < b['time']):
        return None
    edge = near_edge
    close_location = (b['close']-b['low']) / max(b['high']-b['low'], 1e-12)
    strong_close = close_location >= .7 if sign == 1 else close_location <= .3
    named_pattern = any(label in patterns for label in (
        'bullish_engulfing' if sign == 1 else 'bearish_engulfing',
        'bullish_rejection' if sign == 1 else 'bearish_rejection'))
    if (sign*(b['close']-edge) <= tolerance or _direction(b) != sign or
            not strong_close or not (named_pattern or _body_fraction(b) >= body_min)):
        return None
    return {
        'family': 'order_block_retest', 'level': edge,
        'invalidation': (min(b['low'], block['low']) if sign == 1 else
                         max(b['high'], block['high'])),
        'entry_zone': {'low': block['low'], 'high': block['high'],
                       'trigger_side': 'BID', 'direction': direction},
        'order_block': block,
    }


def detect(bars, buffer, tolerance, expiry, config=None):
    found = []
    config = config or {}
    body_min = config.get('pattern_body_min_fraction', .5)
    block_lookback = int(config.get('order_block_lookback_bars', 8))
    contexts = config.get('_contexts') or {}
    # Replay a bounded suffix: each trigger uses only pivots known before that candle.
    start = max(8, len(bars) - int(expiry) - 2)
    for i in range(start, len(bars)):
        prior = pivots(bars[:i-1], tolerance)
        single_prior = pivots(bars[:i], tolerance)
        a, b = bars[i-1:i+1]
        signal_atr = atr(bars[:i+1]) or config.get('_m5_atr') or 0
        entry_width = max(buffer, config.get('limit_entry_atr_fraction', .25) * signal_atr)
        zone_width = max(tolerance, config.get('zone_atr_fraction', .25) * signal_atr)
        zones = _zones_known_by(
            contexts, b['time'], zone_width,
            config.get('minimum_zone_strength', 2),
            config.get('_bars_by_timeframe'),
            config.get('_zone_invalidation_tolerance', tolerance)) if contexts else []
        for direction, side, opposite, sign in [('BUY', 'high', 'low', 1), ('SELL', 'low', 'high', -1)]:
            evidence = candle_patterns(bars[:i+1], direction, body_min)
            template = None
            swept_points = single_prior[opposite]
            if swept_points:
                swept = swept_points[-1]['price']
                wick = b['low'] < swept-buffer if sign == 1 else b['high'] > swept+buffer
                reclaim = sign*(b['close']-swept) > tolerance
                rejection = ('bullish_rejection' if sign == 1 else
                             'bearish_rejection') in evidence
                engulfing = ('bullish_engulfing' if sign == 1 else
                             'bearish_engulfing') in evidence
                if wick and reclaim and _direction(b) == sign and (rejection or engulfing):
                    template = {
                        'family': 'sweep_reversal', 'level': swept,
                        'invalidation': b['low'] if sign == 1 else b['high'],
                        'entry_zone': limit_entry_zone(swept, direction, entry_width),
                    }
            if template is None and zones:
                template = _zone_setup(bars, i, direction, evidence, zones, tolerance)
            if template is None:
                template = _order_block_setup(
                    bars, i, direction, evidence, block_lookback, tolerance, body_min)

            family = None
            # A crossing, followed by acceptance. Avoid renewing an old trigger forever.
            if template is None and prior[side] and prior[opposite]:
                level = prior[side][-1]['price']
                if sign*(bars[i-2]['close']-level) <= buffer and sign*(a['close']-level) > buffer and sign*(b['close']-level) > buffer:
                    family = 'breakout_follow_through'
                touch = b['low'] <= level+buffer if sign == 1 else b['high'] >= level-buffer
                if sign*(a['close']-level) > buffer and touch and sign*(b['close']-level) > buffer and sign*(b['close']-b['open']) > 0:
                    family = 'rejected_retest'
                swings = prior[opposite]
                if len(swings) >= 2 and sign*(swings[-1]['price']-swings[-2]['price']) > tolerance and sign*(a['close']-level) <= buffer and sign*(b['close']-level) > buffer:
                    family = 'pullback_continuation'
                if family:
                    block = order_block(bars[:i+1], direction, block_lookback)
                    invalidation = min(a['low'], b['low']) if sign == 1 else max(a['high'], b['high'])
                    if block:
                        invalidation = (min(invalidation, block['low']) if sign == 1 else
                                        max(invalidation, block['high']))
                    template = {
                        'family': family, 'level': level, 'invalidation': invalidation,
                        'entry_zone': limit_entry_zone(level, direction, entry_width),
                        'order_block': block,
                    }
            if template:
                subsequent = bars[i+1:]
                invalid = any(sign*(x['close']-template['invalidation']) < 0 for x in subsequent)
                age = len(bars)-1-i
                found.append({'direction': direction, **template,
                    'confirmation_time': b['time'],
                    'confirmation_close': b['close'], 'originating_timeframe': 'M5',
                    'chart_patterns': evidence,
                    'state': 'INVALIDATED' if invalid else ('EXPIRED' if age > expiry else 'CONFIRMED'),
                    'age_bars': age})
    return found
