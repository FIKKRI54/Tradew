"""Snapshot-bound LLM consensus and one-shot automatic market execution."""
import hashlib
import json
import math
import threading
import time
from pathlib import Path

from mt5_connector import create_mt5_adapter
from entry_plan import plan_direction, plan_key, quote_blocker
from risk_budget import INDEPENDENT_RISK_FRACTIONS, independent_risk_fraction


EXECUTION_LOCK = threading.Lock()


def consensus_result(decision, review_result, independent_result):
    direction = plan_direction(decision)
    review = (review_result or {}).get('review') or {}
    independent = (independent_result or {}).get('analysis') or {}
    same_snapshot = bool(decision.get('snapshot_id')) and all(
        item.get('snapshot_id') == decision['snapshot_id']
        for item in (review_result or {}, independent_result or {}))
    snapshot_mismatch = any(
        item and item.get('snapshot_id') != decision.get('snapshot_id')
        for item in (review_result, independent_result))
    cross_check_agrees = (
        review.get('status') == 'ok' and review.get('verdict') == 'AGREE' and
        review.get('recommended_action') == direction)
    independent_agrees = (
        independent.get('status') == 'ok' and
        independent.get('recommended_action') == direction and
        independent.get('setup_status') == 'READY')
    agreed = (direction in ('BUY', 'SELL') and same_snapshot and
              cross_check_agrees and independent_agrees)
    blockers = []
    if direction not in ('BUY', 'SELL'):
        blockers.append('deterministic_action_not_actionable')
    if snapshot_mismatch:
        blockers.append('analysis_snapshot_mismatch')
    if not cross_check_agrees:
        if review.get('status') == 'unavailable':
            blockers.append('informed_review_unavailable')
        elif review:
            blockers.append('informed_review_did_not_agree')
        else:
            blockers.append('informed_review_not_run')
    if not independent_agrees:
        if independent.get('status') == 'unavailable':
            blockers.append('independent_analysis_unavailable')
        elif independent:
            blockers.append('independent_analysis_not_ready')
        else:
            blockers.append('independent_analysis_not_run')
    errors = [item.get('error') for item in (review, independent)
              if item.get('status') == 'unavailable' and item.get('error')]
    return {
        'status': 'CONSENSUS' if agreed else 'NO_CONSENSUS',
        'direction': direction if direction in ('BUY', 'SELL') else None,
        'same_snapshot': same_snapshot,
        'deterministic': direction,
        'gemini_review': review.get('recommended_action'),
        'gemini_verdict': review.get('verdict'),
        'review_status': review.get('status'),
        'independent': independent.get('recommended_action'),
        'independent_setup_status': independent.get('setup_status'),
        'independent_status': independent.get('status'),
        'blockers': blockers,
        'errors': errors,
    }


