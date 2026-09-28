"""Shared plan identity and quote eligibility for scanning, execution and replay."""
import hashlib
import json
import math


def plan_direction(decision):
    if decision.get('action') in ('BUY', 'SELL'):
        return decision['action']
    if decision.get('setup_state') == 'ARMED_LIMIT':
        return decision.get('planned_direction')
    return None


def plan_key(decision):
    plan = decision.get('entry_plan')
    if not plan:
        return None
    return hashlib.sha256(json.dumps(plan, sort_keys=True, allow_nan=False,
                                     separators=(',', ':')).encode()).hexdigest()


def quote_blocker(plan, bid, ask, now, max_spread):
    if not all(isinstance(x, (int, float)) and math.isfinite(x)
               for x in (bid, ask, now, max_spread)) or not 0 < bid <= ask:
        return 'invalid_quote'
    if now >= plan['expires_at']:
        return 'plan_expired'
    if ask-bid > max_spread + 1e-9:
        return 'spread_exceeded'
    sign = 1 if plan['direction'] == 'BUY' else -1
    if sign*(bid-plan['structural_invalidation']) <= 0:
        return 'structure_invalidated'
    if sign*(bid-plan['level']) <= 0:
        return 'level_reclaimed'
    # MT5 candles and all structural zones are bid prices in both directions.
    zone = plan['zone']
    if not zone['low'] <= bid <= zone['high']:
        return 'outside_entry_zone'
    return None
