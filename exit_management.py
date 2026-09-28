"""Shared deterministic helpers for take-profit and runner allocation."""
from decimal import Decimal, ROUND_HALF_UP
import re


def tp1_payoff(reward_per_lot, cost_per_lot, volume, minimum, step, config):
    """Cash objective at TP1, counting no unbanked runner profit.

    This is a conditional payoff estimate, not a promise of realized return.
    Runner costs remain reserved and the denominator retains its full SL risk.
    """
    take_volume, _ = (split_exit_volume(volume, minimum, step, config['take_profit_close_fraction'])
                      if config.get('runner_enabled') else (volume, 0))
    return reward_per_lot*take_volume-cost_per_lot*volume


ROLE_PATTERN = re.compile(r'(?:^|\s)L\d+([TR])(?:\s|$)')


def _field(item, name, default=None):
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def position_role(comment):
    """Return the immutable broker-comment role for a split position."""
    match = ROLE_PATTERN.search(str(comment or ''))
    if not match:
        return 'legacy'
    return 'take_profit' if match.group(1) == 'T' else 'runner'


def protected_runner(position, magic):
    """Return whether one owned runner has a stop at or beyond entry."""
    if position_role(_field(position, 'comment')) != 'runner':
        return False
    try:
        if int(_field(position, 'magic', 0) or 0) != int(magic):
            return False
        kind = int(_field(position, 'type'))
        entry = float(_field(position, 'price_open'))
        stop = float(_field(position, 'sl'))
        volume = float(_field(position, 'volume'))
    except (TypeError, ValueError):
        return False
    if kind not in (0, 1) or entry <= 0 or stop <= 0 or volume <= 0:
        return False
    return stop >= entry-1e-9 if kind == 0 else stop <= entry+1e-9


def entry_exposure_allowed(positions, orders, magic):
    """Allow a new setup only with no exposure or protected owned runners only."""
    positions = list(positions or [])
    orders = list(orders or [])
    return not orders and (not positions or all(
        protected_runner(position, magic) for position in positions))


def exposure_signature(positions):
    """Capture fixed position terms so pre-send exposure changes are detected."""
    keys = ('ticket', 'symbol', 'type', 'volume', 'price_open', 'sl', 'tp',
            'magic', 'comment')
    return sorted(tuple(_field(position, key) for key in keys)
                  for position in positions or [])


def split_exit_volume(volume, volume_min, volume_step, close_fraction):
    """Split a broker-valid volume while preserving its exact total.

    Unsplittable small orders remain one take-profit order. Volumes are
    returned as ``(take_profit_volume, runner_volume)``.
    """
    values = tuple(Decimal(str(value)) for value in
                   (volume, volume_min, volume_step, close_fraction))
    total, minimum, step, fraction = values
    if total <= 0 or minimum <= 0 or step <= 0:
        raise ValueError('Positive volume, minimum, and step are required')
    if not Decimal('0') < fraction < Decimal('1'):
        raise ValueError('take_profit_close_fraction must be in (0, 1)')
    total_units = int((total/step).to_integral_value(rounding=ROUND_HALF_UP))
    minimum_units = int((minimum/step).to_integral_value(rounding=ROUND_HALF_UP))
    if total_units < minimum_units*2:
        return float(total_units*step), 0.0
    take_units = int((Decimal(total_units)*fraction).to_integral_value(
        rounding=ROUND_HALF_UP))
    take_units = max(minimum_units, min(take_units, total_units-minimum_units))
    runner_units = total_units-take_units
    return float(take_units*step), float(runner_units*step)
