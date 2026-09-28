"""Private Web Push alerts for new deterministic BUY/SELL setups."""
import base64
import hashlib
import json
import os
import queue
import threading
import time
from pathlib import Path
from urllib.parse import urlparse


_lock = threading.Lock()
_queue = queue.Queue(maxsize=20)
_queued = set()
_attempted_at = {}
_worker_started = False
_data_root = None
_status = {
    'enabled': False, 'subscription_count': 0, 'queued': 0,
    'sent_count': 0, 'last_sent_at': None, 'last_error': None,
}


def _paths(root):
    root = Path(root)
    return {
        'private': root / 'vapid_private.pem',
        'subscriptions': root / 'push_subscriptions.json',
        'sent': root / 'push_alert_state.json',
        'events': root / 'notification_events.jsonl',
    }


def _atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def ensure_vapid(root):
    """Create a persistent server identity and return its browser public key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    paths = _paths(root)
    Path(root).mkdir(parents=True, exist_ok=True)
    with _lock:
        if paths['private'].exists():
            key = serialization.load_pem_private_key(
                paths['private'].read_bytes(), password=None)
        else:
            key = ec.generate_private_key(ec.SECP256R1())
            paths['private'].write_bytes(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()))
            paths['private'].chmod(0o600)
    raw = key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def _read_list(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, list) else []
    except (OSError, ValueError):
        return []


def _valid_subscription(subscription):
    if not isinstance(subscription, dict):
        return False
    endpoint = subscription.get('endpoint')
    keys = subscription.get('keys')
    if not isinstance(endpoint, str) or len(endpoint) > 4096:
        return False
    parsed = urlparse(endpoint)
    if parsed.scheme != 'https' or not parsed.netloc:
        return False
    return (isinstance(keys, dict) and
            all(isinstance(keys.get(name), str) and 10 <= len(keys[name]) <= 1024
                for name in ('p256dh', 'auth')))


def save_subscription(root, subscription):
    if not _valid_subscription(subscription):
        raise ValueError('Invalid Web Push subscription')
    path = _paths(root)['subscriptions']
    normalized = {key: subscription.get(key) for key in ('endpoint', 'expirationTime', 'keys')}
    with _lock:
        subscriptions = _read_list(path)
        subscriptions = [item for item in subscriptions
                         if item.get('endpoint') != normalized['endpoint']]
        subscriptions.append(normalized)
        _atomic_json(path, subscriptions)
        _status['subscription_count'] = len(subscriptions)
    return len(subscriptions)


def remove_subscription(root, endpoint):
    if not isinstance(endpoint, str):
        raise ValueError('endpoint is required')
    path = _paths(root)['subscriptions']
    with _lock:
        subscriptions = [item for item in _read_list(path)
                         if item.get('endpoint') != endpoint]
        _atomic_json(path, subscriptions)
        _status['subscription_count'] = len(subscriptions)
    return len(subscriptions)


def signal_key(decision):
    if decision.get('action') not in ('BUY', 'SELL') or decision.get('data_quality'):
        return None
    identity = decision.get('setup_id') or decision.get('setup_ref')
    if not identity:
        identity = ':'.join(str(decision.get(name)) for name in
                            ('symbol', 'action', 'entry', 'stop'))
    return f"{decision.get('action')}:{identity}"


def _sent_keys(root):
    try:
        value = json.loads(_paths(root)['sent'].read_text(encoding='utf-8'))
        return value if isinstance(value, list) else []
    except (OSError, ValueError):
        return []


def consider_signal_alert(decision):
    """Queue a setup once; delivery is kept off the deterministic scan thread."""
    key = signal_key(decision)
    if not key or _data_root is None:
        return False
    now = time.time()
    with _lock:
        if (key in _queued or key in _sent_keys(_data_root) or
                now - _attempted_at.get(key, 0) < 60):
            return False
        if not _read_list(_paths(_data_root)['subscriptions']):
            return False
        try:
            _queue.put_nowait((key, json.loads(json.dumps(decision, allow_nan=False))))
        except queue.Full:
            _status['last_error'] = 'Notification queue is full'
            return False
        _queued.add(key)
        _status['queued'] = _queue.qsize()
    return True


def _message(decision, key):
    action = decision['action']
    score = decision.get('buy_setup_score') if action == 'BUY' else decision.get('sell_setup_score')
    parts = [f"Score {score}" if score is not None else None,
             f"Entry {decision.get('entry')}" if decision.get('entry') is not None else None,
             'Advisory only — no order was placed']
    return {
        'title': f"{decision.get('symbol') or 'Gold'} {action} setup",
        'body': ' · '.join(part for part in parts if part),
        'tag': 'tradew-' + hashlib.sha256(key.encode()).hexdigest()[:16],
        'url': '/', 'action': action,
    }


def _record_event(root, event):
    with _paths(root)['events'].open('a', encoding='utf-8') as log:
        log.write(json.dumps({'time': time.time(), **event}, allow_nan=False) + '\n')


def _deliver(key, decision):
    from pywebpush import WebPushException, webpush

    paths = _paths(_data_root)
    subscriptions = _read_list(paths['subscriptions'])
    payload = json.dumps(_message(decision, key), allow_nan=False)
    survivors = []
    sent = 0
    failures = []
    for subscription in subscriptions:
        try:
            webpush(subscription_info=subscription, data=payload,
                    vapid_private_key=str(paths['private']),
                    vapid_claims={
                        'sub': os.environ.get(
                            'TRADEW_VAPID_SUBJECT', 'mailto:admin@example.com')},
                    ttl=180)
            survivors.append(subscription)
            sent += 1
        except WebPushException as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            if status not in (404, 410):
                survivors.append(subscription)
                failures.append(str(exc))
        except Exception as exc:
            survivors.append(subscription)
            failures.append(str(exc))
    with _lock:
        if survivors != subscriptions:
            _atomic_json(paths['subscriptions'], survivors)
        _status['subscription_count'] = len(survivors)
        if sent:
            sent_keys = _sent_keys(_data_root)
            if key not in sent_keys:
                sent_keys.append(key)
            _atomic_json(paths['sent'], sent_keys[-500:])
            _status['sent_count'] += sent
            _status['last_sent_at'] = time.time()
        _status['last_error'] = '; '.join(failures)[-500:] if failures else None
    _record_event(_data_root, {'event': 'PUSH_SENT' if sent else 'PUSH_FAILED',
                              'signal_key': key, 'recipients': sent,
                              'failures': len(failures)})


def _worker():
    while True:
        key, decision = _queue.get()
        try:
            with _lock:
                _attempted_at[key] = time.time()
            _deliver(key, decision)
        except Exception as exc:
            with _lock:
                _status['last_error'] = str(exc)[-500:]
        finally:
            with _lock:
                _queued.discard(key)
                _status['queued'] = _queue.qsize()
            _queue.task_done()


def start_notification_worker(root):
    global _worker_started, _data_root
    ensure_vapid(root)
    with _lock:
        _data_root = Path(root)
        _status['enabled'] = True
        _status['subscription_count'] = len(_read_list(_paths(root)['subscriptions']))
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(target=_worker, name='tradew-push-alerts', daemon=True).start()


def notification_status():
    with _lock:
        return dict(_status)
