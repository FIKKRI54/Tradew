"""Independent LLM reviews of deterministic scans; never an execution path."""
import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from config_paths import load_json_config


ROOT = Path(__file__).resolve().parent
REVIEW_LOCK = threading.Lock()
ALLOWED_ACTIONS = {
    'BUY', 'SELL', 'WAIT', 'CLOSE', 'PARTIAL CLOSE',
    'MOVE SL', 'MOVE SL TO BE', 'TRAIL SL',
}
ALLOWED_VERDICTS = {'AGREE', 'CAUTION', 'DISAGREE'}
INDEPENDENT_ACTIONS = {'BUY', 'SELL', 'WAIT'}
MARKET_BIASES = {'BULLISH', 'BEARISH', 'NEUTRAL'}
SETUP_STATUSES = {'NO_SETUP', 'WATCH', 'READY'}
INDEPENDENT_CANDLE_LIMIT = 24
INDEPENDENT_LEVEL_LIMIT = 6


def decision_review_key(decision):
    """Return a stable key for the material state an LLM actually reviews.

    Live prices and snapshot identifiers deliberately do not participate. Scores
    are grouped into five-point bands so harmless one-point oscillations cannot
    repeatedly launch an expensive browser review.
    """
    def score_band(name):
        value = decision.get(name)
        return int(value // 5 * 5) if isinstance(value, (int, float)) else None

    changes = []
    for change in decision.get('proposed_management_changes') or []:
        changes.append({key: change.get(key) for key in (
            'ticket', 'action', 'volume', 'proposed_stop',
            'proposed_stop_after_reduction', 'required_stop_action')})
    material = {
        'symbol': decision.get('symbol'),
        'action': decision.get('action'),
        'setup_id': decision.get('setup_id'),
        'setup_state': decision.get('setup_state'),
        'entry_plan': decision.get('entry_plan'),
        'buy_score_band': score_band('buy_setup_score'),
        'sell_score_band': score_band('sell_setup_score'),
        'management': changes,
        'data_quality': sorted(decision.get('data_quality') or []),
    }
    return json.dumps(material, sort_keys=True, separators=(',', ':'), allow_nan=False)


def load_llm_config(root=ROOT):
    config = load_json_config('llm_config.json', root)
    if os.environ.get('TRADEW_LLM_DEFAULT_PROVIDER'):
        config['default_provider'] = os.environ['TRADEW_LLM_DEFAULT_PROVIDER'].lower()
    if os.environ.get('TRADEW_CODEX_COMMAND'):
        config['codex']['command'] = os.environ['TRADEW_CODEX_COMMAND']
    if os.environ.get('TRADEW_GEMINI_TRANSPORT'):
        config['gemini']['transport'] = os.environ['TRADEW_GEMINI_TRANSPORT'].lower()
    if not isinstance(config.get('enabled'), bool):
        raise ValueError('llm_config.enabled must be boolean')
    if config.get('mode') != 'single_provider':
        raise ValueError('llm_config.mode must be single_provider')
    if config.get('default_provider') not in ('astra', 'gemini'):
        raise ValueError('llm_config.default_provider must be astra or gemini')
    for provider in ('codex', 'gemini'):
        item = config.get(provider)
        if not isinstance(item, dict) or not isinstance(item.get('enabled'), bool):
            raise ValueError('Invalid LLM provider configuration: ' + provider)
        timeout = item.get('timeout_seconds')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 5 <= timeout <= 600:
            raise ValueError(provider + ' timeout_seconds must be between 5 and 600')
        if not isinstance(item.get('model'), str) or not item['model'].strip():
            raise ValueError(provider + ' model is required')
    gemini = config['gemini']
    if config['codex'].get('reasoning_effort') not in ('low', 'medium', 'high', 'xhigh', 'max'):
        raise ValueError('codex.reasoning_effort must be low, medium, high, xhigh, or max')
    if config['codex'].get('service_tier', 'default') not in ('default', 'fast'):
        raise ValueError('codex.service_tier must be default or fast')
    if gemini.get('transport') not in ('ssh', 'local_worker'):
        raise ValueError('gemini.transport must be ssh or local_worker')
    required = ['remote_python', 'remote_script']
    if gemini.get('transport') == 'ssh':
        required.append('ssh_host')
    if not all(isinstance(gemini.get(key), str) and gemini[key].strip() for key in required):
        raise ValueError('Gemini transport requires valid worker paths and SSH host when remote')
    return config


def review_context(decision):
    """Bound the payload and omit chart history, comments, and account identity."""
    structure = decision.get('structure') or {}
    positions_summary = decision.get('positions_summary') or {}
    positions = [{key: position.get(key) for key in (
        'ticket', 'symbol', 'type', 'volume', 'price_open', 'sl', 'tp', 'profit',
        'setup_binding_error')}
        for position in (positions_summary.get('positions') or [])]
    return {
        'rule_version': decision.get('score_method', '').split(';')[0],
        'snapshot_id': decision.get('snapshot_id'),
        'symbol': decision.get('symbol'), 'account_mode': decision.get('account_mode'),
        'risk_currency': decision.get('risk_currency'), 'session': decision.get('session'),
        'action': decision.get('action'), 'setup_state': decision.get('setup_state'),
        'bid': decision.get('bid'), 'ask': decision.get('ask'), 'spread': decision.get('spread'),
        'entry': decision.get('entry'), 'stop': decision.get('stop'),
        'targets': decision.get('targets'), 'volume': decision.get('volume'),
        'planned_entry_zone': decision.get('planned_entry_zone'),
        'planned_direction': decision.get('planned_direction'),
        'planned_stop': (decision.get('entry_plan') or {}).get('stop'),
        'planned_targets': decision.get('planned_targets'),
        'chart_confirmation': decision.get('chart_confirmation'),
        'order_block': decision.get('order_block'),
        'cash_risk': decision.get('cash_risk'), 'reward_risk': decision.get('reward_risk'),
        'buy_setup_score': decision.get('buy_setup_score'),
        'sell_setup_score': decision.get('sell_setup_score'),
        'score_method': decision.get('score_method'),
        'reasons': decision.get('reasons', []), 'next_trigger': decision.get('next_trigger'),
        'data_quality': decision.get('data_quality', []),
        'timeframes': {name: {
            'trend': context.get('trend'),
            'atr14_simple': context.get('atr14_simple'),
            'volatility_regime': context.get('volatility_regime'),
        } for name, context in structure.items()},
        'positions': positions,
        'pending_order_count': len(positions_summary.get('pending_orders') or []),
        'actual_portfolio_cash_risk': positions_summary.get('actual_portfolio_cash_risk'),
        'planned_cash_risk': positions_summary.get('planned_cash_risk'),
        'proposed_management_changes': decision.get('proposed_management_changes', []),
        'execution_status': decision.get('execution_status'),
    }


def independent_context(decision, candle_limit=INDEPENDENT_CANDLE_LIMIT,
                        level_limit=INDEPENDENT_LEVEL_LIMIT,
                        timestamp_offset_seconds=0,
                        momentum_target_atr_multiple=1.5):
    """Build a blind market payload without the deterministic conclusion."""
    if not isinstance(candle_limit, int) or not 5 <= candle_limit <= 60:
        raise ValueError('candle_limit must be an integer from 5 to 60')
    if not isinstance(level_limit, int) or not 1 <= level_limit <= 12:
        raise ValueError('level_limit must be an integer from 1 to 12')
    if (isinstance(timestamp_offset_seconds, bool) or
            not isinstance(timestamp_offset_seconds, (int, float))):
        raise ValueError('timestamp_offset_seconds must be numeric')
    if (isinstance(momentum_target_atr_multiple, bool) or
            not isinstance(momentum_target_atr_multiple, (int, float)) or
            not math.isfinite(momentum_target_atr_multiple) or
            momentum_target_atr_multiple <= 0):
        raise ValueError('momentum_target_atr_multiple must be positive')
    def normalized_time(value):
        return value - timestamp_offset_seconds if isinstance(value, (int, float)) else value
    chart_data = decision.get('chart_data') or {}
    structure = decision.get('structure') or {}
    chart_analysis = decision.get('chart_analysis') or {}
    target_zones = chart_analysis.get('active_target_zones') or {}
    if not target_zones:
        # Compatibility for archived snapshots created before zone activity was
        # persisted. Never promote explicitly broken or unverified bands.
        quote = decision.get('bid')
        ask = decision.get('ask')
        target_zones = {
            'BUY': [zone for zone in chart_analysis.get('zones', [])
                    if zone.get('status') == 'active' and zone.get('kind') == 'SUPPLY'
                    and isinstance(ask, (int, float)) and zone.get('price', 0) > ask][:6],
            'SELL': [zone for zone in chart_analysis.get('zones', [])
                     if zone.get('status') == 'active' and zone.get('kind') == 'DEMAND'
                     and isinstance(quote, (int, float)) and zone.get('price', 0) < quote][-6:],
        }
    blind_target_zones = {}
    for direction in ('BUY', 'SELL'):
        blind_target_zones[direction] = [
            {'kind': zone.get('kind'), 'low': zone.get('low'),
             'high': zone.get('high'), 'price': zone.get('price'),
             'source_timeframes': zone.get('active_sources') or zone.get('sources') or []}
            for zone in target_zones.get(direction, [])
            if zone.get('status') == 'active' and
            all(isinstance(zone.get(key), (int, float))
                for key in ('low', 'high', 'price'))
        ]
    candles = {}
    levels = {}
    for timeframe in ('M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1'):
        # The last broker bar is still forming and is deliberately excluded.
        completed = (chart_data.get(timeframe) or [])[:-1]
        candles[timeframe] = [
            [normalized_time(bar.get('time')),
             *[bar.get(key) for key in ('open', 'high', 'low', 'close')]]
            for bar in completed[-candle_limit:]
        ]
        pivots = (structure.get(timeframe) or {}).get('pivots') or {}
        levels[timeframe] = {
            side: [[point.get('price'), normalized_time(point.get('time')),
                    normalized_time(point.get('confirmed_bar'))]
                   for point in (pivots.get(side) or [])[-level_limit:]]
            for side in ('high', 'low')
        }
    return {
        'analysis_mode': 'blind_independent_market_read',
        'snapshot_id': decision.get('snapshot_id'),
        'snapshot_time': decision.get('snapshot_time'),
        'time_basis': 'UTC; broker timestamp offset normalized before analysis',
        'symbol': decision.get('symbol'),
        'session': decision.get('session'),
        'quote': {'bid': decision.get('bid'), 'ask': decision.get('ask'),
                  'spread': decision.get('spread')},
        'completed_candles_per_timeframe': candle_limit,
        'candle_fields': ['time', 'open', 'high', 'low', 'close'],
        'candles': candles,
        'structural_level_fields': ['price', 'pivot_time', 'confirmed_bar_time'],
        'confirmed_structural_levels': levels,
        'active_target_zones': blind_target_zones,
        'active_entry_zones': [
            {'kind': z.get('kind'), 'low': z.get('low'), 'high': z.get('high'),
             'source_timeframes': z.get('active_sources') or z.get('sources') or []}
            for z in chart_analysis.get('zones', []) if z.get('status') == 'active'
        ],
        'daily_reference': chart_analysis.get('daily_reference'),
        'm5_atr14': (structure.get('M5') or {}).get('atr14_simple'),
        'planning_contract': {
            'allowed_orders': ['BUY_LIMIT', 'SELL_LIMIT'],
            'expiry_minutes': [5, 30],
            'risk_tiers': {'LOW': '10%', 'MEDIUM': '20%', 'HIGH': '30%'},
            'risk_tier_rule': 'READY requires one explicit tier; WAIT requires null; there is no default tier',
            'price_source': 'prices must be grounded in supplied candles, quote, or levels; the only derived exceptions are an exact completed-M5 body midpoint entry and the fixed M5-ATR fallback target',
            'fallback_target_atr_multiple': float(momentum_target_atr_multiple),
            'take_profit_rule': 'Prefer an active opposing M5-H1 zone; otherwise use an unbroken confirmed M5-H1 pivot; only when neither exists use exactly current bid plus/minus fallback_target_atr_multiple times M5 ATR in the trade direction',
            'tp1_source_rule': 'H4/D1-only zones and pivots are context or runner objectives, not intraday TP1 evidence',
            'confirmation_rule': 'Latest completed M5 must show directional engulfing, rejection, soldiers/crows or displacement touching/crossing confirmation_level; M1 alone is insufficient',
            'entry_rule': 'Entry normally matches active M5+ demand/low for BUY or supply/high for SELL; after an aligned latest completed-M5 displacement it may instead equal that candle open or exact 50% body midpoint; no quote-interpolated ladder',
            'entry_reachability_rule': 'The backend rejects short-lived limits beyond both recent M1 pullback behavior and the outer M5-ATR cap: 0.875 ATR for 5m, 1.125 ATR for 15m, 1.5 ATR for 30m; return WAIT instead of READY for a substantial rebound',
            'market_freshness_rule': 'Return WAIT during rollover/closed-market windows and immediately after a large opening gap until three fresh M5 candles complete',
            'stop_rule': 'SL must be beyond structural invalidation and the confirmation pattern, with distance at least max(0.5*M5 ATR14, 2*spread); never invent a tighter SL for reward/risk',
        },
        'data_quality': decision.get('data_quality', []),
    }


def _prompt(context):
    return (
        'You are an independent risk reviewer for an XAUUSD research scanner. '
        'Review only the supplied deterministic decision. Do not use tools, browse, '
        'invent prices, infer missing news, or claim execution occurred. The deterministic '
        'engine remains authoritative and all actions are advisory. Mark DISAGREE when the '
        'action contradicts its own data or risk state; CAUTION for material uncertainty; '
        'otherwise AGREE. recommended_action is your advisory cross-check, not an order. '
        'For ARMED_LIMIT, review the conditional planned_direction, planned_stop, '
        'planned_entry_zone and planned_targets. AGREE with BUY/SELL means approving '
        'that plan only if price retests its zone before expiry and fresh checks pass. '
        'WAIT remains appropriate if the conditional plan itself is unsupported. '
        'Return only JSON matching the provided schema.\n\nDECISION_CONTEXT:\n' +
        json.dumps(context, allow_nan=False, separators=(',', ':')))


def _independent_prompt(context):
    return (
        'You are producing a hypothetical, structured XAUUSD research scenario for a '
        'scanner; this is not personal financial advice and you do not execute '
        'orders. The deterministic '
        'scanner action, scores, reasons, setup state, entries, stops, and targets are '
        'intentionally withheld and must not influence you. Analyze only the supplied '
        'completed candles, confirmed structural levels, quote, spread, session, and '
        'data-quality flags. Treat D1 as macro context and major boundaries only, H4/H1 '
        'as directional context, M30/M15 as setup development, '
        'M5 as completed-candle confirmation, and M1 as timing. Do not browse, use tools, '
        'invent candles, infer news, claim execution, or infer the hidden result. '
        'Return READY with BUY or SELL only for a complete native limit-order plan. '
        'BUY requires BUY_LIMIT below the current ask with stop_loss and '
        'structural_invalidation below entry_price and take_profit above it. SELL requires '
        'SELL_LIMIT above the current bid with stop_loss and structural_invalidation above '
        'entry_price and take_profit below it. confirmation_level must be a completed-candle '
        'or confirmed structural price that the market currently holds beyond in the trade '
        'direction. Prefer a take_profit inside a supplied active opposing M5, M15, M30, '
        'or H1 zone. If no such zone exists, take_profit may equal an unbroken confirmed '
        'M5-H1 opposing pivot. Only when neither intraday target exists may it equal the '
        'exact current-bid projection using fallback_target_atr_multiple and M5 ATR14 from '
        'the planning_contract. H4/D1-only targets remain context or runner objectives. '
        'Every plan price must be '
        'supported by the planning_contract. A READY plan needs a latest completed M5 '
        'engulfing/rejection, soldiers/crows or displacement at confirmation_level. '
        'M1-only movement cannot authorize a trade. D1 must never authorize an entry by '
        'itself, but D1 opposition alone is macro risk context and must not force WAIT or '
        'extra entry confirmation. If both H1 and H4 last completed closes are against '
        'the trade relative to their closes two bars earlier, require an M5 '
        'engulfing/rejection closing beyond the previous M5 extreme AND M15-or-higher '
        'entry structure. A plan against D1 alone may still be READY, but its risk tier '
        'must not exceed MEDIUM. Entries normally match active same-side zones or '
        'unbroken M5+ pivots (M15+ only when both H1 and H4 oppose). When the latest '
        'completed M5 candle itself has '
        'directional displacement aligned with the higher-timeframe context, a shallow '
        'pullback entry may instead equal that candle open or its exact 50% body midpoint. '
        'BUY chart levels are bid prices, so allow spread when selecting an executable '
        'entry. Stop must be at or beyond structural '
        'invalidation and outside the last two M5 extremes, at least 0.5 M5 ATR14 '
        'and two spreads from entry. If unsupported, WAIT; do not tighten stops to '
        'manufacture reward/risk. The original entry will be preserved; extra layers '
        'are allowed only at separately verified better-priced structural levels. '
        'A structural level is not automatically reachable: compare its distance with '
        'recent M1 pullbacks and the expiry-specific M5 ATR cap supplied in the planning '
        'contract. If entry needs a substantial rebound, return WAIT and let a fresh '
        'review occur near the zone. Return WAIT during rollover or immediately after a '
        'large opening gap. All plan prices must be grounded in the supplied candles, '
        'quote, or confirmed levels, except the exact body-midpoint entry and exact fixed '
        'ATR target described above. Use a 5-30 minute expiry. If any complete plan element is '
        'unsupported, return WAIT with WATCH or NO_SETUP and trade_plan null. '
        'For every READY plan, explicitly choose exactly one risk_tier from LOW, MEDIUM, '
        'or HIGH. LOW maps to a 10% account-balance stop-risk budget, MEDIUM to 20%, and '
        'HIGH to 30%. Do not output a number and do not select a fallback tier. Choose the '
        'tier from the strength and agreement of the supplied multi-timeframe structure, '
        'entry confirmation, target quality, volatility/spread conditions, and risk flags. '
        'Use HIGH only when the supplied evidence is exceptionally aligned and has no '
        'material structural conflict; use MEDIUM for clear but imperfect alignment; use '
        'LOW for a valid READY plan with meaningful uncertainty. For WATCH or NO_SETUP, '
        'risk_tier must be null. Always explain the selection in risk_reason. '
        'The plain_language_summary '
        'must use everyday language for a non-trader, avoid unexplained trading terms, and '
        'state what price appears to be doing, why the action is BUY/SELL/WAIT, and what '
        'would need to happen next in no more than three short sentences. Return only JSON matching '
        'the provided schema.\n\nBLIND_MARKET_CONTEXT:\n' +
        json.dumps(context, allow_nan=False, separators=(',', ':')))


def _validated(payload, provider, model, elapsed):
    if not isinstance(payload, dict):
        raise ValueError('Model response is not an object')
    if payload.get('verdict') not in ALLOWED_VERDICTS:
        raise ValueError('Invalid verdict')
    if payload.get('recommended_action') not in ALLOWED_ACTIONS:
        raise ValueError('Invalid recommended_action')
    if not isinstance(payload.get('summary'), str) or not payload['summary'].strip():
        raise ValueError('Missing summary')
    for key in ('risk_flags', 'conditions_to_proceed'):
        if not isinstance(payload.get(key), list) or not all(
                isinstance(item, str) for item in payload[key]):
            raise ValueError('Invalid ' + key)
    return {'provider': provider, 'model': model, 'status': 'ok',
            'elapsed_seconds': round(elapsed, 3), **payload}


def _validated_independent(payload, provider, model, elapsed):
    if not isinstance(payload, dict):
        raise ValueError('Model response is not an object')
    if payload.get('market_bias') not in MARKET_BIASES:
        raise ValueError('Invalid market_bias')
    if payload.get('recommended_action') not in INDEPENDENT_ACTIONS:
        raise ValueError('Invalid recommended_action')
    if payload.get('setup_status') not in SETUP_STATUSES:
        raise ValueError('Invalid setup_status')
    for key in ('summary', 'plain_language_summary', 'entry_condition',
                'invalidation_condition', 'risk_reason'):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise ValueError('Missing ' + key)
    for key in ('evidence', 'objectives', 'risk_flags'):
        if not isinstance(payload.get(key), list) or not all(
                isinstance(item, str) for item in payload[key]):
            raise ValueError('Invalid ' + key)
    plan = payload.get('trade_plan')
    risk_tier = payload.get('risk_tier')
    ready = payload['setup_status'] == 'READY'
    directional = payload['recommended_action'] in ('BUY', 'SELL')
    if ready != directional:
        raise ValueError('READY requires BUY or SELL and directional action requires READY')
    if not ready:
        if risk_tier is not None:
            raise ValueError('Non-ready analysis must have null risk_tier')
        if plan is not None:
            raise ValueError('Non-ready analysis must have null trade_plan')
    else:
        from risk_budget import INDEPENDENT_RISK_FRACTIONS
        if risk_tier not in INDEPENDENT_RISK_FRACTIONS:
            raise ValueError('READY analysis requires an explicit valid risk_tier')
        if not isinstance(plan, dict):
            raise ValueError('READY analysis requires trade_plan')
        required = ('direction', 'order_type', 'entry_price', 'stop_loss',
                    'take_profit', 'confirmation_level', 'structural_invalidation',
                    'expires_in_minutes')
        if any(key not in plan for key in required):
            raise ValueError('Incomplete trade_plan')
        direction = payload['recommended_action']
        if plan['direction'] != direction or plan['order_type'] != direction+'_LIMIT':
            raise ValueError('trade_plan direction or order type mismatch')
        prices = [plan[key] for key in required[2:-1]]
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and
                   math.isfinite(value) and value > 0 for value in prices):
            raise ValueError('Invalid trade_plan price')
        expiry = plan['expires_in_minutes']
        if isinstance(expiry, bool) or not isinstance(expiry, int) or not 5 <= expiry <= 30:
            raise ValueError('Invalid trade_plan expiry')
        sign = 1 if direction == 'BUY' else -1
        if (sign*(plan['entry_price']-plan['stop_loss']) <= 0 or
                sign*(plan['entry_price']-plan['structural_invalidation']) <= 0 or
                sign*(plan['take_profit']-plan['entry_price']) <= 0):
            raise ValueError('Invalid trade_plan geometry')
    return {'provider': provider, 'model': model, 'status': 'ok',
            'elapsed_seconds': round(elapsed, 3), **payload}