def authorization_result(mode, decision, review_result=None, independent_result=None):
    direction = plan_direction(decision)
    independent = (independent_result or {}).get('analysis') or {}
    if mode == 'independent':
        independent_direction = independent.get('recommended_action')
        same_snapshot = bool(decision.get('snapshot_id')) and (
            (independent_result or {}).get('snapshot_id') == decision['snapshot_id'])
        ready = (independent.get('status') == 'ok' and
                 independent.get('setup_status') == 'READY' and
                 independent_direction in ('BUY', 'SELL') and
                 independent.get('risk_tier') in INDEPENDENT_RISK_FRACTIONS and
                 isinstance(independent.get('trade_plan'), dict))
        blockers = []
        if not same_snapshot:
            blockers.append('analysis_snapshot_mismatch')
        if not ready:
            blockers.append('independent_analysis_unavailable'
                            if independent.get('status') == 'unavailable'
                            else 'independent_plan_not_ready')
        errors = ([independent.get('error')]
                  if independent.get('status') == 'unavailable' and independent.get('error')
                  else [])
        return {
            'mode': mode, 'direction': independent_direction if ready else None,
            'deterministic': direction, 'signal_origin': 'independent_llm',
            'same_snapshot': same_snapshot,
            'independent': independent_direction,
            'independent_setup_status': independent.get('setup_status'),
            'independent_status': independent.get('status'),
            'risk_tier': independent.get('risk_tier') if ready else None,
            'status': 'AUTHORIZED' if same_snapshot and ready else 'NO_AUTHORIZATION',
            'blockers': blockers, 'errors': errors,
        }
    base = {
        'mode': mode, 'direction': direction if direction in ('BUY', 'SELL') else None,
        'deterministic': direction,
    }
    if direction not in ('BUY', 'SELL'):
        return {**base, 'status': 'NO_AUTHORIZATION',
                'blockers': ['deterministic_action_not_actionable'], 'errors': []}
    if mode == 'deterministic':
        return {**base, 'status': 'AUTHORIZED'}
    if mode == 'reviewed':
        review = (review_result or {}).get('review') or {}
        same_snapshot = bool(decision.get('snapshot_id')) and (
            (review_result or {}).get('snapshot_id') == decision['snapshot_id'])
        agrees = (review.get('status') == 'ok' and
                  review.get('verdict') == 'AGREE' and
                  review.get('recommended_action') == direction)
        blockers = []
        if not same_snapshot:
            blockers.append('analysis_snapshot_mismatch')
        if not agrees:
            blockers.append(
                'informed_review_unavailable' if review.get('status') == 'unavailable'
                else 'informed_review_did_not_agree' if review
                else 'informed_review_not_run')
        errors = ([review.get('error')]
                  if review.get('status') == 'unavailable' and review.get('error')
                  else [])
        return {
            **base, 'same_snapshot': same_snapshot,
            'gemini_review': review.get('recommended_action'),
            'gemini_verdict': review.get('verdict'),
            'review_status': review.get('status'),
            'signal_origin': 'reviewed_deterministic',
            'status': 'AUTHORIZED' if same_snapshot and agrees else 'NO_AUTHORIZATION',
            'blockers': blockers, 'errors': errors,
        }
    independent_agrees = (
        independent.get('status') == 'ok' and
        independent.get('recommended_action') == direction and
        independent.get('setup_status') == 'READY')
    same_snapshot = bool(decision.get('snapshot_id')) and (
        (independent_result or {}).get('snapshot_id') == decision['snapshot_id'])
    details = {
        **base, 'same_snapshot': same_snapshot,
        'independent': independent.get('recommended_action'),
        'independent_setup_status': independent.get('setup_status'),
        'independent_status': independent.get('status'),
    }
    consensus = consensus_result(decision, review_result, independent_result)
    return {**details, **consensus, 'mode': mode,
            'status': ('AUTHORIZED' if consensus['status'] == 'CONSENSUS'
                       else 'NO_AUTHORIZATION')}


