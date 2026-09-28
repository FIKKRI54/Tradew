"""Deterministic planning for explicitly confirmed manual portfolio actions."""
import hashlib
import json
import math
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP


ACTION_VERSION = 'portfolio-half-close-1'


def _field(item, name, default=None):
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def _volume_units(value, step):
    value, step = Decimal(str(value)), Decimal(str(step))
    units = int((value/step).to_integral_value(rounding=ROUND_HALF_UP))
    if units < 0 or abs(value-Decimal(units)*step) > step/Decimal('100'):
        raise ValueError('Position volume is not aligned to the broker volume step')
    return units


def exposure_fingerprint(account, symbol, positions):
    """Bind confirmation to stable account and position identity/volume fields."""
    payload = {
        'version': ACTION_VERSION,
        'account': {
            key: _field(account, key) for key in ('login', 'server', 'currency', 'trade_mode')
        },
        'symbol': symbol,
        'positions': sorted([
            {
                key: _field(position, key) for key in
                ('ticket', 'symbol', 'type', 'volume', 'price_open', 'magic', 'comment')
            }
            for position in positions if _field(position, 'symbol') == symbol
        ], key=lambda item: item['ticket']),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                         allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def build_half_close_plan(account, symbol, positions, volume_min, volume_step, magic):
    """Plan a broker-step-valid close of 50% aggregate symbol volume.

    Tickets are interleaved across their chronological sequence so an equal-size
    book realizes a representative half instead of only its oldest or newest end.
    External/manual positions are intentionally eligible for this explicit action.
    """
    if not isinstance(symbol, str) or not symbol:
        raise ValueError('A configured symbol is required')
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or
           not math.isfinite(value) or value <= 0 for value in (volume_min, volume_step)):
        raise ValueError('Positive broker volume limits are required')
    eligible = [position for position in positions
                if _field(position, 'symbol') == symbol and
                isinstance(_field(position, 'volume'), (int, float)) and
                _field(position, 'volume') > 0]
    if not eligible:
        raise ValueError('No open positions exist for the configured symbol')

    step = Decimal(str(volume_step))
    minimum_units = _volume_units(volume_min, volume_step)
    total_units = sum(_volume_units(_field(position, 'volume'), volume_step)
                      for position in eligible)
    target_units = int((Decimal(total_units)/2).to_integral_value(rounding=ROUND_FLOOR))
    if target_units < minimum_units:
        raise ValueError('Half of the open volume is below broker minimum volume')

    chronological = sorted(eligible, key=lambda item: (
        _field(item, 'time', 0), _field(item, 'ticket', 0)))
    ordered = chronological[::2]+chronological[1::2]
    remaining = target_units
    instructions = []
    for position in ordered:
        if remaining <= 0:
            break
        units = _volume_units(_field(position, 'volume'), volume_step)
        close_units = min(units, remaining)
        residual = units-close_units
        if 0 < residual < minimum_units:
            close_units = units-minimum_units
        if close_units < minimum_units:
            continue
        close_volume = float(Decimal(close_units)*step)
        original_volume = float(Decimal(units)*step)
        profit = float(_field(position, 'profit', 0) or 0)
        instructions.append({
            'ticket': int(_field(position, 'ticket')),
            'symbol': symbol,
            'position_type': int(_field(position, 'type')),
            'original_volume': original_volume,
            'close_volume': close_volume,
            'full_close': close_units == units,
            'magic': int(_field(position, 'magic', 0) or 0),
            'comment': str(_field(position, 'comment', '') or ''),
            'estimated_profit': profit*(close_units/units),
        })
        remaining -= close_units
    if remaining:
        raise ValueError('Exact half volume cannot be represented under broker volume limits')

    target_volume = float(Decimal(target_units)*step)
    total_volume = float(Decimal(total_units)*step)
    return {
        'action_version': ACTION_VERSION,
        'status': 'ready',
        'symbol': symbol,
        'snapshot_token': exposure_fingerprint(account, symbol, eligible),
        'position_count': len(eligible),
        'tradew_position_count': sum(
            int(_field(position, 'magic', 0) or 0) == int(magic)
            for position in eligible),
        'external_position_count': sum(
            int(_field(position, 'magic', 0) or 0) != int(magic)
            for position in eligible),
        'total_volume': total_volume,
        'close_target_volume': target_volume,
        'remaining_volume': total_volume-target_volume,
        'floating_profit': sum(float(_field(position, 'profit', 0) or 0)
                               for position in eligible),
        'estimated_profit_to_realize': sum(item['estimated_profit'] for item in instructions),
        'instructions': instructions,
        'full_position_closes': sum(item['full_close'] for item in instructions),
        'partial_position_closes': sum(not item['full_close'] for item in instructions),
    }