def _failure(provider, model, exc, elapsed=0):
    message = str(exc).strip().replace('\n', ' ')
    return {'provider': provider, 'model': model, 'status': 'unavailable',
            'elapsed_seconds': round(elapsed, 3), 'error': message[-500:] or 'Unknown error'}


def _codex_environment(environ=os.environ):
    """Give the CLI enough OS context for stored login, but no API secrets."""
    allowed = ('SYSTEMROOT', 'WINDIR', 'PATH', 'PATHEXT', 'TEMP', 'TMP',
               'USERPROFILE', 'HOME', 'HOMEDRIVE', 'HOMEPATH', 'LOCALAPPDATA',
               'APPDATA', 'CODEX_HOME', 'USERNAME')
    return {name: environ[name] for name in allowed if environ.get(name)}


def review_with_codex(context, config, root=ROOT, runner=subprocess.run,
                      prompt_factory=None, schema_filename='llm_review_schema.json',
                      validator=None):
    started = time.monotonic()
    model = config['model']
    effort = config.get('reasoning_effort', 'low')
    service_tier = config.get('service_tier', 'default')
    try:
        command = shutil.which(config.get('command', 'codex'))
        if not command:
            raise RuntimeError('Codex CLI is not installed or not on PATH')
        prompt_factory = prompt_factory or _prompt
        validator = validator or _validated
        schema = str(Path(root) / schema_filename)
        completed = runner([
            command, 'exec', '--ephemeral', '--ignore-user-config', '--ignore-rules',
            '--skip-git-repo-check', '--sandbox', 'read-only',
            '--model', model, '--config', f'model_reasoning_effort="{effort}"',
            '--config', f'features.fast_mode={str(service_tier == "fast").lower()}',
            '--config', f'service_tier="{service_tier}"',
            '--output-schema', schema, '--color', 'never',
            '--cd', tempfile.gettempdir(), '-'], input=prompt_factory(context), text=True,
            capture_output=True, timeout=config['timeout_seconds'], check=False,
            cwd=tempfile.gettempdir(), env=_codex_environment())
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr or completed.stdout or 'Codex CLI failed')
        payload = json.loads(completed.stdout.strip())
        result = validator(payload, 'codex_cli', model, time.monotonic()-started)
        result['reasoning_effort'] = effort
        result['requested_service_tier'] = service_tier
        return result
    except Exception as exc:
        return _failure('codex_cli', model, exc, time.monotonic()-started)