def build_independent_execution_decision(source, live, independent_result, config):
    """Convert a grounded blind LLM plan into the normal immutable broker contract."""
    analysis = (independent_result or {}).get('analysis') or {}
    if ((independent_result or {}).get('snapshot_id') != source.get('snapshot_id') or
            analysis.get('status') != 'ok' or analysis.get('setup_status') != 'READY'):
        raise ValueError('Independent plan is not READY for its source snapshot')
    raw = analysis.get('trade_plan')
    direction = analysis.get('recommended_action')
    risk_tier = analysis.get('risk_tier')
    risk_reason = analysis.get('risk_reason')
    if not isinstance(raw, dict) or direction not in ('BUY', 'SELL'):
        raise ValueError('Independent analysis has no executable trade plan')
    risk_fraction = independent_risk_fraction(risk_tier)
    if not isinstance(risk_reason, str) or not risk_reason.strip():
        raise ValueError('Independent plan requires a risk-tier reason')
    if raw.get('direction') != direction or raw.get('order_type') != direction+'_LIMIT':
        raise ValueError('Independent plan direction is inconsistent')
    if (live.get('symbol') != source.get('symbol') or live.get('data_quality') or
            live.get('account_mode') != config.get('expected_mode')):
        raise ValueError('Live broker state is not eligible for independent execution')
    exposure = live.get('positions_summary') or {}
    from exit_management import entry_exposure_allowed
    if not entry_exposure_allowed(
            exposure.get('positions'), exposure.get('pending_orders'),
            config['execution_magic']):
        raise ValueError('Existing gold exposure blocks independent execution')
    now = time.time()
    if not 0 <= now-source.get('snapshot_time', 0) <= config['llm_approval_max_age_seconds']:
        raise ValueError('Independent source snapshot expired')
    prices = {key: raw.get(key) for key in (
        'entry_price', 'stop_loss', 'take_profit', 'confirmation_level',
        'structural_invalidation')}
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and
               math.isfinite(value) and value > 0 for value in prices.values()):
        raise ValueError('Independent plan contains an invalid price')
    sign = 1 if direction == 'BUY' else -1
    if (sign*(prices['entry_price']-prices['stop_loss']) <= 0 or
            sign*(prices['entry_price']-prices['structural_invalidation']) <= 0 or
            sign*(prices['take_profit']-prices['entry_price']) <= 0):
        raise ValueError('Independent plan price geometry is invalid')

    from independent_validation import independent_target_evidence
    target_evidence = independent_target_evidence(
        source, live, direction, prices['take_profit'], config)
    bid, ask = live.get('bid'), live.get('ask')
    if not all(isinstance(value, (int, float)) and math.isfinite(value)
               for value in (bid, ask)) or not 0 < bid <= ask:
        raise ValueError('Fresh live quote is unavailable')
    if (ask-bid > config['max_spread']+1e-9 or
            (direction == 'BUY' and prices['entry_price'] >= ask) or
            (direction == 'SELL' and prices['entry_price'] <= bid)):
        raise ValueError('Independent limit is no longer on the valid side of market')
    if ((direction == 'BUY' and prices['take_profit'] <= ask) or
            (direction == 'SELL' and prices['take_profit'] >= bid)):
        raise ValueError('Independent target has already been reached or passed by live price')
    if (sign*(bid-prices['confirmation_level']) <= 0 or
            sign*(bid-prices['structural_invalidation']) <= 0):
        raise ValueError('Live price no longer confirms the independent plan')

    # All executable levels must be traceable to the exact blind dataset.
    observed = [value for timeframe in (source.get('chart_data') or {}).values()
                for candle in timeframe[:-1]
                for key in ('open', 'high', 'low', 'close')
                if isinstance((value := candle.get(key)), (int, float))]
    observed.extend(
        point.get('price') for context in (source.get('structure') or {}).values()
        for side in ('high', 'low') for point in (context.get('pivots') or {}).get(side, [])
        if isinstance(point.get('price'), (int, float)))
    observed.extend(value for value in (source.get('bid'), source.get('ask'))
                    if isinstance(value, (int, float)))
    observed.extend(
        (candle['open']+candle['close'])/2
        for candle in ((source.get('chart_data') or {}).get('M5') or [])[:-1]
        if all(isinstance(candle.get(key), (int, float))
               for key in ('open', 'close')))
    tolerance = max(config.get('structural_tolerance', 0),
                    source.get('spread') or 0, 0.01)+1e-9
    grounded_prices = dict(prices)
    if target_evidence.get('type') == 'atr_projection':
        grounded_prices.pop('take_profit', None)
    if not observed or any(min(abs(value-reference) for reference in observed) > tolerance
                           for value in grounded_prices.values()):
        raise ValueError('Independent plan contains a price not grounded in blind input')
    m5 = (source.get('chart_data') or {}).get('M5') or []
    if len(m5) < 2:
        raise ValueError('Independent plan requires a completed M5 candle')
    expiry_minutes = raw.get('expires_in_minutes')
    if isinstance(expiry_minutes, bool) or not isinstance(expiry_minutes, int) or not 5 <= expiry_minutes <= 30:
        raise ValueError('Independent plan expiry is invalid')
    from independent_validation import validate_independent_evidence
    evidence, verified_prices, m5_atr = validate_independent_evidence(source, live, raw, config)
    if evidence.get('d1_macro_opposes') and risk_fraction > .2:
        risk_tier = 'MEDIUM'
        risk_fraction = .2
        risk_reason = risk_reason.strip() + ' D1 macro opposition caps risk at MEDIUM.'
    configured_max = config.get('balance_risk_fraction')
    if (not isinstance(configured_max, (int, float)) or
            risk_fraction > configured_max+1e-12):
        raise ValueError('Independent risk tier exceeds the configured account-risk cap')
    confirmation_time = int(m5[-2]['time'])
    entry = float(prices['entry_price'])
    spread = ask-bid
    bid_entry = entry-spread if direction == 'BUY' else entry
    zone_width = max(config.get('structural_tolerance', 0), 0.01)
    zone = {'low': bid_entry-zone_width/2, 'high': bid_entry+zone_width/2,
            'trigger_side': 'BID', 'direction': direction}
    setup_id = (f"{live['symbol']}:llm_independent:{direction}:"
                f"{confirmation_time}:{entry}")
    setup_ref = hashlib.sha256(setup_id.encode('utf-8')).hexdigest()[:10]
    plan = {
        'rule_version': 'independent-auto-31',
        'confirmation_evidence': evidence, 'verified_entry_prices': verified_prices,
        'target_evidence': target_evidence,
        'source': 'independent_llm',
        'risk_tier': risk_tier, 'risk_reason': risk_reason.strip(),
        'source_snapshot_id': source['snapshot_id'], 'setup_id': setup_id,
        'symbol': live['symbol'], 'direction': direction,
        'account': {key: config['expected_'+key] for key in
                    ('login', 'server', 'mode', 'currency')},
        'zone': zone, 'pending_price': entry,
        'level': float(prices['confirmation_level']),
        'structural_invalidation': float(prices['structural_invalidation']),
        'stop': float(prices['stop_loss']), 'targets': [float(prices['take_profit'])],
        'confirmation_time': confirmation_time,
        'expires_at': now+expiry_minutes*60,
        'expiry_minutes': expiry_minutes,
        'source_session': source.get('session'),
        'risk_fraction': risk_fraction,
        'cost_per_lot': config['cost_per_lot'],
        'm5_atr': m5_atr,
    }
    from limit_ladder import attach_limit_ladder, ladder_quality_acceptable
    plan = attach_limit_ladder(plan, bid, ask, config)
    acceptable, actual_span, required_span = ladder_quality_acceptable(
        plan, bid, ask, config)
    if not acceptable:
        raise ValueError(
            f'Independent ladder span {actual_span or 0:.2f} is below '
            f'the required {required_span or 0:.2f}')
    return {
        'snapshot_id': live['snapshot_id'], 'snapshot_time': live['snapshot_time'],
        'symbol': live['symbol'], 'account_mode': live['account_mode'],
        'bid': bid, 'ask': ask, 'spread': spread, 'data_quality': [],
        'action': 'WAIT', 'setup_state': 'ARMED_LIMIT',
        'setup_id': setup_id, 'setup_ref': setup_ref,
        'planned_direction': direction, 'planned_entry_zone': zone,
        'planned_targets': plan['targets'], 'invalidation': plan['stop'],
        'entry_plan': plan,
        'reasons': [
            ('Independent LLM plan uses one reduced-risk limit because its '
             'grounded range is too narrow for a quality ladder')
            if plan.get('ladder_execution_profile') == 'single_limit_reduced_risk'
            else 'Independent LLM-originated native limit plan',
            f'Independent risk tier {risk_tier}: {risk_fraction:.0%} maximum stop-risk budget'],
    }


