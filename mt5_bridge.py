"""Private authenticated HTTP bridge from Linux Tradew to MT5 under Wine."""
import hmac
import os
import threading

from flask import Flask, jsonify, request

from config_paths import load_json_config
from mt5_connector import MT5Adapter


app = Flask(__name__)
BRIDGE_LOCK = threading.Lock()
BROKER = None


def _broker():
    global BROKER
    if BROKER is None:
        BROKER = MT5Adapter()
    return BROKER


def _authorized():
    expected = os.environ.get('TRADEW_BRIDGE_TOKEN', '')
    supplied = request.headers.get('X-Tradew-Bridge-Token', '')
    return bool(expected) and hmac.compare_digest(expected, supplied)


def _execution_configuration_matches(data, local_config):
    """Bind bridge execution to the deployed config and immutable plan risk."""
    if (not local_config.get('automatic_execution') or
            data.get('constraints') != local_config or
            data.get('cost_per_lot') != local_config.get('cost_per_lot')):
        return False
    plan = data.get('plan')
    risk_fraction = data.get('risk_fraction')
    if not isinstance(plan, dict) or plan.get('risk_fraction') != risk_fraction:
        return False
    if plan.get('source') == 'independent_llm':
        from risk_budget import independent_risk_fraction
        try:
            mapped_fraction = independent_risk_fraction(plan.get('risk_tier'))
        except ValueError:
            return False
        return (
            isinstance(plan.get('risk_reason'), str) and
            bool(plan['risk_reason'].strip()) and
            risk_fraction == mapped_fraction and
            risk_fraction <= local_config.get('balance_risk_fraction', 0)
        )
    return risk_fraction == local_config.get('balance_risk_fraction')


@app.before_request
def require_bridge_token():
    if not _authorized():
        return jsonify(error='unauthorized'), 401
    return None


@app.get('/health')
def health():
    from scan_engine import RULE_VERSION
    from independent_validation import VERSION
    return jsonify(status='ok', execution_authority=True,
                   rule_version=RULE_VERSION, independent_version=VERSION)


@app.post('/snapshot')
def snapshot():
    data = request.get_json(silent=True) or {}
    config = data.get('config')
    count = data.get('m5_count', 200)
    if not isinstance(config, dict) or isinstance(count, bool) or not isinstance(count, int):
        return jsonify(error='config object and integer m5_count are required'), 400
    with BRIDGE_LOCK:
        return jsonify(_broker().snapshot(config, m5_count=count))


@app.post('/profit')
def profit():
    data = request.get_json(silent=True) or {}
    required = ('direction', 'symbol', 'volume', 'entry', 'stop')
    if any(key not in data for key in required):
        return jsonify(error='missing profit calculation input'), 400
    with BRIDGE_LOCK:
        value = _broker().profit(*(data[key] for key in required))
    return jsonify(value=value)


@app.post('/margin')
def margin():
    data = request.get_json(silent=True) or {}
    required = ('direction', 'symbol', 'volume', 'entry')
    if any(key not in data for key in required):
        return jsonify(error='missing margin calculation input'), 400
    with BRIDGE_LOCK:
        value = _broker().margin(*(data[key] for key in required))
    return jsonify(value=value)


def _local_configuration(data):
    """Require the API and bridge to agree on the complete deployed config."""
    import json
    from pathlib import Path
    local_config = load_json_config(
        'scan_config.json', Path(__file__).resolve().parent)
    if data.get('constraints') != local_config:
        return None
    if data.get('symbol') != local_config.get('symbol'):
        return None
    return local_config


@app.post('/positions/close-half/preview')
def close_half_preview():
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get('symbol'), str) or not isinstance(data.get('constraints'), dict):
        return jsonify(error='symbol and constraints are required'), 400
    local_config = _local_configuration(data)
    if local_config is None:
        return jsonify(accepted=False, status='blocked_configuration_changed')
    with BRIDGE_LOCK:
        result = _broker().preview_half_close(data['symbol'], local_config)
    return jsonify(result)


@app.post('/positions/close-half')
def close_half_positions():
    data = request.get_json(silent=True) or {}
    if (not isinstance(data.get('symbol'), str) or
            not isinstance(data.get('snapshot_token'), str) or
            not isinstance(data.get('constraints'), dict)):
        return jsonify(error='symbol, snapshot_token, and constraints are required'), 400
    local_config = _local_configuration(data)
    if local_config is None:
        return jsonify(accepted=False, status='blocked_configuration_changed')
    with BRIDGE_LOCK:
        result = _broker().close_half_positions(
            data['symbol'], data['snapshot_token'], local_config)
    return jsonify(result)