def _review_with_gemini_worker(context, config, root=ROOT, runner=subprocess.run,
                               prompt_factory=None,
                               schema_filename='llm_review_schema.json',
                               validator=None):
    started = time.monotonic()
    model = config['model']
    try:
        prompt_factory = prompt_factory or _prompt
        validator = validator or _validated
        schema = json.loads((Path(root) / schema_filename).read_text(encoding='utf-8'))
        web_prompt = (prompt_factory(context) + '\n\nREQUIRED_JSON_SCHEMA:\n' +
                      json.dumps(schema, separators=(',', ':')))
        request = json.dumps({
            'model': model, 'prompt': web_prompt, 'schema': schema,
            'timeout_seconds': config['timeout_seconds'],
        }, allow_nan=False)
        if config['transport'] == 'local_worker':
            command = [config['remote_python'], config['remote_script']]
        else:
            ssh = shutil.which(config.get('ssh_command', 'ssh'))
            if not ssh:
                raise RuntimeError('SSH client is not installed or not on PATH')
            command = [ssh, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                       config['ssh_host'], config['remote_python'], config['remote_script']]
        completed = runner(command,
            input=request, text=True, capture_output=True,
            # Browser navigation/login checks can consume about a minute before
            # Gemini receives the prompt. The worker may also make one fresh-tab
            # retry when Gemini itself returns its generic transient error page.
            timeout=config['timeout_seconds']*2+180, check=False,
            env=os.environ.copy())
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr or completed.stdout or 'VPS Gemini worker failed')
        return validator(json.loads(completed.stdout), 'gemini_vps', model,
                         time.monotonic()-started)
    except Exception as exc:
        return _failure('gemini_vps', model, exc, time.monotonic()-started)