def _already_attempted(path, setup_id):
    pending = False
    try:
        for line in path.read_text(encoding='utf-8').splitlines():
            record = json.loads(line)
            if record.get('setup_id') != setup_id:
                continue
            if record.get('event') == 'ATTEMPT_STARTED':
                pending = True
            if record.get('event') == 'ATTEMPT_FINISHED':
                outcome = record.get('execution') or {}
                # Pre-send rejections may retry. An uncertain send must be reconciled.
                if outcome.get('accepted'):
                    return True
                if outcome.get('status') == 'blocked_minimum_volume':
                    # Balance/contract sizing cannot improve on the next tick.
                    # A new setup ID may try again, but this setup must not spam
                    # identical attempts every second.
                    return True
                if outcome.get('status') == 'order_send_rejected':
                    response = outcome.get('result') or {}
                    if response and not response.get('order') and not response.get('deal'):
                        pending = False
                        continue
                    return True
                # Older v9 logged this explicit MT5 order_check failure as an
                # execution error even though order_send was never reached.
                if (outcome.get('status') == 'execution_error' and
                        'pending order_check returned no result' in outcome.get('error', '')):
                    pending = False
                    continue
                if outcome.get('send_attempted') or outcome.get('status') == 'execution_error':
                    return True
                pending = False
    except FileNotFoundError:
        pass
    return pending


