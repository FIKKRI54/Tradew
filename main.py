"""Serialized advisory scans with no order execution path."""
import json
import math
import os
import threading
from pathlib import Path
from mt5_connector import create_mt5_adapter
from scan_engine import evaluate, validate, RULE_VERSION
from setup_tracker import SetupTracker
from audit_storage import append_record
from limit_ladder import ladder_settings
from config_paths import load_json_config

ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get('TRADEW_DATA_ROOT', ROOT))
DATA_ROOT.mkdir(parents=True, exist_ok=True)
LOCK = threading.Lock()
REPLAY_LOCK = threading.Lock()
LAST = {'execution_status': 'advisory_only', 'action': 'WAIT', 'reasons': ['Waiting for first automatic scan']}
PREVIOUS_TICK = None
LAST_AUDIT_SIGNATURE = None


def decision_audit_signature(result):
    """Exclude per-tick display data so continuous scans log material changes only."""
    positions = result.get('positions_summary') or {}
    material = {
        'action': result.get('action'), 'setup_id': result.get('setup_id'),
        'setup_state': result.get('setup_state'),
        'buy_score_band': int((result.get('buy_setup_score') or 0) // 5) * 5,
        'sell_score_band': int((result.get('sell_setup_score') or 0) // 5) * 5,
        'reasons': result.get('reasons'), 'next_trigger': result.get('next_trigger'),
        'data_quality': result.get('data_quality'),
        'proposed_management_changes': result.get('proposed_management_changes'),
        'positions': [{key: position.get(key) for key in ('ticket', 'volume', 'sl', 'tp')}
                      for position in positions.get('positions') or []],
        'risk_state': ('unknown' if positions.get('actual_portfolio_cash_risk') is None
                       else int(positions.get('actual_portfolio_cash_risk') // 5) * 5),
    }
    return json.dumps(material, sort_keys=True, separators=(',', ':'), allow_nan=False)

def load_config():
    config = load_json_config('scan_config.json', ROOT)
    for key, value in config.items():
        if key in ('symbol', 'expected_server', 'expected_currency', 'expected_mode',
                   'profile_name', 'parameter_status', 'automatic_execution', 'runner_enabled',
                   'limit_ladder_weights') or value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError('Invalid numeric configuration: ' + key)
        if key != 'timestamp_offset_seconds' and value < 0:
            raise ValueError('Negative configuration: ' + key)
    if config.get('max_margin_fraction') is not None and not 0 < config['max_margin_fraction'] <= 1:
        raise ValueError('max_margin_fraction must be in (0, 1]')
    if not isinstance(config.get('automatic_execution'), bool):
        raise ValueError('automatic_execution must be boolean')
    if not isinstance(config.get('runner_enabled'), bool):
        raise ValueError('runner_enabled must be boolean')
    close_fraction = config.get('take_profit_close_fraction')
    if (isinstance(close_fraction, bool) or not isinstance(close_fraction, (int, float)) or
            not 0 < close_fraction < 1):
        raise ValueError('take_profit_close_fraction must be in (0, 1)')
    fraction = config.get('balance_risk_fraction')
    if (isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or
            not 0 < fraction <= 1):
        raise ValueError('balance_risk_fraction must be in (0, 1]')
    expiry = config.get('confirmation_expiry_bars')
    approval_age = config.get('llm_approval_max_age_seconds')
    if approval_age is None or not 1 <= approval_age <= 3600:
        raise ValueError('llm_approval_max_age_seconds must be from 1 to 3600')
    if expiry is not None and (not isinstance(expiry, int) or not 1 <= expiry <= 50):
        raise ValueError('confirmation_expiry_bars must be an integer from 1 to 50')
    lookback = config.get('order_block_lookback_bars')
    if lookback is not None and (not isinstance(lookback, int) or not 2 <= lookback <= 50):
        raise ValueError('order_block_lookback_bars must be an integer from 2 to 50')
    body_fraction = config.get('pattern_body_min_fraction')
    if body_fraction is not None and not 0 < body_fraction <= 1:
        raise ValueError('pattern_body_min_fraction must be in (0, 1]')
    replay_horizon = config.get('replay_horizon_bars')
    if replay_horizon is not None and (not isinstance(replay_horizon, int) or
                                       not 1 <= replay_horizon <= 10000):
        raise ValueError('replay_horizon_bars must be an integer from 1 to 10000')
    minimum = config.get('prospective_min_outcomes')
    if minimum is not None and (not isinstance(minimum, int) or not 1 <= minimum <= 100000):
        raise ValueError('prospective_min_outcomes must be an integer from 1 to 100000')
    for key in ('volatility_low_ratio', 'volatility_high_ratio'):
        value = config.get(key)
        if value is not None and value <= 0:
            raise ValueError(key + ' must be positive')
    momentum_target = config.get('momentum_target_atr_multiple', 1.5)
    if (isinstance(momentum_target, bool) or
            not isinstance(momentum_target, (int, float)) or
            not math.isfinite(momentum_target) or momentum_target <= 0):
        raise ValueError('momentum_target_atr_multiple must be positive')
    if (config.get('volatility_low_ratio') is not None and
            config.get('volatility_high_ratio') is not None and
            config['volatility_low_ratio'] >= config['volatility_high_ratio']):
        raise ValueError('volatility_low_ratio must be below volatility_high_ratio')
    if config.get('expected_mode') not in (None, 'demo', 'live'):
        raise ValueError('expected_mode must be demo or live')
    if not 0 <= config.get('minimum_setup_score', -1) <= 100:
        raise ValueError('minimum_setup_score must be from 0 to 100')
    if config.get('higher_timeframe_opposition_block_threshold') not in (0, 1, 2, 3):
        raise ValueError('higher_timeframe_opposition_block_threshold must be an integer from 0 to 3')
    d1_penalty = config.get('counter_d1_score_penalty')
    if (isinstance(d1_penalty, bool) or
            not isinstance(d1_penalty, (int, float)) or
            not math.isfinite(d1_penalty) or not 0 <= d1_penalty <= 100):
        raise ValueError('counter_d1_score_penalty must be from 0 to 100')
    d1_risk = config.get('counter_d1_risk_multiplier')
    if (isinstance(d1_risk, bool) or
            not isinstance(d1_risk, (int, float)) or
            not math.isfinite(d1_risk) or not 0 < d1_risk <= 1):
        raise ValueError('counter_d1_risk_multiplier must be above 0 and at most 1')
    for key in ('minimum_ladder_reward_risk', 'minimum_layer_stop_atr_fraction',
                'minimum_ladder_span_atr_fraction',
                'minimum_ladder_span_spread_multiple',
                'independent_reachability_base_atr',
                'independent_reachability_per_minute_atr',
                'independent_reachability_floor_atr',
                'independent_reachability_empirical_multiplier',
                'independent_gap_atr_multiple'):
        if config.get(key, 0) <= 0:
            raise ValueError(key + ' must be positive')
    for key in ('independent_reachability_m1_lookback',
                'independent_gap_cooldown_m5_bars'):
        value = config.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(key + ' must be a positive integer')
    if config.get('max_ladder_volume_ratio', 0) < 1:
        raise ValueError('max_ladder_volume_ratio must be at least 1')
    for key in ('max_consecutive_losses', 'max_session_entries'):
        value = config.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(key + ' must be a positive integer')
    ladder_settings(config)
    return config

def scan(audit_policy='full', lock_timeout=0):
    global LAST, PREVIOUS_TICK, LAST_AUDIT_SIGNATURE
    if audit_policy not in ('full', 'changes'):
        raise ValueError('audit_policy must be full or changes')
    if (isinstance(lock_timeout, bool) or not isinstance(lock_timeout, (int, float)) or
            not math.isfinite(lock_timeout) or lock_timeout < 0):
        raise ValueError('lock_timeout must be a non-negative finite number')
    acquired = (LOCK.acquire(timeout=lock_timeout) if lock_timeout > 0
                else LOCK.acquire(blocking=False))
    if not acquired:
        raise RuntimeError('A scan is already in progress')
    broker = None
    try:
        config = load_config()
        broker = create_mt5_adapter()
        snapshot = broker.snapshot(config)
        tracker = SetupTracker(DATA_ROOT)
        tracker.attach(snapshot)
        result = evaluate(snapshot, config, broker)
        if any('synchronized' in f or 'during collection' in f for f in result['data_quality']):
            snapshot = broker.snapshot(config)
            tracker.attach(snapshot)
            result = evaluate(snapshot, config, broker)
        tick = snapshot.get('tick')
        if tick:
            marker = (snapshot.get('symbol'), snapshot.get('account', {}).get('login'), tick.get('time_msc', tick['time'] * 1000))
            if PREVIOUS_TICK and marker == PREVIOUS_TICK[0] and snapshot['retrieval_end_utc'] - PREVIOUS_TICK[1] > (config.get('max_tick_age_seconds') or 0):
                result.update(action='WAIT', reasons=['Feed stalled since preceding scan'], next_trigger='Advancing broker tick required')
                result['data_quality'].append('Stalled feed')
                for field in ('entry', 'stop', 'targets', 'volume', 'cash_risk', 'reward_risk'):
                    result.pop(field, None)
                result['proposed_management_changes'] = []
            if PREVIOUS_TICK is None or marker != PREVIOUS_TICK[0]:
                PREVIOUS_TICK = (marker, snapshot['retrieval_end_utc'])
        # Display the same snapshot used by the evaluator; never fetch a second chart feed.
        result['chart_data'] = snapshot.get('bars', {})
        account = snapshot.get('account', {})
        result['account_summary'] = {key: account.get(key) for key in ('equity', 'balance', 'currency')}
        result['setup_tracking'] = snapshot.get('setup_tracking', {})
        tracker.enforce_terminal_state(result)
        tracker.record(result, snapshot)
        signature = decision_audit_signature(result)
        if audit_policy == 'full' or signature != LAST_AUDIT_SIGNATURE:
            record = {'rule_version': RULE_VERSION, 'configuration': config,
                      'snapshot': snapshot, 'decision': result, 'outcome': None,
                      'audit_reason': 'manual' if audit_policy == 'full' else 'material_change'}
            append_record(DATA_ROOT, record)
            LAST_AUDIT_SIGNATURE = signature
        LAST = result
        return result
    finally:
        if broker:
            broker.close()
        LOCK.release()


def historical_replay(count=5000, initial_balance=None):
    """Capture broker history, then simulate without holding the live scanner lock."""
    if isinstance(count, bool) or not isinstance(count, int) or not 500 <= count <= 20000:
        raise ValueError('Replay count must be an integer from 500 to 20000')
    if initial_balance is not None and (isinstance(initial_balance, bool) or
            not isinstance(initial_balance, (int, float)) or
            not math.isfinite(initial_balance) or initial_balance <= 0):
        raise ValueError('initial_balance must be positive and finite')
    if not REPLAY_LOCK.acquire(blocking=False):
        raise RuntimeError('A replay is already in progress')
    broker = None
    capture_locked = False
    try:
        from replay_engine import replay_history, ReplayContract
        from execution_settings import load_execution_settings
        capture_locked = LOCK.acquire(blocking=False)
        if not capture_locked:
            raise RuntimeError('A scan is already in progress; retry replay capture')
        config = load_config()
        broker = create_mt5_adapter()
        snapshot = broker.snapshot(config, m5_count=count)
        flags = validate(snapshot, config)
        if flags:
            raise RuntimeError('Replay blocked by snapshot checks: ' + '; '.join(flags))
        symbol = snapshot['symbol']
        model = ReplayContract(broker, symbol, snapshot['tick']['bid'],
                               snapshot['specifications'][symbol]['trade_tick_size'])
        broker.close()
        broker = None
        LOCK.release()
        capture_locked = False
        settings = load_execution_settings(DATA_ROOT)
        if initial_balance is not None:
            config = {**config, 'replay_initial_balance': initial_balance}
        bars = snapshot['bars']['M5'][:-1][-count:]
        report = replay_history(snapshot, config, model, count, settings['auto_trailing'])
        report['selected_live_authorization_mode'] = settings['entry_mode']
        report.update({
            'rule_version': RULE_VERSION, 'symbol': snapshot['symbol'],
            'account_mode': snapshot['account_mode'],
            'risk_currency': snapshot['account']['currency'],
            'bar_count': len(bars), 'start_time': bars[0]['time'],
            'end_time': bars[-1]['time'],
            'parameter_status': config.get('parameter_status'),
        })
        with (DATA_ROOT / 'replay_results.jsonl').open('a', encoding='utf-8') as log:
            log.write(json.dumps(report, allow_nan=False) + '\n')
        return report
    finally:
        if broker:
            broker.close()
        if capture_locked:
            LOCK.release()
        REPLAY_LOCK.release()

if __name__ == '__main__':
    print(json.dumps(scan(), indent=2))
