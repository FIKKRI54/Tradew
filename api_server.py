import json
import threading
import time
from flask import Flask, jsonify, request
from main import scan, historical_replay
import main
from setup_tracker import SetupTracker
import automation

app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False
PORTFOLIO_ACTION_LOCK = threading.Lock()
PORTFOLIO_ACTION_AUDIT_LOCK = threading.Lock()
PORTFOLIO_PREVIEWS = {}
PORTFOLIO_PREVIEW_TTL_SECONDS = 60

def error_payload(message):
    return dict(action='WAIT', execution_status='advisory_only', error=str(message))


def _portfolio_action_paused():
    from execution_settings import load_execution_settings
    return load_execution_settings(main.DATA_ROOT).get('paused') is True


def _record_portfolio_action(kind, payload):
    record = {'time': time.time(), 'action': kind, **payload}
    path = main.DATA_ROOT/'portfolio_actions.jsonl'
    try:
        with PORTFOLIO_ACTION_AUDIT_LOCK:
            with path.open('a', encoding='utf-8') as log:
                log.write(json.dumps(record, allow_nan=False, separators=(',', ':'))+'\n')
        return None
    except OSError as exc:
        return str(exc)

@app.get('/api/status')
def status():
    from scan_engine import RULE_VERSION
    from independent_validation import VERSION
    payload = json.loads(json.dumps(main.LAST))
    payload['automation'] = automation.automation_status()
    payload['runtime_versions'] = {'scanner': RULE_VERSION, 'independent': VERSION}
    return jsonify(payload)

@app.post('/api/scan')
def request_scan():
    try:
        result = scan()
        automation.attach_cached_review(result)
        return jsonify(result)
    except Exception as exc:
        return jsonify(error_payload(exc)), 503

@app.post('/api/setups/bind')
def bind_setup():
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get('ticket'), int) or not isinstance(data.get('setup_ref'), str):
        return jsonify(error='ticket (integer) and setup_ref (string) are required'), 400
    try:
        SetupTracker(main.DATA_ROOT).bind(data['ticket'], data['setup_ref'])
        return jsonify(status='pending_snapshot_validation', ticket=data['ticket'],
                       setup_ref=data['setup_ref'])
    except ValueError as exc:
        return jsonify(error=str(exc)), 404

@app.post('/api/replay')
def request_replay():
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(historical_replay(data.get('count', 5000), data.get('initial_balance')))
    except (ValueError, RuntimeError) as exc:
        return jsonify(error=str(exc)), 400


@app.post('/api/portfolio/close-half/preview')
def portfolio_close_half_preview():
    """Preview a manual 50% symbol scale-out without sending broker orders."""
    if not _portfolio_action_paused():
        return jsonify(error='Pause automation before using a manual portfolio action'), 409
    broker = None
    try:
        config = main.load_config()
        broker = main.create_mt5_adapter()
        result = broker.preview_half_close(config['symbol'], config)
        if result.get('accepted') and isinstance(result.get('snapshot_token'), str):
            with PORTFOLIO_ACTION_LOCK:
                PORTFOLIO_PREVIEWS.clear()
                PORTFOLIO_PREVIEWS[result['snapshot_token']] = time.time()
        audit_error = _record_portfolio_action('CLOSE_HALF_PREVIEW', {'result': result})
        if audit_error:
            result['audit_error'] = audit_error
        return jsonify(result)
    except (ValueError, RuntimeError) as exc:
        return jsonify(error=str(exc)), 503
    finally:
        if broker:
            broker.close()