def execute_if_fresh(current, reviewed, consensus, config, data_root,
                     broker_factory=create_mt5_adapter):
    """Submit an armed broker limit or an immediately actionable market order."""
    result = {'attempted': False, 'accepted': False, 'status': 'not_eligible'}
    if (not config.get('automatic_execution') or
            consensus.get('status') not in ('CONSENSUS', 'AUTHORIZED')):
        return result
    from execution_settings import load_execution_settings
    settings = load_execution_settings(data_root)
    if settings['paused']:
        return {**result, 'status': 'automation_paused'}
    required = ('setup_id', 'setup_ref', 'symbol', 'account_mode', 'entry_plan')
    if any(not current.get(field) for field in required):
        return {**result, 'status': 'missing_execution_fields'}
    state = current.get('setup_state')
    pending = state == 'ARMED_LIMIT'
    actionable = state == 'ACTIONABLE' and current.get('action') in ('BUY', 'SELL')
    if not pending and not actionable:
        return {**result, 'status': 'setup_no_longer_actionable'}
    direction = plan_direction(current)
    plan = current['entry_plan']
    if not plan.get('stop') or not plan.get('targets'):
        return {**result, 'status': 'missing_execution_fields'}
    if current.get('data_quality'):
        return {**result, 'status': 'stale_or_untrusted_data'}
    if consensus.get('direction') != direction:
        return {**result, 'status': 'authorization_direction_mismatch'}
    if (direction not in ('BUY', 'SELL') or
            direction != plan_direction(reviewed) or
            current.get('setup_id') != reviewed.get('setup_id')):
        return {**result, 'status': 'setup_no_longer_actionable'}
    if plan_key(current) != plan_key(reviewed):
        return {**result, 'status': 'reviewed_plan_changed'}
    now = time.time()
    if not 0 <= now-current.get('snapshot_time', 0) <= config['max_tick_age_seconds']:
        return {**result, 'status': 'stale_execution_snapshot'}
    if not 0 <= now-reviewed.get('snapshot_time', 0) <= config.get('llm_approval_max_age_seconds', 300):
        return {**result, 'status': 'review_expired'}
    blocked = quote_blocker(current['entry_plan'], current.get('bid'), current.get('ask'),
                            now, config['max_spread'])
    if pending and blocked not in (None, 'outside_entry_zone'):
        return {**result, 'status': 'blocked_' + blocked}
    if (pending and plan.get('rule_version') in ('structure-research-18',
                                                 'structure-research-19',
                                                 'structure-research-20',
                                                 'structure-research-21',
                                                 'structure-research-22',
                                                 'structure-research-23',
                                                 'structure-research-24',
                                                 'structure-research-25',
                                                 'structure-research-26') and
            not plan.get('retest_confirmation_time')):
        return {**result, 'status': 'retest_not_confirmed'}
    if actionable and blocked:
        return {**result, 'status': 'blocked_' + blocked}
    if current.get('account_mode') != config.get('expected_mode'):
        return {**result, 'status': 'account_mode_mismatch'}
    risk_fraction = config.get('balance_risk_fraction')
    if plan.get('source') == 'independent_llm':
        risk_fraction = plan.get('risk_fraction')
        try:
            expected_risk = independent_risk_fraction(plan.get('risk_tier'))
        except ValueError:
            return {**result, 'status': 'invalid_independent_risk_tier'}
        if (not isinstance(risk_fraction, (int, float)) or
                abs(risk_fraction-expected_risk) > 1e-12 or
                risk_fraction > config.get('balance_risk_fraction', 0)+1e-12):
            return {**result, 'status': 'independent_risk_tier_mismatch'}
    if consensus.get('mode'):
        if (settings['entry_mode'] != consensus['mode'] or
                (consensus.get('provider') and settings['provider'] != consensus['provider'])):
            return {**result, 'status': 'settings_changed_during_analysis'}
    journal = Path(data_root) / 'auto_executions.jsonl'
    with EXECUTION_LOCK:
        if _already_attempted(journal, current['setup_id']):
            return {**result, 'status': 'duplicate_setup_blocked'}
        journal.parent.mkdir(parents=True, exist_ok=True)
        started = {
            'event': 'ATTEMPT_STARTED', 'time': time.time(),
            'setup_id': current['setup_id'], 'setup_ref': current['setup_ref'],
            'reviewed_snapshot_id': reviewed['snapshot_id'],
            'execution_snapshot_id': current['snapshot_id'], 'consensus': consensus,
            'execution_type': 'BROKER_LIMIT' if pending else 'MARKET',
            'plan_key': plan_key(current), 'expires_at': plan['expires_at'],
            'entry_plan': plan,
        }
        with journal.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(started, allow_nan=False) + '\n')
        broker = None
        try:
            broker = broker_factory()
            if pending:
                execution = broker.place_pending_limit(
                    direction, current['symbol'], plan['stop'], plan['targets'][0],
                    risk_fraction, config['cost_per_lot'],
                    config['execution_magic'], current['setup_ref'], plan, config)
            else:
                execution = broker.execute_market_order(
                    direction, current['symbol'], current['stop'],
                    current['targets'][0], risk_fraction,
                    config['cost_per_lot'], config['order_deviation_points'],
                    config['execution_magic'], current['setup_ref'], plan, config)
        except Exception as exc:
            execution = {'accepted': False, 'status': 'execution_error',
                         'error': str(exc)[-500:]}
        finally:
            if broker:
                broker.close()
        record = {
            'event': 'ATTEMPT_FINISHED', 'time': time.time(),
            'setup_id': current['setup_id'],
            'setup_ref': current['setup_ref'], 'reviewed_snapshot_id': reviewed['snapshot_id'],
            'execution_snapshot_id': current['snapshot_id'], 'consensus': consensus,
            'entry_plan': plan, 'execution': execution,
        }
        with journal.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, allow_nan=False) + '\n')
        return {'attempted': True, **execution}


