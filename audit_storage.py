"""Bounded local audit logs with verified compressed archive uploads."""
import gzip
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


_lock = threading.Lock()
_wake = threading.Event()
_started = False
_root = None
_status = {
    'enabled': True,
    'active_bytes': 0,
    'pending_archives': 0,
    'drive_configured': False,
    'uploaded_count': 0,
    'last_rotation_at': None,
    'last_upload_at': None,
    'last_error': None,
}


def _integer(name, default, minimum):
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


MAX_ACTIVE_BYTES = _integer('TRADEW_AUDIT_ROTATE_BYTES', 25 * 1024 * 1024, 1024 * 1024)
UPLOAD_INTERVAL = _integer('TRADEW_AUDIT_UPLOAD_INTERVAL_SECONDS', 60, 10)


def _paths(root):
    root = Path(root)
    return root / 'decisions.jsonl', root / 'audit-archives'


def _timeframe_summary(context):
    pivots = context.get('pivots') or {}
    return {
        key: context.get(key) for key in
        ('trend', 'atr14_simple', 'volatility_ratio', 'volatility_regime')
    } | {
        'latest_confirmed_high': (pivots.get('high') or [{}])[-1].get('price'),
        'latest_confirmed_low': (pivots.get('low') or [{}])[-1].get('price'),
    }


def compact_decision(decision):
    """Keep decision evidence while excluding repeated chart/pivot arrays."""
    excluded = {'chart_data', 'structure'}
    result = {key: value for key, value in decision.items() if key not in excluded}
    result['structure'] = {
        frame: _timeframe_summary(context)
        for frame, context in (decision.get('structure') or {}).items()
    }
    return result


def compact_snapshot(snapshot):
    """Store identity, exposure and recent bar evidence instead of full feeds."""
    result = {key: value for key, value in snapshot.items()
              if key not in ('bars', 'recent_deals')}
    result['bar_evidence'] = {
        frame: bars[-8:] for frame, bars in (snapshot.get('bars') or {}).items()
    }
    deals = snapshot.get('recent_deals')
    result['recent_deal_count'] = len(deals) if isinstance(deals, list) else None
    return result


def compact_record(record):
    return {
        'rule_version': record.get('rule_version'),
        'configuration': record.get('configuration'),
        'snapshot': compact_snapshot(record.get('snapshot') or {}),
        'decision': compact_decision(record.get('decision') or {}),
        'outcome': record.get('outcome'),
        'audit_reason': record.get('audit_reason'),
    }


def _archive_name():
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    return f'decisions-{stamp}.jsonl'


def _rotate_locked(active, archives):
    if not active.exists() or active.stat().st_size < MAX_ACTIVE_BYTES:
        return None
    archives.mkdir(parents=True, exist_ok=True)
    target = archives / _archive_name()
    active.replace(target)
    _status['last_rotation_at'] = time.time()
    return target


def append_record(root, record):
    global _root
    active, archives = _paths(root)
    Path(root).mkdir(parents=True, exist_ok=True)
    payload = json.dumps(compact_record(record), allow_nan=False, separators=(',', ':')) + '\n'
    with _lock:
        _root = Path(root)
        rotated = _rotate_locked(active, archives)
        with active.open('a', encoding='utf-8') as log:
            log.write(payload)
        _status['active_bytes'] = active.stat().st_size
    start_archive_worker(root)
    if rotated:
        _wake.set()


def _compress(path):
    target = path.with_suffix(path.suffix + '.gz')
    temporary = target.with_suffix(target.suffix + '.part')
    with path.open('rb') as source, gzip.open(temporary, 'wb', compresslevel=6) as output:
        shutil.copyfileobj(source, output, length=1024 * 1024)
    temporary.replace(target)
    path.unlink()
    return target


def _remote_target(path):
    remote = os.environ.get('TRADEW_AUDIT_REMOTE', '').rstrip('/')
    if not remote:
        return None
    # Archive names begin with a UTC date.
    year, month = path.name[10:14], path.name[14:16]
    return f'{remote}/{year}/{month}/{path.name}'


def _upload(path):
    target = _remote_target(path)
    if not target:
        return False
    command = os.environ.get('TRADEW_RCLONE_COMMAND', 'rclone')
    completed = subprocess.run(
        [command, 'copyto', str(path), target, '--retries', '3',
         '--low-level-retries', '10', '--checkers', '2', '--transfers', '1'],
        text=True, capture_output=True, timeout=1800, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or 'rclone upload failed')
    verified = subprocess.run(
        [command, 'size', target, '--json'], text=True, capture_output=True,
        timeout=120, check=False)
    if verified.returncode != 0:
        raise RuntimeError(verified.stderr or 'Unable to verify Drive archive')
    remote_size = json.loads(verified.stdout).get('bytes')
    if remote_size != path.stat().st_size:
        raise RuntimeError(f'Drive archive size mismatch: local={path.stat().st_size}, remote={remote_size}')
    path.unlink()
    with _lock:
        _status['uploaded_count'] += 1
        _status['last_upload_at'] = time.time()
    return True


def _pending(archives):
    return sorted(list(archives.glob('*.jsonl')) + list(archives.glob('*.jsonl.gz')))


def _process_archives():
    if _root is None:
        return
    _, archives = _paths(_root)
    archives.mkdir(parents=True, exist_ok=True)
    # Clean only abandoned partial compression outputs; the source is retained.
    for partial in archives.glob('*.gz.part'):
        partial.unlink()
    for path in _pending(archives):
        if path.suffix == '.jsonl':
            path = _compress(path)
        if _remote_target(path):
            _upload(path)
    with _lock:
        _status['pending_archives'] = len(_pending(archives))
        _status['drive_configured'] = bool(os.environ.get('TRADEW_AUDIT_REMOTE'))
        _status['last_error'] = None


def _worker():
    while True:
        try:
            _process_archives()
        except Exception as exc:
            with _lock:
                _status['last_error'] = str(exc)[-500:]
        _wake.wait(UPLOAD_INTERVAL)
        _wake.clear()


def start_archive_worker(root):
    global _started, _root
    with _lock:
        _root = Path(root)
        if _started:
            return
        _started = True
    threading.Thread(target=_worker, name='tradew-audit-archive', daemon=True).start()


def archive_status(root=None):
    root = Path(root or _root) if (root or _root) else None
    with _lock:
        status = dict(_status)
    if root:
        active, archives = _paths(root)
        status['active_bytes'] = active.stat().st_size if active.exists() else 0
        status['pending_archives'] = len(_pending(archives)) if archives.exists() else 0
    status['rotate_bytes'] = MAX_ACTIVE_BYTES
    status['remote'] = os.environ.get('TRADEW_AUDIT_REMOTE') or None
    return status
