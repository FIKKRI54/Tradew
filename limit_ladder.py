"""Deterministic limit-ladder construction shared by live and replay paths."""
import math


DEFAULT_WEIGHTS = (0.25, 0.35, 0.40)
REDUCED_SINGLE_PROFILE = 'single_limit_reduced_risk'
PREFLIGHT_SUBSET_PROFILE = 'preflight_validated_subset'


def ladder_span_metrics(plan, bid, ask, config):
    """Measure whether separate layers cover a meaningful executable range."""
    layers = plan.get('limit_layers') or []
    prices = [layer.get('price') for layer in layers if isinstance(layer, dict)]
    if not prices or any(not isinstance(value, (int, float)) or
                         not math.isfinite(value) for value in prices):
        return None, None
    actual = max(prices)-min(prices) if len(prices) > 1 else 0.0
    spread = max(0.0, ask-bid)
    atr = float(plan.get('m5_atr') or 0)
    required = max(
        spread*float(config.get('minimum_ladder_span_spread_multiple', 2.0)),
        atr*float(config.get('minimum_ladder_span_atr_fraction', .25)))
    return actual, required


def ladder_settings(config):
    count = config.get('limit_ladder_layers', len(DEFAULT_WEIGHTS))
    weights = config.get('limit_ladder_weights', list(DEFAULT_WEIGHTS))
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 5:
        raise ValueError('limit_ladder_layers must be an integer from 1 to 5')
    if not isinstance(weights, list) or len(weights) != count:
        raise ValueError('limit_ladder_weights must match limit_ladder_layers')
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or
           not math.isfinite(value) or value <= 0 for value in weights):
        raise ValueError('limit_ladder_weights must contain positive finite numbers')
    total = sum(weights)
    if total <= 0:
        raise ValueError('limit_ladder_weights must have a positive total')
    return count, [value/total for value in weights]


def reduced_single_limit_valid(plan, config):
    """Validate the immutable independent-plan fallback contract."""
    if (plan.get('rule_version') not in ('independent-auto-20',
                                         'independent-auto-21',
                                         'independent-auto-22',
                                         'independent-auto-23',
                                         'independent-auto-24',
                                         'independent-auto-25',
                                         'independent-auto-26', 'independent-auto-27',
                                         'independent-auto-28', 'independent-auto-29') or
            plan.get('source') != 'independent_llm' or
            plan.get('ladder_execution_profile') != REDUCED_SINGLE_PROFILE):
        return False
    layers = plan.get('limit_layers') or []
    targets = plan.get('targets') or []
    pending = plan.get('pending_price')
    risk_fraction = plan.get('risk_fraction')
    if (len(layers) != 1 or not targets or
            not all(isinstance(value, (int, float)) and not isinstance(value, bool) and
                    math.isfinite(value) and value > 0
                    for value in (pending, targets[0], risk_fraction))):
        return False
    _, weights = ladder_settings(config)
    layer = layers[0]
    if not isinstance(layer, dict):
        return False
    expected_weight = weights[0]
    expected_total = risk_fraction*expected_weight
    return (
        layer.get('quality') == 'single_reduced_risk' and
        isinstance(layer.get('price'), (int, float)) and
        not isinstance(layer.get('price'), bool) and
        isinstance(layer.get('target'), (int, float)) and
        not isinstance(layer.get('target'), bool) and
        isinstance(layer.get('risk_weight'), (int, float)) and
        not isinstance(layer.get('risk_weight'), bool) and
        abs(layer['price']-pending) <= 1e-9 and
        abs(layer['target']-targets[0]) <= 1e-9 and
        abs(layer['risk_weight']-expected_weight) <= 1e-9 and
        isinstance(plan.get('limit_ladder_total_risk_fraction'), (int, float)) and
        not isinstance(plan.get('limit_ladder_total_risk_fraction'), bool) and
        abs(plan['limit_ladder_total_risk_fraction']-expected_total) <= 1e-9)


def ladder_quality_acceptable(plan, bid, ask, config):
    """Return quality acceptance plus current span diagnostics."""
    actual, required = ladder_span_metrics(plan, bid, ask, config)
    accepted = (actual is not None and required is not None and
                actual+1e-9 >= required)
    subset = plan.get('ladder_execution_profile') == PREFLIGHT_SUBSET_PROFILE
    subset_accepted = (
        subset and bool(plan.get('limit_layers')) and
        isinstance(plan.get('preflight_original_span'), (int, float)) and
        isinstance(plan.get('minimum_limit_ladder_span'), (int, float)) and
        plan['preflight_original_span']+1e-9 >= plan['minimum_limit_ladder_span'] and
        all(check.get('eligible') for check in
            (plan.get('preflight_layer_checks') or [])) and
        len(plan.get('preflight_layer_checks') or []) == len(plan['limit_layers']))
    return (accepted or reduced_single_limit_valid(plan, config) or subset_accepted,
            actual, required)