@app.post('/execute')
def execute():
    data = request.get_json(silent=True) or {}
    required = ('direction', 'symbol', 'stop', 'target', 'risk_fraction',
                'cost_per_lot', 'deviation_points', 'magic', 'setup_ref', 'plan', 'constraints')
    if any(key not in data for key in required):
        return jsonify(error='missing execution input'), 400
    if data['direction'] not in ('BUY', 'SELL'):
        return jsonify(error='direction must be BUY or SELL'), 400
    if not isinstance(data['setup_ref'], str) or len(data['setup_ref']) != 10:
        return jsonify(error='invalid setup_ref'), 400
    if (isinstance(data['risk_fraction'], bool) or
            not isinstance(data['risk_fraction'], (int, float)) or
            not 0 < data['risk_fraction'] <= 1):
        return jsonify(error='risk_fraction must be in (0, 1]'), 400
    with BRIDGE_LOCK:
        # Deployment configuration is authoritative even if the API has stale settings.
        import json
        from pathlib import Path
        local_config = load_json_config(
            'scan_config.json', Path(__file__).resolve().parent)
        if not _execution_configuration_matches(data, local_config):
            return jsonify(accepted=False, status='blocked_configuration_changed')
        result = _broker().execute_market_order(*(data[key] for key in required))
    return jsonify(result)


@app.post('/pending')
def pending():
    data = request.get_json(silent=True) or {}
    required = ('direction', 'symbol', 'stop', 'target', 'risk_fraction',
                'cost_per_lot', 'magic', 'setup_ref', 'plan', 'constraints')
    if any(key not in data for key in required):
        return jsonify(error='missing pending-order input'), 400
    if data['direction'] not in ('BUY', 'SELL'):
        return jsonify(error='direction must be BUY or SELL'), 400
    if not isinstance(data['setup_ref'], str) or len(data['setup_ref']) != 10:
        return jsonify(error='invalid setup_ref'), 400
    if (isinstance(data['risk_fraction'], bool) or
            not isinstance(data['risk_fraction'], (int, float)) or
            not 0 < data['risk_fraction'] <= 1):
        return jsonify(error='risk_fraction must be in (0, 1]'), 400
    with BRIDGE_LOCK:
        # The bridge's deployed configuration remains authoritative.
        import json
        from pathlib import Path
        local_config = load_json_config(
            'scan_config.json', Path(__file__).resolve().parent)
        if not _execution_configuration_matches(data, local_config):
            return jsonify(accepted=False, status='blocked_configuration_changed')
        result = _broker().place_pending_limit(*(data[key] for key in required))
    return jsonify(result)


@app.post('/pending/revalidate')
def pending_revalidate():
    data = request.get_json(silent=True) or {}
    required = ('ticket', 'symbol', 'magic', 'setup_ref', 'plan', 'expected',
                'constraints')
    if any(key not in data for key in required):
        return jsonify(error='missing pending-supervision input'), 400
    if isinstance(data['ticket'], bool) or not isinstance(data['ticket'], int):
        return jsonify(error='ticket must be an integer'), 400
    if not isinstance(data['setup_ref'], str) or len(data['setup_ref']) != 10:
        return jsonify(error='invalid setup_ref'), 400
    if not isinstance(data['plan'], dict) or not isinstance(data['expected'], dict):
        return jsonify(error='plan and expected must be objects'), 400
    with BRIDGE_LOCK:
        # Do not let an API process supervise orders under stale constraints.
        import json
        from pathlib import Path
        local_config = load_json_config(
            'scan_config.json', Path(__file__).resolve().parent)
        if data['constraints'] != local_config:
            return jsonify(accepted=False, status='blocked_configuration_changed')
        result = _broker().revalidate_pending_limit(
            *(data[key] for key in required))
    return jsonify(result)


@app.post('/position/stop')
def position_stop():
    data = request.get_json(silent=True) or {}
    required = ('ticket', 'symbol', 'stop', 'magic')
    if any(key not in data for key in required):
        return jsonify(error='missing stop-modification input'), 400
    if isinstance(data['ticket'], bool) or not isinstance(data['ticket'], int):
        return jsonify(error='ticket must be an integer'), 400
    with BRIDGE_LOCK:
        result = _broker().modify_position_stop(*(data[key] for key in required))
    return jsonify(result)


@app.errorhandler(Exception)
def bridge_error(exc):
    return jsonify(error=str(exc)), 503


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5010, debug=False)
