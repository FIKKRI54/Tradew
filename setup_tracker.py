"""Persistent setup/thesis registry and broker-history outcome reconciliation."""
import hashlib
import json
import re
import sqlite3
import threading
from functools import wraps
from pathlib import Path


_TRACKER_LOCK = threading.RLock()


def _serialized(method):
    """Serialize a complete tracker read/modify/write cycle."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with _TRACKER_LOCK:
            self._connection = self._connect()
            self._connection.execute('BEGIN IMMEDIATE')
            self._in_transaction = True
            try:
                self.state = self._load_database()
                result = method(self, *args, **kwargs)
                self._write_database()
                self._connection.commit()
                self._mirror_json()
                return result
            except Exception:
                self._connection.rollback()
                raise
            finally:
                self._in_transaction = False
                self._connection.close()
                self._connection = None
    return wrapped


class SetupTracker:
    TERMINAL_STATES = {'INVALIDATED', 'EXPIRED', 'CLOSED'}
    def __init__(self, root):
        self.root = Path(root)
        self.state_path = self.root / 'setup_state.json'
        self.events_path = self.root / 'setup_events.jsonl'
        self.database_path = self.root / 'setup_state.sqlite3'
        self.root.mkdir(parents=True, exist_ok=True)
        self._in_transaction = False
        with _TRACKER_LOCK:
            connection = self._connect()
            try:
                connection.execute(
                    'CREATE TABLE IF NOT EXISTS tracker_state '
                    '(id INTEGER PRIMARY KEY CHECK (id=1), payload TEXT NOT NULL)')
                existing = connection.execute(
                    'SELECT payload FROM tracker_state WHERE id=1').fetchone()
                if existing is None:
                    initial = self._load_json()
                    connection.execute(
                        'INSERT INTO tracker_state(id,payload) VALUES(1,?)',
                        (json.dumps(initial, sort_keys=True),))
                    connection.commit()
                row = connection.execute(
                    'SELECT payload FROM tracker_state WHERE id=1').fetchone()
                self.state = json.loads(row[0])
            finally:
                connection.close()
        self._connection = None

    def _connect(self):
        connection = sqlite3.connect(
            self.database_path, timeout=30, check_same_thread=False)
        connection.execute('PRAGMA journal_mode=WAL')
        return connection

    def _load_json(self):
        try:
            data = json.loads(self.state_path.read_text(encoding='utf-8'))
            if isinstance(data.get('setups'), dict) and isinstance(data.get('bindings'), dict):
                return data
        except (OSError, ValueError, TypeError):
            pass
        return {'setups': {}, 'bindings': {}}

    def _load_database(self):
        row = self._connection.execute(
            'SELECT payload FROM tracker_state WHERE id=1').fetchone()
        try:
            data = json.loads(row[0]) if row else None
            if isinstance(data.get('setups'), dict) and isinstance(data.get('bindings'), dict):
                return data
        except (ValueError, TypeError, AttributeError):
            pass
        return {'setups': {}, 'bindings': {}}

    def _write_database(self):
        self._connection.execute(
            'INSERT INTO tracker_state(id,payload) VALUES(1,?) '
            'ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
            (json.dumps(self.state, sort_keys=True),))

    def _mirror_json(self):
        temporary = self.state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.state, indent=2, sort_keys=True), encoding='utf-8')
        temporary.replace(self.state_path)

    def _save(self):
        if self._connection is None:
            self._connection = self._connect()
            try:
                self._connection.execute('BEGIN IMMEDIATE')
                self._write_database()
                self._connection.commit()
                self._mirror_json()
            except Exception:
                self._connection.rollback()
                raise
            finally:
                self._connection.close()
                self._connection = None
            return
        self._write_database()
        if not self._in_transaction:
            self._connection.commit()
            self._mirror_json()

    def _event(self, event):
        with self.events_path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(event, allow_nan=False) + '\n')

    @staticmethod
    def reference(setup_id):
        return hashlib.sha256(setup_id.encode('utf-8')).hexdigest()[:10]

    @staticmethod
    def account_error(setup, snapshot):
        checks = (
            ('account login', setup.get('account_login'), snapshot.get('account', {}).get('login')),
            ('account server', setup.get('account_server'), snapshot.get('account', {}).get('server')),
            ('account mode', setup.get('account_mode'), snapshot.get('account_mode')),
            ('account currency', setup.get('account_currency'), snapshot.get('account', {}).get('currency')),
        )
        for label, expected, actual in checks:
            if expected is None:
                return 'Setup binding rejected: missing recorded ' + label
            if expected != actual:
                return f'Setup binding rejected: {label} mismatch ({expected} != {actual})'
        return None

    @staticmethod
    def binding_error(setup, position, snapshot):
        direction = 'BUY' if position.get('type') == 0 else 'SELL' if position.get('type') == 1 else None
        for label, expected, actual in (
                ('symbol', setup.get('symbol'), position.get('symbol')),
                ('direction', setup.get('direction'), direction)):
            if expected is None:
                return 'Setup binding rejected: missing recorded ' + label
            if expected != actual:
                return f'Setup binding rejected: {label} mismatch ({expected} != {actual})'
        return SetupTracker.account_error(setup, snapshot)

    @staticmethod
    def binding_key(setup, ticket):
        fields = (setup.get('account_login'), setup.get('account_server'),
                  setup.get('account_mode'))
        if any(value is None for value in fields):
            raise ValueError('Setup has no complete recorded account identity')
        return '|'.join(map(str, (*fields, int(ticket))))

    @staticmethod
    def snapshot_binding_key(snapshot, ticket):
        account = snapshot.get('account', {})
        return '|'.join(map(str, (account.get('login'), account.get('server'),
                                  snapshot.get('account_mode'), int(ticket))))

    @_serialized
    def bind(self, ticket, setup_ref, timestamp=None):
        if setup_ref not in self.state['setups']:
            raise ValueError('Unknown setup reference')
        if self.state['setups'][setup_ref].get('state') not in (
                'CONFIRMED', 'ARMED_LIMIT', 'ACTIONABLE'):
            raise ValueError('Setup is not in a bindable state')
        key = self.binding_key(self.state['setups'][setup_ref], ticket)
        self.state['bindings'][key] = setup_ref
        self._event({'event': 'BINDING_REQUESTED', 'time': timestamp,
                     'setup_ref': setup_ref, 'ticket': int(ticket)})
        self._save()

    @_serialized
    def attach(self, snapshot):
        """Attach only explicit ticket bindings or S:<ref> broker comments."""
        changed = False
        binding_errors = []
        deals = snapshot.get('recent_deals') or []
        # Recover filled positions from immutable broker comments even if the
        # scanner restarted before it observed the live position.
        for deal in deals:
            match = re.search(r'(?:^|\s)S:([0-9a-f]{10})(?:\s|$)',
                              str(deal.get('comment', '') or ''))
            position_id = int(deal.get('position_id') or 0)
            if not match or not position_id or match.group(1) not in self.state['setups']:
                continue
            thesis = self.state['setups'][match.group(1)]
            if self.account_error(thesis, snapshot):
                continue
            if position_id not in thesis.setdefault('linked_tickets', []):
                thesis['linked_tickets'].append(position_id)
                changed = True
        pending_refs = set()
        for order in snapshot.get('pending_orders') or []:
            match = re.search(r'(?:^|\s)S:([0-9a-f]{10})(?:\s|$)',
                              str(order.get('comment', '') or ''))
            if match and match.group(1) in self.state['setups']:
                pending_refs.add(match.group(1))
        for position in snapshot.get('positions') or []:
            ticket = str(position['ticket'])
            scoped_key = self.snapshot_binding_key(snapshot, position['ticket'])
            ref = self.state['bindings'].get(scoped_key)
            legacy_ref = self.state['bindings'].get(ticket)
            if ref is None:
                ref = legacy_ref
            match = re.search(r'(?:^|\s)S:([0-9a-f]{10})(?:\s|$)', position.get('comment', ''))
            if ref is None and match and match.group(1) in self.state['setups']:
                ref = match.group(1)
            thesis = self.state['setups'].get(ref)
            if thesis:
                error = self.binding_error(thesis, position, snapshot)
                if error:
                    position['setup_binding_error'] = error
                    binding_errors.append({'ticket': position['ticket'], 'setup_ref': ref,
                                           'error': error})
                    continue
                if self.state['bindings'].get(scoped_key) != ref:
                    self.state['bindings'][scoped_key] = ref
                    changed = True
                if legacy_ref == ref:
                    del self.state['bindings'][ticket]
                    changed = True
                # Native pending entries are recorded while still ARMED, before
                # the broker knows the eventual fill. Bind the actual fill so
                # break-even and outcome management retain the true entry.
                if thesis.get('original_entry') is None:
                    thesis['original_entry'] = position.get('price_open')
                    changed = True
                if not thesis.get('original_targets') and position.get('tp', 0) > 0:
                    thesis['original_targets'] = [position['tp']]
                    changed = True
                position['original_setup'] = dict(thesis)
                if int(ticket) not in thesis.setdefault('linked_tickets', []):
                    thesis['linked_tickets'].append(int(ticket))
                    self._event({'event': 'POSITION_BOUND', 'time': snapshot.get('retrieval_end_utc'),
                                 'setup_ref': ref, 'ticket': int(ticket)})
                    changed = True
        open_tickets = {p['ticket'] for p in snapshot.get('positions') or []}
        for ref, setup in self.state['setups'].items():
            if self.account_error(setup, snapshot):
                continue
            linked = set(setup.get('linked_tickets', []))
            related = [d for d in deals if d.get('position_id') in linked]
            exits = [d for d in related if d.get('entry_role') in ('OUT', 'OUT_BY')]
            if (linked and not linked.intersection(open_tickets) and
                    ref not in pending_refs and exits):
                outcome = {
                    'currency': snapshot.get('account', {}).get('currency'),
                    'net_result': sum(d.get('profit', 0) + d.get('commission', 0) +
                                      d.get('swap', 0) + d.get('fee', 0) for d in related),
                    'exit_deals': [d.get('ticket') for d in exits],
                    'closed_time': max(d.get('time', 0) for d in exits),
                }
                previous = setup.get('outcome')
                if setup.get('state') != 'CLOSED' or previous != outcome:
                    event = ('SETUP_CLOSED' if setup.get('state') != 'CLOSED'
                             else 'SETUP_OUTCOME_REVISED')
                    setup['state'] = 'CLOSED'
                    setup['outcome'] = outcome
                    self._event({'event': event, 'setup_ref': ref, **outcome})
                    changed = True
        if changed:
            self._save()
        snapshot['setup_tracking'] = {
            'known_setups': len(self.state['setups']),
            'bound_positions': sum('original_setup' in p for p in snapshot.get('positions') or []),
            'binding_errors': binding_errors,
            'outcome_history': 'available' if snapshot.get('recent_deals') is not None else 'unavailable',
        }

    @_serialized
    def record(self, result, snapshot):
        setup_id = result.get('setup_id')
        if not setup_id:
            return
        ref = self.reference(setup_id)
        result['setup_ref'] = ref
        existing = self.state['setups'].get(ref)
        prior = existing or {}
        account = snapshot.get('account', {})
        observed_direction = (result.get('action') if result.get('action') in ('BUY', 'SELL') else
                              result.get('planned_direction') if result.get('planned_direction') in ('BUY', 'SELL') else
                              next((s['direction'] for s in result.get('observed_setups', [])
                                    if str(s.get('confirmation_time')) in setup_id), None))
        state = (existing.get('state')
                 if existing and existing.get('state') in self.TERMINAL_STATES
                 else result.get('setup_state'))
        details = {
            'setup_id': setup_id, 'setup_ref': ref, 'symbol': result.get('symbol'),
            'account_login': (prior.get('account_login') if prior.get('account_login') is not None
                              else account.get('login')),
            'account_server': (prior.get('account_server') if prior.get('account_server') is not None
                               else account.get('server')),
            'account_mode': (prior.get('account_mode') if prior.get('account_mode') is not None
                             else snapshot.get('account_mode')),
            'account_currency': (prior.get('account_currency')
                                 if prior.get('account_currency') is not None
                                 else account.get('currency')),
            'family': prior.get('family') or (setup_id.split(':')[1] if ':' in setup_id else None),
            'direction': prior.get('direction') or observed_direction,
            'original_entry': (prior.get('original_entry') if prior.get('original_entry') is not None
                               else result.get('entry')),
            'original_invalidation': (prior.get('original_invalidation')
                                      if prior.get('original_invalidation') is not None
                                      else result.get('invalidation')),
            'original_targets': (prior.get('original_targets') or result.get('targets') or
                                 (result.get('entry_plan') or {}).get('targets', [])),
            'created_time': prior.get('created_time', result.get('snapshot_time')),
            'state': state,
            # A one-second scanner must not rewrite the tracker every tick. Keep
            # the prior timestamp for a minute unless the setup state changes.
            'last_seen_time': (
                result.get('snapshot_time')
                if (not existing or existing.get('state') != state or
                    (result.get('snapshot_time') or 0) - (existing.get('last_seen_time') or 0) >= 60)
                else existing.get('last_seen_time')),
            'session': prior.get('session') or result.get('session'),
            'volatility_regime': (prior.get('volatility_regime') or
                                  result.get('structure', {}).get('M5', {}).get('volatility_regime')),
            'linked_tickets': existing.get('linked_tickets', []) if existing else [],
        }
        updated = {**(existing or {}), **details}
        self.state['setups'][ref] = updated
        if existing is None:
            self._event({'event': 'SETUP_RECORDED', **details})
        elif existing.get('state') != details['state']:
            self._event({'event': 'SETUP_STATE_CHANGED', 'setup_ref': ref,
                         'from': existing.get('state'), 'to': details['state'],
                         'time': result.get('snapshot_time')})
        if existing != updated:
            self._save()

    @_serialized
    def enforce_terminal_state(self, result):
        """A setup ID cannot become tradable again after expiry or invalidation."""
        setup_id = result.get('setup_id')
        if not setup_id:
            return result
        existing = self.state['setups'].get(self.reference(setup_id))
        state = (existing or {}).get('state')
        if state not in self.TERMINAL_STATES:
            return result
        result['action'] = 'WAIT'
        result['setup_state'] = state
        result['reasons'] = [f'Original setup is terminal: {state.lower()}']
        result['next_trigger'] = 'Wait for a newly confirmed setup with a new setup ID'
        for field in ('entry', 'volume', 'cash_risk', 'reward_risk'):
            result.pop(field, None)
        return result