def _review_with_gemini_ssh(context, config, root=ROOT, runner=subprocess.run):
    """Backward-compatible entry point retained for the existing contract tests."""
    remote = dict(config)
    remote['transport'] = 'ssh'
    return _review_with_gemini_worker(context, remote, root, runner)


def review_with_gemini(context, config, root=ROOT, prompt_factory=None,
                       schema_filename='llm_review_schema.json', validator=None):
    return _review_with_gemini_worker(
        context, config, root, prompt_factory=prompt_factory,
        schema_filename=schema_filename, validator=validator)


def _assessment(review, deterministic_action):
    if review['status'] != 'ok':
        status = 'UNAVAILABLE'
    elif review['verdict'] == 'DISAGREE':
        status = 'DISAGREEMENT'
    elif review['verdict'] == 'CAUTION' or review['recommended_action'] != deterministic_action:
        status = 'CAUTION'
    else:
        status = 'AGREEMENT'
    return {
        'status': status, 'deterministic_action': deterministic_action,
        'reviewed_action': review.get('recommended_action'),
        'execution_authority': False,
        'note': 'LLM output is an advisory cross-check and cannot place or modify orders.',
    }


def review_decision(decision, provider=None, root=ROOT):
    if not REVIEW_LOCK.acquire(blocking=False):
        raise RuntimeError('An LLM review is already in progress')
    try:
        config = load_llm_config(root)
        if not config['enabled']:
            raise RuntimeError('LLM review is disabled')
        provider = (provider or config['default_provider']).lower()
        if provider not in ('astra', 'gemini'):
            raise ValueError('provider must be astra or gemini')
        context = review_context(decision)
        if not context.get('snapshot_id'):
            raise ValueError('Run a broker scan before requesting an LLM review')
        if provider == 'astra':
            if not config['codex']['enabled']:
                raise RuntimeError('Astra review is disabled')
            review = review_with_codex(context, config['codex'], root)
        else:
            if not config['gemini']['enabled']:
                raise RuntimeError('Gemini review is disabled')
            review = review_with_gemini(context, config['gemini'], root)
        result = {
            'snapshot_id': context['snapshot_id'], 'reviewed_at': time.time(),
            'decision_key': decision_review_key(decision),
            'selected_provider': provider, 'review': review,
            'assessment': _assessment(review, context.get('action')),
        }
        data_root = Path(os.environ.get('TRADEW_DATA_ROOT', root))
        data_root.mkdir(parents=True, exist_ok=True)
        with (data_root / 'llm_reviews.jsonl').open('a', encoding='utf-8') as log:
            log.write(json.dumps({'context': context, **result}, allow_nan=False)+'\n')
        return result
    finally:
        REVIEW_LOCK.release()