def preflight_ladder_layers(plan, bid, ask, config):
    """Remove deterministically unusable layers before a plan can be armed.

    The broker adapter repeats these checks with exact symbol economics.  This
    early pass prevents the UI and the authorization layer from advertising
    prices that already fail direction, stop-distance, or banked-TP1 geometry.
    Removed risk is not redistributed to the remaining layers.
    """
    if not isinstance(plan, dict) or plan.get('direction') not in ('BUY', 'SELL'):
        raise ValueError('A directional plan is required for layer preflight')
    original_layers = plan.get('limit_layers') or []
    original_span, required_span = ladder_span_metrics(plan, bid, ask, config)
    direction = plan['direction']
    sign = 1 if direction == 'BUY' else -1
    spread = max(0.0, ask-bid)
    broker_minimum = max(0.0, float(plan.get('broker_minimum_distance') or 0))
    protected_distance = max(
        broker_minimum,
        spread*2,
        float(plan.get('m5_atr') or 0) *
        float(config.get('minimum_layer_stop_atr_fraction', .25)))
    close_fraction = float(config.get('take_profit_close_fraction', .65))
    minimum_rr = float(config.get('minimum_ladder_reward_risk', 0))
    eligible = []
    checks = []
    rejected = []
    for layer in original_layers:
        price = layer.get('price')
        target = layer.get('target')
        reasons = []
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and
                   math.isfinite(value) for value in (price, target, plan.get('stop'))):
            reasons.append('invalid_geometry')
            risk = reward = banked_rr = None
        else:
            pending = price < ask if direction == 'BUY' else price > bid
            if not pending:
                reasons.append('not_pending_at_current_quote')
            pending_distance = ask-price if direction == 'BUY' else price-bid
            if pending and pending_distance+1e-9 < broker_minimum:
                reasons.append('pending_distance_below_broker_minimum')
            risk = sign*(price-plan['stop'])
            reward = sign*(target-price)
            if risk <= 0 or reward <= 0:
                reasons.append('invalid_reward_or_stop_geometry')
                banked_rr = None
            else:
                if risk+1e-9 < protected_distance:
                    reasons.append('stop_distance_below_protected_minimum')
                banked_rr = close_fraction*reward/risk
                if banked_rr+1e-9 < minimum_rr:
                    reasons.append('banked_tp1_reward_risk_below_minimum')
        check = {
            'index': layer.get('index'), 'quality': layer.get('quality'),
            'price': price, 'eligible': not reasons, 'reasons': reasons,
            'stop_distance': risk, 'protected_stop_distance': protected_distance,
            'banked_tp1_reward_risk': banked_rr,
        }
        if reasons:
            rejected.append(check)
        else:
            eligible.append(dict(layer))
            checks.append(check)
    risk_fraction = float(plan.get('risk_fraction') or 0)
    result = {
        **plan,
        'limit_layers': eligible,
        'preflight_layers_requested': len(original_layers),
        'preflight_layers_eligible': len(eligible),
        'preflight_layer_checks': checks,
        'preflight_rejected_layers': rejected,
        'preflight_original_span': original_span,
        'minimum_limit_ladder_span': required_span,
        'limit_ladder_total_risk_fraction':
            risk_fraction*sum(float(layer.get('risk_weight') or 0)
                              for layer in eligible),
    }
    if len(eligible) != len(original_layers) and eligible:
        result['ladder_execution_profile'] = PREFLIGHT_SUBSET_PROFILE
    actual, _ = ladder_span_metrics(result, bid, ask, config)
    result['limit_ladder_span'] = actual
    return result


