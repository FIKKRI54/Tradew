"""Resolve private runtime configuration with safe example fallbacks."""
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ENV_NAMES = {
    'scan_config.json': 'TRADEW_SCAN_CONFIG',
    'llm_config.json': 'TRADEW_LLM_CONFIG',
}


def config_path(name, root=ROOT):
    root = Path(root)
    override = os.environ.get(ENV_NAMES.get(name, ''))
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            path = root / path
        return path
    private = root / name
    if private.is_file():
        return private
    stem = Path(name).stem
    example = root / f'{stem}.example.json'
    if example.is_file():
        return example
    raise FileNotFoundError(
        f'Missing {name}; copy {example.name} to {name} and configure it.')


def load_json_config(name, root=ROOT):
    return json.loads(config_path(name, root).read_text(encoding='utf-8'))