def independent_analysis(decision, provider=None, root=ROOT):
    """Run a blind market read that never receives the deterministic conclusion."""
    if not REVIEW_LOCK.acquire(blocking=False):
        raise RuntimeError('An LLM review is already in progress')
    try:
        config = load_llm_config(root)
        if not config['enabled']:
            raise RuntimeError('LLM review is disabled')
        provider = (provider or config['default_provider']).lower()
        if provider not in ('astra', 'gemini'):
            raise ValueError('provider must be astra or gemini')
        market_config = load_json_config('scan_config.json', root)
        context = independent_context(
            decision,
            timestamp_offset_seconds=market_config.get('timestamp_offset_seconds', 0),
            momentum_target_atr_multiple=market_config.get(
                'momentum_target_atr_multiple', 1.5))
        context['planning_contract'].update(
            minimum_layer_reward_risk=market_config['minimum_ladder_reward_risk'],
            tp1_close_fraction=(market_config['take_profit_close_fraction']
                               if market_config.get('runner_enabled') else 1),
            reward_basis='TP1 closed portion only, reserving costs for all volume; no assumed runner profit')
        if not context.get('snapshot_id'):
            raise ValueError('Run a broker scan before requesting independent analysis')
        if not any(context['candles'].values()):
            raise ValueError('Completed candle data is unavailable')
        options = dict(
            prompt_factory=_independent_prompt,
            schema_filename='llm_independent_schema.json',
            validator=_validated_independent,
        )
        if provider == 'astra':
            if not config['codex']['enabled']:
                raise RuntimeError('Astra review is disabled')
            analysis = review_with_codex(context, config['codex'], root, **options)
        else:
            if not config['gemini']['enabled']:
                raise RuntimeError('Gemini review is disabled')
            analysis = review_with_gemini(context, config['gemini'], root, **options)
        result = {
            'snapshot_id': context['snapshot_id'], 'analyzed_at': time.time(),
            'decision_key': decision_review_key(decision),
            'selected_provider': provider, 'analysis_mode': context['analysis_mode'],
            'blind_input': True, 'candle_limit': INDEPENDENT_CANDLE_LIMIT,
            'level_limit': INDEPENDENT_LEVEL_LIMIT, 'analysis': analysis,
        }
        data_root = Path(os.environ.get('TRADEW_DATA_ROOT', root))
        data_root.mkdir(parents=True, exist_ok=True)
        with (data_root / 'llm_independent_analyses.jsonl').open('a', encoding='utf-8') as log:
            log.write(json.dumps({'context': context, **result}, allow_nan=False)+'\n')
        return result
    finally:
        REVIEW_LOCK.release()


