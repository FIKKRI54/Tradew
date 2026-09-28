"""Persistent runtime choices for automatic entry authorization and trailing."""
import json
from pathlib import Path


DEFAULTS = {
    'entry_mode': 'consensus',
    'provider': 'gemini',
    'auto_trailing': True,
    'paused': False,
}
ENTRY_MODES = {'deterministic', 'reviewed', 'independent', 'consensus'}
PROVIDERS = {'gemini', 'astra'}


def _validated(value):
    settings = {**DEFAULTS, **(value if isinstance(value, dict) else {})}
    if settings['entry_mode'] not in ENTRY_MODES:
        raise ValueError(
            'entry_mode must be deterministic, reviewed, independent, or consensus')
    if settings['provider'] not in PROVIDERS:
        raise ValueError('provider must be gemini or astra')
    if not isinstance(settings['auto_trailing'], bool):
        raise ValueError('auto_trailing must be boolean')
    if not isinstance(settings['paused'], bool):
        raise ValueError('paused must be boolean')
    return {key: settings[key] for key in DEFAULTS}


def load_execution_settings(data_root):
    path = Path(data_root) / 'execution_settings.json'
    try:
        return _validated(json.loads(path.read_text(encoding='utf-8')))
    except FileNotFoundError:
        return dict(DEFAULTS)


def save_execution_settings(data_root, update):
    if not isinstance(update, dict):
        raise ValueError('settings update must be an object')
    unknown = set(update) - set(DEFAULTS)
    if unknown:
        raise ValueError('unknown execution setting: ' + sorted(unknown)[0])
    settings = _validated({**load_execution_settings(data_root), **update})
    path = Path(data_root) / 'execution_settings.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(settings, indent=2), encoding='utf-8')
    temporary.replace(path)
    return settings
