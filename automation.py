"""Continuous scanning, selectable AI authorization, execution, and trailing."""
import json
import os
import threading
import time

import main
from auto_execution import (apply_automatic_trailing, authorization_result,
                            build_independent_execution_decision,
                            execute_if_fresh, supervise_pending_orders)
from execution_settings import load_execution_settings
from entry_plan import plan_direction, plan_key
from llm_advisors import decision_review_key, independent_analysis, review_decision
from notification_service import (consider_signal_alert, notification_status,
                                  start_notification_worker)
from audit_storage import archive_status, start_archive_worker


def _number(name, default, minimum=0.0):
    try:
        return max(minimum, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return float(default)


SCAN_INTERVAL = _number('TRADEW_AUTO_SCAN_INTERVAL_SECONDS', 0, 0)
AUTO_GEMINI = os.environ.get('TRADEW_AUTO_GEMINI', 'false').lower() in ('1', 'true', 'yes')
MIN_SCORE = _number('TRADEW_AUTO_GEMINI_MIN_SCORE', 60, 0)
REPEAT_SECONDS = _number('TRADEW_AUTO_GEMINI_REPEAT_SECONDS', 900, 1)
INDEPENDENT_MIN_INTERVAL = _number('TRADEW_INDEPENDENT_MIN_INTERVAL_SECONDS', 300, 30)

_stop = threading.Event()
_review_ready = threading.Event()
_state_lock = threading.Lock()
_started = False
_pending_review = None
_review_cache = {}
_independent_cache = {}
_reviewed_at = {}
_last_candidate_key = None
_plan_approvals = {}
_active_plan_job = None
_approval_settings = None
_approval_generation = 0
_approval_plan = None
_last_independent_queued_at = 0
_state = {
    'enabled': False,
    'target_interval_seconds': SCAN_INTERVAL,
    'scan_in_progress': False,
    'scan_count': 0,
    'last_scan_started_at': None,
    'last_scan_completed_at': None,
    'last_scan_duration_seconds': None,
    'last_scan_error': None,
    'automatic_gemini_enabled': AUTO_GEMINI,
    'automatic_gemini_min_score': MIN_SCORE,
    'gemini_review_in_progress': False,
    'gemini_review_count': 0,
    'last_gemini_started_at': None,
    'last_gemini_completed_at': None,
    'last_gemini_error': None,
    'pending_gemini_review': False,
    'last_consensus': None,
    'last_authorization': None,
    'last_execution': None,
    'last_pending_supervision': [],
    'last_trailing': [],
    'entry_mode': 'consensus',
    'automatic_provider': 'gemini',
    'auto_trailing': True,
    'paused': False,
}


def _clone(value):
    return json.loads(json.dumps(value, allow_nan=False))


def _provider_error(*results):
    """Expose structured provider failures that do not raise exceptions."""
    for result in results:
        payload = ((result or {}).get('review') or
                   (result or {}).get('analysis') or {})
        if payload.get('status') == 'unavailable':
            return payload.get('error') or 'Selected AI provider is unavailable'
    return None


def _independent_can_authorize(decision, result):
    analysis = (result or {}).get('analysis') or {}
    return (result or {}).get('snapshot_id') == decision.get('snapshot_id') and (
        analysis.get('status') == 'ok' and
        plan_direction(decision) in ('BUY', 'SELL') and
        analysis.get('recommended_action') == plan_direction(decision) and
        analysis.get('risk_tier') in ('LOW', 'MEDIUM', 'HIGH') and
        analysis.get('setup_status') == 'READY')


def execute_independent_authorization(source, independent, authorization, config):
    """Refresh broker state after slow AI work, then validate and execute once."""
    timeout = max(15.0, float(config['max_tick_age_seconds'])*2)
    fresh = main.scan(audit_policy='changes', lock_timeout=timeout)
    independent_decision = build_independent_execution_decision(
        source, fresh, independent, config)
    from setup_tracker import SetupTracker
    SetupTracker(main.DATA_ROOT).record(independent_decision, {
        'account': {
            'login': config['expected_login'],
            'server': config['expected_server'],
            'currency': config['expected_currency'],
        },
        'account_mode': config['expected_mode'],
    })
    execution = execute_if_fresh(
        independent_decision, independent_decision,
        authorization, config, main.DATA_ROOT)
    execution.update(
        source_snapshot_id=source.get('snapshot_id'),
        execution_snapshot_id=independent_decision.get('snapshot_id'),
        execution_snapshot_age_seconds=max(
            0, time.time()-independent_decision.get('snapshot_time', time.time())))
    return fresh, independent_decision, execution


def cache_review(review):
    key = review.get('decision_key')
    if not key:
        return
    with _state_lock:
        _review_cache[key] = _clone(review)
        # The dashboard only needs a small recent cache.
        while len(_review_cache) > 20:
            _review_cache.pop(next(iter(_review_cache)))


def cache_independent(analysis):
    key = analysis.get('decision_key')
    if not key:
        return
    with _state_lock:
        _independent_cache[key] = _clone(analysis)
        while len(_independent_cache) > 20:
            _independent_cache.pop(next(iter(_independent_cache)))


def attach_cached_review(decision):
    if not decision or not decision.get('snapshot_id'):
        return decision
    key = decision_review_key(decision)
    with _state_lock:
        review = _review_cache.get(key)
        independent = _independent_cache.get(key)
    if review:
        decision['llm_review'] = _clone(review)
    if independent:
        decision['llm_independent_analysis'] = _clone(independent)
    return decision


def _potential_signal(decision):
    if not AUTO_GEMINI or not decision.get('snapshot_id'):
        return False
    if decision.get('data_quality'):
        return False
    if decision.get('action') in ('BUY', 'SELL'):
        return True
    if decision.get('setup_state') not in ('WATCH_LEVEL', 'CONFIRMED', 'ARMED_LIMIT'):
        return False
    score = max(decision.get('buy_setup_score') or 0,
                decision.get('sell_setup_score') or 0)
    return score >= MIN_SCORE


def _independent_market_event(decision):
    """Return a stable, low-cost trigger without exposing a deterministic plan."""
    if not AUTO_GEMINI or not decision.get('snapshot_id') or decision.get('data_quality'):
        return None
    exposure = decision.get('positions_summary') or {}
    from exit_management import entry_exposure_allowed
    if not entry_exposure_allowed(
            exposure.get('positions'), exposure.get('pending_orders'),
            main.load_config()['execution_magic']):
        return None
    score = max(decision.get('buy_setup_score') or 0,
                decision.get('sell_setup_score') or 0)
    if (decision.get('action') in ('BUY', 'SELL') or
            decision.get('setup_state') in ('WATCH_LEVEL', 'CONFIRMED', 'ARMED_LIMIT')):
        if score >= MIN_SCORE:
            return 'candidate:'+str(decision.get('setup_id') or decision.get('setup_state'))
    completed = ((decision.get('chart_data') or {}).get('M5') or [])[:-1]
    atr = ((decision.get('structure') or {}).get('M5') or {}).get('atr14_simple')
    if not completed or not isinstance(atr, (int, float)) or atr <= 0:
        return None
    candle = completed[-1]
    timestamp = candle.get('time')
    candle_range = candle.get('high', 0)-candle.get('low', 0)
    if candle_range >= atr*1.25:
        direction = 'up' if candle.get('close', 0) >= candle.get('open', 0) else 'down'
        return f'm5_displacement:{timestamp}:{direction}'
    threshold = max(atr*.2, (decision.get('spread') or 0)*2)
    zones = (decision.get('chart_analysis') or {}).get('zones') or []
    close = candle.get('close')
    near = [zone for zone in zones
            if isinstance(close, (int, float)) and
            (zone.get('strength') or 0) >= 2 and
            zone.get('low', float('-inf'))-threshold <= close <=
            zone.get('high', float('inf'))+threshold]
    if near:
        strongest = max(near, key=lambda zone: zone.get('strength') or 0)
        return f"m5_zone:{timestamp}:{strongest.get('kind')}:{strongest.get('price')}"
    return None


def consider_automatic_review(decision, settings=None):
    """Queue only a new material state or a stronger five-point score band."""
    global _pending_review, _last_candidate_key, _last_independent_queued_at
    settings = settings or load_execution_settings(main.DATA_ROOT)
    if settings.get('paused', False):
        return False
    if settings['entry_mode'] == 'deterministic':
        return False
    key = decision_review_key(decision)
    if settings['entry_mode'] == 'independent':
        event = _independent_market_event(decision)
        if not event:
            return False
        identity = event
    else:
        if not _potential_signal(decision):
            return False
        identity = plan_key(decision) or key
    approval_key = '|'.join((settings['entry_mode'], settings['provider'], identity))
    # A WATCH while armed must not suppress a fresh review at actual zone contact.
    job_key = approval_key + '|' + decision.get('setup_state', '')
    now = time.time()
    with _state_lock:
        if (settings['entry_mode'] == 'independent' and
                now-_last_independent_queued_at < INDEPENDENT_MIN_INTERVAL):
            return False
        if (job_key == _active_plan_job or
                (_pending_review or {}).get('job_key') == job_key):
            return False
        _last_candidate_key = job_key
        if now - _reviewed_at.get(job_key, 0) < REPEAT_SECONDS:
            return False
        # One slot intentionally coalesces bursts to the newest market state.
        _pending_review = {
            'decision': _clone(decision), 'decision_key': key,
            'job_key': job_key, 'mode': settings['entry_mode'],
            'provider': settings['provider'],
            'generation': _approval_generation,
            'approval_key': approval_key,
        }
        if settings['entry_mode'] == 'independent':
            _last_independent_queued_at = now
        _state['pending_gemini_review'] = True
    _review_ready.set()
    return True


def _review_loop():
    global _pending_review, _active_plan_job
    while not _stop.is_set():
        if not _review_ready.wait(1):
            continue
        _review_ready.clear()
        with _state_lock:
            job = _pending_review
            _pending_review = None
            _state['pending_gemini_review'] = False
            if job:
                _active_plan_job = job['job_key']
                _state['gemini_review_in_progress'] = True
                _state['last_gemini_started_at'] = time.time()
        if not job:
            continue
        decision = job['decision']
        key = job['decision_key']
        try:
            if job['generation'] != _approval_generation:
                continue
            if (job['mode'] != 'independent' and decision.get('entry_plan') and
                    time.time() >= decision['entry_plan']['expires_at']):
                continue
            if (job['mode'] == 'consensus' and
                    approved_plan(decision, {'entry_mode': job['mode'],
                                             'provider': job['provider']})):
                continue
            review = None
            independent = None
            if job['mode'] in ('independent', 'consensus'):
                independent = independent_analysis(decision, job['provider'], main.ROOT)
                independent['automatic'] = True
                cache_independent(independent)
            # Full consensus cannot succeed unless the blind gate is READY in
            # the same direction. Short-circuit here instead of spending a
            # second browser call while the market setup expires.
            if (job['mode'] == 'consensus' and
                    _independent_can_authorize(decision, independent)):
                review = review_decision(decision, job['provider'], main.ROOT)
                review['automatic'] = True
                cache_review(review)
            authorization = authorization_result(
                job['mode'], decision, review, independent)
            authorization['provider'] = job['provider']
            active_settings = load_execution_settings(main.DATA_ROOT)
            paused = active_settings['paused']
            settings_match = (not paused and
                              active_settings['entry_mode'] == job['mode'] and
                              active_settings['provider'] == job['provider'] and
                              job['generation'] == _approval_generation)
            if (job['mode'] == 'consensus' and settings_match and
                    authorization['status'] == 'AUTHORIZED' and plan_key(decision)):
                with _state_lock:
                    if (job['generation'] == _approval_generation and
                            plan_key(main.LAST) == plan_key(decision) and
                            main.LAST.get('setup_state') in ('ARMED_LIMIT', 'ACTIONABLE')):
                        _plan_approvals[job['approval_key']] = {
                            'decision': _clone(decision), 'authorization': _clone(authorization)}
                    while len(_plan_approvals) > 20:
                        _plan_approvals.pop(next(iter(_plan_approvals)))
            provider_error = _provider_error(review, independent)
            current = _clone(main.LAST)
            if paused:
                authorization['status'] = 'PAUSED'
                authorization.setdefault('blockers', []).append('automation_paused')
                execution = {'attempted': False, 'accepted': False,
                             'status': 'automation_paused'}
            elif not settings_match:
                execution = {'attempted': False, 'accepted': False,
                             'status': 'settings_changed_during_analysis'}
            elif job['mode'] == 'independent':
                execution = {'attempted': False, 'accepted': False,
                             'status': 'independent_plan_not_ready'}
                if authorization['status'] == 'AUTHORIZED':
                    try:
                        config = main.load_config()
                        current, independent_decision, execution = (
                            execute_independent_authorization(
                                decision, independent, authorization, config))
                        authorization['independent_plan'] = _clone(
                            independent_decision['entry_plan'])
                        authorization['source_snapshot_id'] = decision.get('snapshot_id')
                        authorization['execution_snapshot_id'] = (
                            independent_decision.get('snapshot_id'))
                    except (ValueError, RuntimeError) as exc:
                        message = str(exc)
                        authorization['status'] = 'NO_AUTHORIZATION'
                        if message.startswith('independent_entry_not_reachable:'):
                            blocker = 'independent_entry_not_reachable'
                            status = 'deferred_until_entry_zone_is_near'
                        elif message.startswith('independent_market_not_fresh:'):
                            blocker = 'independent_market_not_fresh'
                            status = 'deferred_until_fresh_m5_context'
                        else:
                            blocker = 'fresh_execution_validation_failed'
                            status = 'fresh_execution_validation_failed'
                        authorization.setdefault('blockers', []).append(blocker)
                        authorization.setdefault('errors', []).append(message)
                        execution = {'attempted': False, 'accepted': False,
                                     'status': status, 'error': message}
            else:
                execution = execute_if_fresh(
                    current, decision, authorization, main.load_config(), main.DATA_ROOT)
            with _state_lock:
                if job['generation'] == _approval_generation:
                    _reviewed_at[job['job_key']] = time.time()
                _state['gemini_review_count'] += 1
                _state['last_gemini_error'] = provider_error
                _state['last_consensus'] = _clone(authorization)
                _state['last_authorization'] = _clone(authorization)
                _state['last_execution'] = _clone(execution)
            live = main.LAST
            same_material_state = (live.get('snapshot_id') and
                                   decision_review_key(live) == key)
            if same_material_state or job['mode'] == 'independent':
                if review:
                    live['llm_review'] = _clone(review)
                if independent:
                    live['llm_independent_analysis'] = _clone(independent)
                live['consensus'] = _clone(authorization)
                live['automatic_execution'] = _clone(execution)
                live['execution_status'] = (
                    'submitted' if execution.get('accepted') else
                    'rejected' if execution.get('attempted') else 'not_submitted')
        except Exception as exc:
            with _state_lock:
                if job['generation'] == _approval_generation:
                    _reviewed_at[job['job_key']] = time.time()
                _state['last_gemini_error'] = str(exc)
        finally:
            with _state_lock:
                _active_plan_job = None
                _state['gemini_review_in_progress'] = False
                _state['last_gemini_completed_at'] = time.time()
            # A newer state may have arrived while Gemini was working.
            with _state_lock:
                has_pending = _pending_review is not None
            if has_pending:
                _review_ready.set()


def approved_plan(decision, settings):
    identity = plan_key(decision)
    if not identity or decision.get('setup_state') not in ('ARMED_LIMIT', 'ACTIONABLE'):
        return None
    key = '|'.join((settings['entry_mode'], settings['provider'], identity))
    with _state_lock:
        approval = _clone(_plan_approvals.get(key))
    if not approval:
        return None
    now = time.time()
    ttl = main.load_config().get('llm_approval_max_age_seconds', 300)
    if (now >= decision['entry_plan']['expires_at'] or
            not 0 <= now-approval['decision'].get('snapshot_time', 0) <= ttl):
        with _state_lock:
            _plan_approvals.pop(key, None)
            for reviewed_key in list(_reviewed_at):
                if reviewed_key.startswith(key + '|'):
                    _reviewed_at.pop(reviewed_key, None)
        return None
    return approval


def refresh_approval_scope(decision, settings):
    """Discard cached/in-flight approval when settings or the observed thesis change."""
    global _approval_settings, _approval_generation, _approval_plan, _pending_review
    scope = (settings['entry_mode'], settings['provider'])
    with _state_lock:
        if scope != _approval_settings:
            _approval_settings = scope
            _approval_generation += 1
            _plan_approvals.clear()
            _reviewed_at.clear()
            _pending_review = None
        active = (plan_key(decision)
                  if settings['entry_mode'] != 'independent' and
                  decision.get('setup_state') in ('ARMED_LIMIT', 'ACTIONABLE')
                  else None)
        if active != _approval_plan:
            _approval_generation += 1
            _approval_plan = active
            _pending_review = None
            _reviewed_at.clear()
        for key in list(_plan_approvals):
            if not active or not key.endswith('|' + active):
                _plan_approvals.pop(key, None)
                for reviewed_key in list(_reviewed_at):
                    if reviewed_key.startswith(key + '|'):
                        _reviewed_at.pop(reviewed_key, None)


def _scan_loop():
    while not _stop.is_set():
        started = time.time()
        with _state_lock:
            _state['scan_in_progress'] = True
            _state['last_scan_started_at'] = started
        try:
            decision = main.scan(audit_policy='changes')
            attach_cached_review(decision)
            settings = load_execution_settings(main.DATA_ROOT)
            refresh_approval_scope(decision, settings)
            config = main.load_config()
            pending_supervision = supervise_pending_orders(
                decision, config, main.DATA_ROOT)
            if pending_supervision:
                decision['pending_supervision'] = _clone(pending_supervision)
                if any(item.get('status') == 'pending_cancelled'
                       for item in pending_supervision):
                    decision['execution_status'] = 'pending_cancelled'
            trailing = (apply_automatic_trailing(decision, config, main.DATA_ROOT)
                        if settings['auto_trailing'] else [])
            if trailing:
                decision['automatic_trailing'] = _clone(trailing)
            if settings['paused']:
                authorization = {
                    'mode': settings['entry_mode'], 'status': 'PAUSED',
                    'direction': None, 'blockers': ['automation_paused'], 'errors': [],
                }
                execution = {'attempted': False, 'accepted': False,
                             'status': 'automation_paused'}
                decision['consensus'] = _clone(authorization)
                decision['automatic_execution'] = _clone(execution)
                decision['execution_status'] = 'paused'
                with _state_lock:
                    _state['last_consensus'] = _clone(authorization)
                    _state['last_authorization'] = _clone(authorization)
                    _state['last_execution'] = _clone(execution)
            elif settings['entry_mode'] == 'deterministic':
                authorization = authorization_result('deterministic', decision)
                execution = execute_if_fresh(
                    _clone(decision), decision, authorization, config, main.DATA_ROOT)
                decision['consensus'] = _clone(authorization)
                if execution.get('attempted'):
                    decision['automatic_execution'] = _clone(execution)
                    decision['execution_status'] = ('submitted' if execution.get('accepted')
                                                    else 'rejected')
                with _state_lock:
                    _state['last_consensus'] = _clone(authorization)
                    _state['last_authorization'] = _clone(authorization)
                    _state['last_execution'] = _clone(execution)
            else:
                approval = approved_plan(decision, settings)
                if approval:
                    execution = execute_if_fresh(
                        _clone(decision), approval['decision'], approval['authorization'],
                        config, main.DATA_ROOT)
                    decision['consensus'] = _clone(approval['authorization'])
                    with _state_lock:
                        _state['last_authorization'] = _clone(approval['authorization'])
                        _state['last_execution'] = _clone(execution)
                    if execution.get('attempted'):
                        decision['automatic_execution'] = _clone(execution)
                        decision['execution_status'] = ('submitted' if execution.get('accepted') else 'rejected')
                else:
                    consider_automatic_review(decision, settings)
            consider_signal_alert(decision)
            with _state_lock:
                _state['scan_count'] += 1
                _state['last_scan_error'] = None
                _state['last_trailing'] = _clone(trailing)
                _state['last_pending_supervision'] = _clone(pending_supervision)
                _state['entry_mode'] = settings['entry_mode']
                _state['automatic_provider'] = settings['provider']
                _state['auto_trailing'] = settings['auto_trailing']
                _state['paused'] = settings['paused']
        except RuntimeError as exc:
            # A manual replay/scan owns the broker lock; retry on the next tick.
            if 'already in progress' not in str(exc):
                with _state_lock:
                    _state['last_scan_error'] = str(exc)
        except Exception as exc:
            with _state_lock:
                _state['last_scan_error'] = str(exc)
        finally:
            finished = time.time()
            with _state_lock:
                _state['scan_in_progress'] = False
                _state['last_scan_completed_at'] = finished
                _state['last_scan_duration_seconds'] = round(finished - started, 3)
        # Target start-to-start cadence, without concurrent scans.
        _stop.wait(max(0.05, SCAN_INTERVAL - (time.time() - started)))


def automation_status():
    with _state_lock:
        status = dict(_state)
    status['phone_notifications'] = notification_status()
    status['audit_storage'] = archive_status(main.DATA_ROOT)
    return status


def execution_settings_changed(settings):
    """Apply runtime choices immediately and let the new mode review current state."""
    global _last_candidate_key, _pending_review, _last_independent_queued_at
    with _state_lock:
        _last_candidate_key = None
        _pending_review = None
        _reviewed_at.clear()
        _state['pending_gemini_review'] = False
        _state['entry_mode'] = settings['entry_mode']
        _state['automatic_provider'] = settings['provider']
        _state['auto_trailing'] = settings['auto_trailing']
        _state['paused'] = settings['paused']
        _last_independent_queued_at = 0


def start_automation():
    global _started
    if SCAN_INTERVAL <= 0:
        return False
    with _state_lock:
        if _started:
            return True
        _started = True
        _state['enabled'] = True
    start_notification_worker(main.DATA_ROOT)
    start_archive_worker(main.DATA_ROOT)
    threading.Thread(target=_review_loop, name='tradew-gemini-review', daemon=True).start()
    threading.Thread(target=_scan_loop, name='tradew-market-scan', daemon=True).start()
    return True