def provider_status(root=ROOT):
    config = load_llm_config(root)
    command = shutil.which(config['codex'].get('command', 'codex'))
    codex = {'model': config['codex']['model'],
             'reasoning_effort': config['codex']['reasoning_effort'],
             'service_tier': config['codex'].get('service_tier', 'default'),
             'fast_mode': config['codex'].get('service_tier', 'default') == 'fast',
             'installed': command is not None,
             'authenticated': False}
    if command:
        try:
            result = subprocess.run([command, 'login', 'status'], text=True,
                                    capture_output=True, timeout=10, check=False,
                                    env=_codex_environment())
            codex['authenticated'] = result.returncode == 0 and 'logged in' in (
                result.stdout+result.stderr).lower()
        except (OSError, subprocess.SubprocessError):
            pass
    transport = config['gemini']['transport']
    gemini = {'model': config['gemini']['model'], 'transport': transport, 'ready': False}
    if transport == 'ssh':
        gemini['host'] = config['gemini']['ssh_host']
        ssh = shutil.which(config['gemini'].get('ssh_command', 'ssh'))
        command = None if not ssh else [
            ssh, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
            config['gemini']['ssh_host'], config['gemini']['remote_python'],
            config['gemini']['remote_script'], '--status']
    else:
        command = [config['gemini']['remote_python'], config['gemini']['remote_script'], '--status']
    if command:
        try:
            status = subprocess.run(command, text=True, capture_output=True,
                                    timeout=15, check=False, env=os.environ.copy())
            if status.returncode == 0:
                gemini.update(json.loads(status.stdout))
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return {'enabled': config['enabled'], 'mode': config['mode'],
            'default_provider': config['default_provider'], 'astra': codex, 'gemini': gemini,
            'execution_authority': False}