def attach_limit_ladder(plan, bid, ask, config):
    """Return a copy of *plan* with near-to-deep limit layers.

    The total risk weights always sum to one. Better-priced deeper layers receive
    the larger default allocations, while every layer retains the immutable
    structural stop and one of the plan's grounded objectives.
    """
    if not isinstance(plan, dict) or plan.get('direction') not in ('BUY', 'SELL'):
        raise ValueError('A directional entry plan is required for a limit ladder')
    if not all(isinstance(value, (int, float)) and math.isfinite(value)
               for value in (bid, ask)) or not 0 < bid <= ask:
        raise ValueError('A valid quote is required for a limit ladder')
    count, weights = ladder_settings(config)
    configured_weights = list(weights)
    direction = plan['direction']
    spread = ask-bid
    zone = plan.get('zone') or {}
    targets = plan.get('targets') or []
    if not targets:
        raise ValueError('A limit ladder requires at least one target')
    if plan.get('rule_version') in ('independent-auto-27', 'independent-auto-28',
                                    'independent-auto-29'):
        # Never manufacture earlier entries from a live quote. Preserve the LLM's
        # exact entry, adding only independently verified, better-priced levels.
        verified = plan.get('verified_entry_prices') or []
        entry = plan.get('pending_price')
        if not verified or entry not in verified:
            raise ValueError('Independent entry is not structurally verified')
        required = max(spread*config.get('minimum_ladder_span_spread_multiple', 2),
                       plan['m5_atr']*config.get('minimum_ladder_span_atr_fraction', .25))
        spacing = required/max(count-1, 1)
        sign = 1 if direction == 'BUY' else -1
        prices = [entry]
        for price in sorted(set(verified), reverse=direction == 'BUY'):
            if sign*(prices[-1]-price) >= spacing and len(prices) < count:
                prices.append(price)
        if len(prices) < count or abs(prices[0]-prices[-1]) < required:
            prices = [entry]
        layers = [{'index': i+1, 'quality': 'single_reduced_risk' if len(prices) == 1 else
                   ('near' if i == 0 else 'deep' if i == len(prices)-1 else 'middle'),
                   'price': price, 'target': float(targets[0]), 'risk_weight': weights[i]}
                  for i, price in enumerate(prices)]
        return {**plan, 'limit_layers': layers,
                'ladder_execution_profile': REDUCED_SINGLE_PROFILE if len(prices) == 1 else 'structural_levels',
                'limit_ladder_total_risk_fraction': plan['risk_fraction']*sum(x['risk_weight'] for x in layers),
                'limit_ladder_span': abs(prices[0]-prices[-1]), 'minimum_limit_ladder_span': required}

    if plan.get('pending_price') is not None:
        deep = float(plan['pending_price'])
        raw_reference = float(plan.get('level', deep))
        reference = raw_reference + (spread if direction == 'BUY' else 0)
        if direction == 'BUY':
            near = reference if deep <= reference < ask else deep
        else:
            near = reference if bid < reference <= deep else deep
        # Independent plans sometimes use the same confirmed level and entry.
        # That is a valid single-price thesis, but it is not a useful ladder.
        # In that case, stage the first order one fraction of the way from the
        # executable quote toward the explicit LLM entry, retaining that entry
        # as the deepest/best-priced layer. This remains bounded by the live
        # quote and the LLM's own price instead of inventing levels beyond it.
        if count > 1 and (abs(raw_reference-deep) <= 1e-9 or
                          abs(near-deep) <= 1e-9):
            executable = ask if direction == 'BUY' else bid
            if ((direction == 'BUY' and deep < executable) or
                    (direction == 'SELL' and deep > executable)):
                near = executable+(deep-executable)/count
    else:
        if not all(isinstance(zone.get(key), (int, float)) and math.isfinite(zone[key])
                   for key in ('low', 'high')) or zone['low'] > zone['high']:
            raise ValueError('A valid entry zone is required for a limit ladder')
        if direction == 'BUY':
            near, deep = float(zone['high'])+spread, float(zone['low'])+spread
        else:
            near, deep = float(zone['low']), float(zone['high'])

    # Collapse genuinely zero-width plans to one order instead of sending
    # duplicate prices with different comments and artificial allocations.
    if abs(near-deep) <= 1e-9:
        count, weights = 1, [1.0]
    layers = []
    for index in range(count):
        fraction = index/(count-1) if count > 1 else 0
        price = near+(deep-near)*fraction
        target_index = min(index, len(targets)-1)
        layers.append({
            'index': index+1,
            'quality': ('near' if index == 0 else
                        'deep' if index == count-1 else 'middle'),
            'price': price,
            'target': float(targets[target_index]),
            'risk_weight': weights[index],
        })
    result = {**plan, 'limit_layers': layers,
              'limit_ladder_total_risk_fraction': plan.get('risk_fraction')}
    actual, required = ladder_span_metrics(result, bid, ask, config)
    if (actual is not None and required is not None and actual+1e-9 < required and
            plan.get('rule_version') in ('independent-auto-20',
                                         'independent-auto-21',
                                         'independent-auto-22',
                                         'independent-auto-23',
                                         'independent-auto-24',
                                         'independent-auto-25',
                                         'independent-auto-26') and
            plan.get('source') == 'independent_llm' and
            isinstance(plan.get('pending_price'), (int, float))):
        reduced_weight = configured_weights[0]
        reduced_layer = {
            'index': 1, 'quality': 'single_reduced_risk',
            'price': float(plan['pending_price']),
            'target': float(targets[0]), 'risk_weight': reduced_weight,
        }
        return {
            **result, 'limit_layers': [reduced_layer],
            'ladder_execution_profile': REDUCED_SINGLE_PROFILE,
            'limit_ladder_original_span': actual,
            'limit_ladder_span': 0.0,
            'minimum_limit_ladder_span': required,
            'limit_ladder_total_risk_fraction':
                float(plan.get('risk_fraction') or 0)*reduced_weight,
        }
    return {**result, 'limit_ladder_span': actual,
            'minimum_limit_ladder_span': required}