def _submitted_pending_orders(path, config):
    """Recover broker tickets and immutable plans from the append-only journal."""
    started = {}
    submitted = {}
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except FileNotFoundError:
        return submitted
    for line in lines:
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            continue
        setup_id = record.get('setup_id')
        if record.get('event') == 'ATTEMPT_STARTED' and setup_id:
            started[setup_id] = record
            continue
        execution = record.get('execution') or {}
        if (record.get('event') != 'ATTEMPT_FINISHED' or
                execution.get('status') not in (
                    'pending_submitted', 'pending_ladder_submitted',
                    'pending_ladder_partial') or
                not execution.get('accepted')):
            continue
        plan = record.get('entry_plan') or (started.get(setup_id) or {}).get('entry_plan')
        if not plan:
            # Backward-compatible recovery for the first v9 orders. Their
            # setup id contains the broker M5 confirmation time and level.
            try:
                parts = setup_id.rsplit(':', 4)
                symbol, _, direction, confirmation_time, level = parts
                plan = {
                    'setup_id': setup_id, 'symbol': symbol, 'direction': direction,
                    'account': {key: config['expected_'+key] for key in
                                ('login', 'server', 'mode', 'currency')},
                    'level': float(level),
                    'structural_invalidation': float(execution['stop']),
                    'confirmation_time': int(confirmation_time),
                    'expires_at': float(execution['expiration']),
                    'stop': float(execution['stop']),
                    'targets': [float(execution['target'])],
                }
            except (KeyError, TypeError, ValueError):
                continue
        orders = execution.get('orders')
        if not isinstance(orders, list) or not orders:
            orders = [{
                'result': execution.get('result') or {},
                'pending_price': execution.get('pending_price'),
                'stop': execution.get('stop'), 'target': execution.get('target'),
                'volume': execution.get('volume'),
            }]
        for item in orders:
            response = item.get('result') or {}
            ticket = response.get('order')
            if isinstance(ticket, bool) or not isinstance(ticket, int) or ticket <= 0:
                continue
            submitted[ticket] = {
                'ticket': ticket, 'setup_id': setup_id,
                'setup_ref': record.get('setup_ref'), 'plan': plan,
                'expected': {
                    'pending_price': item.get('pending_price'),
                    'stop': item.get('stop'), 'target': item.get('target'),
                    'volume': item.get('volume'),
                    'broker_expiration': execution.get('broker_expiration'),
                },
            }
    return submitted