@app.post('/api/portfolio/close-half')
def portfolio_close_half():
    """Execute a preview-bound manual half close, including external positions."""
    data = request.get_json(silent=True) or {}
    if data.get('confirmation') != 'CLOSE 50%':
        return jsonify(error='Exact confirmation CLOSE 50% is required'), 400
    token = data.get('snapshot_token')
    if not isinstance(token, str) or len(token) != 64:
        return jsonify(error='A valid preview snapshot token is required'), 400
    if not _portfolio_action_paused():
        return jsonify(error='Automation must remain paused during the half close'), 409
    if not PORTFOLIO_ACTION_LOCK.acquire(blocking=False):
        return jsonify(error='Another portfolio action is already running'), 409
    broker = None
    try:
        previewed_at = PORTFOLIO_PREVIEWS.pop(token, None)
        if previewed_at is None or time.time()-previewed_at > PORTFOLIO_PREVIEW_TTL_SECONDS:
            return jsonify(error='Half-close preview expired; request a fresh preview'), 409
        config = main.load_config()
        broker = main.create_mt5_adapter()
        result = broker.close_half_positions(config['symbol'], token, config)
        audit_error = _record_portfolio_action('CLOSE_HALF_EXECUTION', {
            'snapshot_token': token, 'result': result})
        if audit_error:
            result['audit_error'] = audit_error
        return jsonify(result)
    except (ValueError, RuntimeError) as exc:
        _record_portfolio_action('CLOSE_HALF_ERROR', {
            'snapshot_token': token, 'error': str(exc)})
        return jsonify(error=str(exc)), 503
    finally:
        if broker:
            broker.close()
        PORTFOLIO_ACTION_LOCK.release()

@app.get('/api/validation')
def validation_status():
    from validation_report import prospective_report
    config = main.load_config()
    return jsonify(prospective_report(main.DATA_ROOT, config.get('prospective_min_outcomes', 100)))

@app.get('/api/ai/status')
def ai_status():
    from llm_advisors import provider_status
    return jsonify(provider_status(main.ROOT))


@app.get('/api/execution/settings')
def execution_settings():
    from execution_settings import load_execution_settings
    return jsonify(load_execution_settings(main.DATA_ROOT))


@app.put('/api/execution/settings')
def update_execution_settings():
    from execution_settings import save_execution_settings
    try:
        settings = save_execution_settings(
            main.DATA_ROOT, request.get_json(silent=True) or {})
        automation.execution_settings_changed(settings)
        return jsonify(settings)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@app.get('/api/notifications/config')
def notification_config():
    from notification_service import ensure_vapid, notification_status
    try:
        return jsonify(enabled=True, public_key=ensure_vapid(main.DATA_ROOT),
                       status=notification_status())
    except Exception as exc:
        return jsonify(enabled=False, error=str(exc)), 503


@app.post('/api/notifications/subscription')
def add_notification_subscription():
    from notification_service import save_subscription
    try:
        count = save_subscription(main.DATA_ROOT, request.get_json(silent=True) or {})
        return jsonify(enabled=True, subscription_count=count)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@app.delete('/api/notifications/subscription')
def delete_notification_subscription():
    from notification_service import remove_subscription
    data = request.get_json(silent=True) or {}
    try:
        count = remove_subscription(main.DATA_ROOT, data.get('endpoint'))
        return jsonify(enabled=False, subscription_count=count)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

@app.post('/api/ai/review')
def ai_review():
    from llm_advisors import decision_review_key, review_decision
    try:
        data = request.get_json(silent=True) or {}
        decision = json.loads(json.dumps(main.LAST))
        review = review_decision(decision, data.get('provider'), main.ROOT)
        review.setdefault('decision_key', decision_review_key(decision))
        review['automatic'] = False
        automation.cache_review(review)
        if (main.LAST.get('snapshot_id') and
                decision_review_key(main.LAST) == review['decision_key']):
            main.LAST['llm_review'] = review
        return jsonify(review)
    except (ValueError, RuntimeError) as exc:
        return jsonify(error=str(exc)), 400


@app.post('/api/ai/independent')
def ai_independent():
    from llm_advisors import independent_analysis
    try:
        data = request.get_json(silent=True) or {}
        decision = json.loads(json.dumps(main.LAST))
        return jsonify(independent_analysis(
            decision, data.get('provider'), main.ROOT))
    except (ValueError, RuntimeError) as exc:
        return jsonify(error=str(exc)), 400

@app.errorhandler(Exception)
def unexpected_error(exc):
    main.LAST = error_payload(exc)
    return jsonify(main.LAST), 500

automation.start_automation()

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False)