def supervise_pending_orders(decision, config, data_root,
                             broker_factory=create_mt5_adapter):
    """Continuously validate owned MT5 limits; cancel only proven-invalid plans."""
    orders = (decision.get('positions_summary') or {}).get('pending_orders') or []
    owned = [order for order in orders
             if order.get('magic') == config.get('execution_magic') and
             'S:' in str(order.get('comment') or '')]
    if not owned:
        return []
    if decision.get('data_quality'):
        return [{'ticket': order.get('ticket'), 'status': 'supervision_deferred_untrusted_snapshot',
                 'accepted': False} for order in owned]
    registry = _submitted_pending_orders(Path(data_root)/'auto_executions.jsonl', config)
    results = []
    broker = None
    try:
        broker = broker_factory()
        for order in owned:
            ticket = order.get('ticket')
            submitted = registry.get(ticket)
            if not submitted:
                results.append({'ticket': ticket, 'accepted': False,
                                'status': 'pending_untracked_no_automatic_cancellation'})
                continue
            try:
                outcome = broker.revalidate_pending_limit(
                    int(ticket), order['symbol'], config['execution_magic'],
                    submitted['setup_ref'], submitted['plan'], submitted['expected'], config)
            except Exception as exc:
                outcome = {'ticket': ticket, 'accepted': False,
                           'status': 'pending_supervision_error',
                           'error': str(exc)[-500:]}
            results.append(outcome)
            if outcome.get('status') in ('pending_cancelled', 'pending_cancel_rejected'):
                journal = Path(data_root)/'pending_supervision.jsonl'
                journal.parent.mkdir(parents=True, exist_ok=True)
                with journal.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps({
                        'time': time.time(), 'setup_id': submitted['setup_id'],
                        'setup_ref': submitted['setup_ref'], 'outcome': outcome,
                    }, allow_nan=False)+'\n')
    finally:
        if broker:
            broker.close()
    return results


def apply_automatic_trailing(decision, config, data_root,
                             broker_factory=create_mt5_adapter):
    """Apply only deterministic stop improvements; never auto-close or widen."""
    if decision.get('data_quality') or decision.get('account_mode') != config.get('expected_mode'):
        return []
    positions = {
        position.get('ticket'): position
        for position in (decision.get('positions_summary') or {}).get('positions') or []
    }
    eligible = [change for change in decision.get('proposed_management_changes') or []
                if change.get('action') in ('REDUCE RISK', 'TRAIL SL', 'MOVE SL TO BE') and
                change.get('proposed_stop') is not None and
                change.get('ticket') in positions]
    if not eligible:
        return []
    journal = Path(data_root) / 'auto_trailing.jsonl'
    results = []
    with EXECUTION_LOCK:
        attempted = set()
        try:
            for line in journal.read_text(encoding='utf-8').splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get('event') == 'ATTEMPT_STARTED':
                    attempted.add((record.get('ticket'), record.get('proposed_stop')))
        except FileNotFoundError:
            pass
        broker = None
        try:
            broker = broker_factory()
            for change in eligible:
                key = (change['ticket'], change['proposed_stop'])
                if key in attempted:
                    continue
                started = {'event': 'ATTEMPT_STARTED', 'time': time.time(),
                           'ticket': change['ticket'],
                           'proposed_stop': change['proposed_stop'],
                           'action': change['action']}
                journal.parent.mkdir(parents=True, exist_ok=True)
                with journal.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(started, allow_nan=False) + '\n')
                try:
                    outcome = broker.modify_position_stop(
                        change['ticket'], positions[change['ticket']]['symbol'],
                        change['proposed_stop'], config['execution_magic'])
                except Exception as exc:
                    outcome = {'accepted': False, 'status': 'trailing_error',
                               'error': str(exc)[-500:]}
                finished = {**started, 'event': 'ATTEMPT_FINISHED', 'outcome': outcome}
                with journal.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(finished, allow_nan=False) + '\n')
                results.append(finished)
        finally:
            if broker:
                broker.close()
    return results
