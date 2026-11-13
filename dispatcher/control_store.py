"""Transactional local controls and durable run/command history."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import unicodedata
import uuid
from collections.abc import Mapping
from pathlib import Path, PureWindowsPath
from .task_inputs import TaskInputStoreMixin


class StateConflict(ValueError):
    """The requested action no longer matches the displayed task version."""


def _reject_path_links(path):
    """Reject links before resolve can erase evidence of a junction component."""
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Artifact paths cannot use links or reparse points')


def _artifact_directory(value):
    if not isinstance(value, (str, Path)) or not str(value).strip() or '\x00' in str(value):
        raise ValueError('Artifact root requires an absolute directory path')
    path = Path(value)
    windows = PureWindowsPath(str(value))
    if (not path.is_absolute() or str(value).startswith(('\\\\', '//')) or
            windows.drive.startswith('\\\\') or ':' in str(value)[len(windows.drive):]):
        raise ValueError('Artifact root requires a local absolute directory path')
    _reject_path_links(path)
    path = path.resolve()
    _reject_path_links(path)
    if not path.is_dir():
        raise ValueError('Artifact root directory does not exist')
    return path


def _normalized_manifest_hashes(hashes):
    if not isinstance(hashes, Mapping) or not hashes:
        return None
    normalized = {}
    for name, digest in hashes.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(digest, str):
            return None
        key = name.replace('\\', '/')
        value = digest.strip().lower()
        if key in normalized or not re.fullmatch(r'[0-9a-f]{64}', value):
            return None
        normalized[key] = value
    return normalized


def manifest_digest(result):
    """Bind artifact hashes and, for recovered runs, the host recovery evidence."""
    if isinstance(result, Mapping) and ('result_receipt' in result or
            (isinstance(result.get('execution'), Mapping) and result['execution'].get('protocol_version') == 2)):
        from .result_receipt import answer_digest
        return answer_digest(result)
    normalized = _normalized_manifest_hashes(result.get('sha256') if isinstance(result, Mapping) else None)
    if normalized is None:
        return None
    execution = result.get('execution')
    warning = result.get('runtime_warning')
    recovered = ((isinstance(execution, Mapping) and execution.get('status') == 'recovered') or
                 (isinstance(warning, Mapping) and warning.get('kind') == 'completed_eof'))
    payload = normalized
    if recovered:
        if (not isinstance(execution, Mapping) or execution.get('status') != 'recovered' or
                execution.get('executor') != 'antigravity' or execution.get('vendor_status') != 'ERROR' or
                not isinstance(warning, Mapping) or warning.get('kind') != 'completed_eof'):
            return None
        execution_warning = execution.get('runtime_warning')
        evidence = execution.get('completion_evidence')
        recovered_hashes = _normalized_manifest_hashes(execution.get('recovered_artifact_sha256'))
        if (not isinstance(execution_warning, Mapping) or execution_warning != warning or
                not isinstance(evidence, Mapping) or not evidence or recovered_hashes != normalized):
            return None
        payload = {
            'recovery_manifest_version': 1,
            'sha256': normalized,
            'runtime_warning': dict(warning),
            'execution': {
                'status': execution['status'],
                'executor': execution['executor'],
                'vendor_status': execution['vendor_status'],
                'runtime_warning': dict(execution_warning),
                'completion_evidence': dict(evidence),
                'recovered_artifact_sha256': recovered_hashes,
            },
        }
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        return hashlib.sha256(encoded.encode('utf-8')).hexdigest()
    except (TypeError, ValueError, RecursionError, UnicodeError):
        return None


_digest_manifest = manifest_digest


TASK_COLUMNS = """t.*, c.manual_executor, COALESCE(c.delegated, 0) AS delegated,
    COALESCE(c.paused, 0) AS paused, c.feedback, c.repo_alias,
    COALESCE(c.execution_overrides, '{}') AS execution_overrides,
    COALESCE(c.workspace_choice, '{"mode":"default"}') AS workspace_choice,
    COALESCE(c.delivery_mode, 'auto') AS delivery_mode,
    COALESCE(c.permission_profile, 'default') AS permission_profile,
    COALESCE(c.input_version, 1) AS input_version,
    (SELECT context FROM task_inputs WHERE task_id=t.id AND version=COALESCE(c.input_version,1)) AS input_context"""
BLOCKED_CONTROLS = frozenset({'running', 'verifying', 'awaiting_review', 'approved', 'writeback_pending', 'done'})
BLOCKED_ARCHIVE = frozenset({'running', 'verifying', 'awaiting_review', 'approved', 'writeback_pending'})
_UNSET = object()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _settings_object(settings):
    """Storage shape check; vendor capabilities are validated at the boundary."""
    if not isinstance(settings, dict) or set(settings) - {'model', 'effort'}:
        raise ValueError('Settings must contain only model and effort')
    if any(not isinstance(value, str) or not value.strip() for value in settings.values()):
        raise ValueError('Model and effort settings must be nonempty strings')
    return dict(settings)


def _settings_executor(executor):
    from .routing import EXECUTORS
    if not isinstance(executor, str) or executor not in EXECUTORS:
        raise ValueError('Unsupported executor')


def validate_workspace_choice(choice):
    """Accept an opaque selection or a single safe name, never a filesystem path."""
    if not isinstance(choice, dict):
        raise ValueError('Workspace choice must be an object')
    mode = choice.get('mode')
    if mode == 'default' and set(choice) == {'mode'}:
        return dict(choice)
    if mode == 'existing' and set(choice) == {'mode', 'workspace_id'}:
        identifier = choice['workspace_id']
        if isinstance(identifier, str) and re.fullmatch(r'ws_[0-9a-f]{24}', identifier):
            return dict(choice)
        raise ValueError('Existing workspace requires a valid workspace ID')
    if mode == 'new' and set(choice) == {'mode', 'name'}:
        from .workspace_layout import WINDOWS_DEVICE_NAMES
        name = choice['name']
        if not isinstance(name, str) or not 1 <= len(name) <= 64:
            raise ValueError('New workspace name must contain 1 to 64 characters')
        normalized = unicodedata.normalize('NFKC', name)
        for value in (name, normalized):
            if (not value or value != value.strip() or value in {'.', '..'} or value.endswith('.') or
                    any(char in '<>:"/\\|?*' or unicodedata.category(char) in {'Cc', 'Cf', 'Cs'}
                        for char in value) or
                    value.split('.', 1)[0].rstrip().upper() in WINDOWS_DEVICE_NAMES):
                raise ValueError('New workspace name must be a safe single directory name')
        return dict(choice)
    raise ValueError('Workspace choice has an unsupported mode or fields')


def _execution_snapshot(result):
    """Read a host-recorded snapshot without accepting conflicting copies."""
    if not isinstance(result, dict):
        return _UNSET
    candidates = [result['execution_settings']] if 'execution_settings' in result else []
    execution = result.get('execution')
    if isinstance(execution, dict) and 'execution_settings' in execution:
        candidates.append(execution['execution_settings'])
    if not candidates:
        return _UNSET
    if any(not isinstance(value, dict) for value in candidates):
        raise ValueError('Execution settings snapshot must be an object')
    if any(value != candidates[0] for value in candidates[1:]):
        raise StateConflict('Conflicting execution settings snapshots')
    return _json(candidates[0])


def _workspace_snapshot(result):
    """Read the host binding consistently from execution and verification reports."""
    if not isinstance(result, dict):
        return _UNSET
    candidates = [result['workspace_binding']] if 'workspace_binding' in result else []
    execution = result.get('execution')
    if isinstance(execution, dict) and 'workspace_binding' in execution:
        candidates.append(execution['workspace_binding'])
    if not candidates:
        return _UNSET
    if any(not isinstance(value, dict) for value in candidates):
        raise ValueError('Workspace binding snapshot must be an object')
    encoded = [_json(value) for value in candidates]
    if any(value != encoded[0] for value in encoded[1:]):
        raise StateConflict('Conflicting workspace binding snapshots')
    return encoded[0]


class ControlStoreMixin(TaskInputStoreMixin):
    def _migrate_controls(self):
        from .core import _now, _task_status
        with self._transaction() as connection:
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(tasks)')}
            if 'revision' not in columns:
                connection.execute('ALTER TABLE tasks ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
            if 'current_run_id' not in columns:
                connection.execute('ALTER TABLE tasks ADD COLUMN current_run_id TEXT')
            if 'source_status' not in columns:
                connection.execute('ALTER TABLE tasks ADD COLUMN source_status INTEGER')
                for row in connection.execute('SELECT id, payload FROM tasks').fetchall():
                    connection.execute('UPDATE tasks SET source_status=? WHERE id=?',
                                       (_task_status(json.loads(row['payload'])), row['id']))
            if 'source_checked_at' not in columns:
                # A migrated snapshot is not a fresh observation of the source.
                connection.execute('ALTER TABLE tasks ADD COLUMN source_checked_at TEXT')
            for statement in (
                '''CREATE TABLE IF NOT EXISTS task_controls (
                    task_id TEXT PRIMARY KEY, manual_executor TEXT, delegated INTEGER NOT NULL DEFAULT 0,
                    paused INTEGER NOT NULL DEFAULT 0, feedback TEXT, repo_alias TEXT)''',
                '''CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
                    executor TEXT, run_dir TEXT, result TEXT, state TEXT NOT NULL, review_status TEXT,
                    review_note TEXT, reviewed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    archived_at TEXT)''',
                'CREATE INDEX IF NOT EXISTS runs_by_directory ON runs(run_dir) WHERE run_dir IS NOT NULL',
                'CREATE INDEX IF NOT EXISTS runs_by_task ON runs(task_id, created_at)',
                '''CREATE TABLE IF NOT EXISTS commands (
                    id TEXT PRIMARY KEY, action TEXT NOT NULL, payload TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL, result TEXT, error TEXT, http_status INTEGER NOT NULL DEFAULT 200,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''',
                'CREATE INDEX IF NOT EXISTS commands_by_status ON commands(status, created_at)',
                '''CREATE TABLE IF NOT EXISTS agent_settings (
                    executor TEXT PRIMARY KEY, settings TEXT NOT NULL, revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL)''',
            ):
                connection.execute(statement)
            control_columns = {row['name'] for row in connection.execute('PRAGMA table_info(task_controls)')}
            connection.execute('''CREATE TABLE IF NOT EXISTS task_inputs(
                task_id TEXT NOT NULL,version INTEGER NOT NULL,parent_run_id TEXT NOT NULL,
                context TEXT NOT NULL,created_at TEXT NOT NULL,command_id TEXT NOT NULL UNIQUE,
                PRIMARY KEY(task_id,version))''')
            connection.execute('''CREATE TABLE IF NOT EXISTS task_input_drafts(
                task_id TEXT NOT NULL,parent_run_id TEXT NOT NULL,revision INTEGER NOT NULL,
                message TEXT NOT NULL,answers TEXT NOT NULL,updated_at TEXT NOT NULL,
                PRIMARY KEY(task_id,parent_run_id))''')
            if 'input_version' not in control_columns:
                connection.execute('ALTER TABLE task_controls ADD COLUMN input_version INTEGER NOT NULL DEFAULT 1')
            if 'delivery_mode' not in control_columns:
                connection.execute("ALTER TABLE task_controls ADD COLUMN delivery_mode TEXT NOT NULL DEFAULT 'auto'")
            if 'permission_profile' not in control_columns:
                connection.execute("ALTER TABLE task_controls ADD COLUMN permission_profile TEXT NOT NULL DEFAULT 'default'")
            if 'execution_overrides' not in control_columns:
                connection.execute("ALTER TABLE task_controls ADD COLUMN execution_overrides TEXT NOT NULL DEFAULT '{}'")
            if 'workspace_choice' not in control_columns:
                connection.execute('''ALTER TABLE task_controls ADD COLUMN workspace_choice
                    TEXT NOT NULL DEFAULT '{"mode":"default"}' ''')
            run_columns = {row['name'] for row in connection.execute('PRAGMA table_info(runs)')}
            for name, definition in (('input_version', 'INTEGER NOT NULL DEFAULT 1'), ('parent_run_id', 'TEXT'),
                                     ('input_context', 'TEXT'), ('round_number', 'INTEGER NOT NULL DEFAULT 0')):
                if name not in run_columns: connection.execute(f'ALTER TABLE runs ADD COLUMN {name} {definition}')
            if 'round_number' not in run_columns:
                counts = {}
                for item in connection.execute('SELECT id,task_id FROM runs ORDER BY created_at,id').fetchall():
                    counts[item['task_id']] = counts.get(item['task_id'], 0)+1
                    connection.execute('UPDATE runs SET round_number=? WHERE id=?', (counts[item['task_id']], item['id']))
            if 'execution_settings' not in run_columns:
                connection.execute('ALTER TABLE runs ADD COLUMN execution_settings TEXT')
            if 'permissions' not in run_columns:
                connection.execute('ALTER TABLE runs ADD COLUMN permissions TEXT')
            if 'workspace_binding' not in run_columns:
                connection.execute('ALTER TABLE runs ADD COLUMN workspace_binding TEXT')
            if 'artifact_root' not in run_columns:
                connection.execute('ALTER TABLE runs ADD COLUMN artifact_root TEXT')
            rows = connection.execute('SELECT * FROM tasks WHERE run_dir IS NOT NULL AND current_run_id IS NULL').fetchall()
            for row in rows:
                existing = connection.execute('SELECT id FROM runs WHERE run_dir=? AND task_id=?', (row['run_dir'], row['id'])).fetchone()
                run_id = existing['id'] if existing else uuid.uuid4().hex
                if existing is None:
                    result = json.loads(row['result']) if row['result'] else {}
                    execution = result.get('execution') if isinstance(result.get('execution'), dict) else {}
                    connection.execute('''INSERT INTO runs (id, task_id, fingerprint, payload, executor, run_dir,
                        result, state, review_status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                        (run_id, row['id'], row['fingerprint'], row['payload'], execution.get('executor') or result.get('executor'), row['run_dir'],
                         row['result'], row['state'], 'legacy', row['started_at'] or row['first_seen'], _now()))
                connection.execute('UPDATE tasks SET current_run_id=? WHERE id=?', (run_id, row['id']))

    @staticmethod
    def _agent_settings_row(row, executor):
        if row is None:
            return {'executor': executor, 'settings': {}, 'revision': 0}
        return {'executor': row['executor'], 'settings': json.loads(row['settings']),
                'revision': row['revision']}

    def get_agent_settings(self, executor):
        _settings_executor(executor)
        with self._connection() as connection:
            row = connection.execute('SELECT * FROM agent_settings WHERE executor=?', (executor,)).fetchone()
            return self._agent_settings_row(row, executor)

    def set_agent_settings(self, executor, settings, expected_revision):
        from .core import _now
        _settings_executor(executor)
        settings = _settings_object(settings)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('Agent settings revision must be a nonnegative integer')
        with self._transaction() as connection:
            row = connection.execute('SELECT * FROM agent_settings WHERE executor=?', (executor,)).fetchone()
            revision = row['revision'] if row is not None else 0
            if revision != expected_revision:
                raise StateConflict('Agent settings changed; refresh before retrying')
            connection.execute('''INSERT INTO agent_settings (executor, settings, revision, updated_at)
                VALUES (?, ?, ?, ?) ON CONFLICT(executor) DO UPDATE SET settings=excluded.settings,
                revision=excluded.revision, updated_at=excluded.updated_at''',
                (executor, _json(settings), revision + 1, _now()))
            self._event(connection, None, f'Agent defaults updated: {executor}; revision {revision + 1}')
            return self._agent_settings_row(connection.execute(
                'SELECT * FROM agent_settings WHERE executor=?', (executor,)).fetchone(), executor)

    @staticmethod
    def _select_task(connection, task_id):
        return connection.execute(f'''SELECT {TASK_COLUMNS} FROM tasks t
            LEFT JOIN task_controls c ON c.task_id=t.id WHERE t.id=?''', (task_id,)).fetchone()

    def _required_task(self, connection, task_id, expected_revision=_UNSET):
        row = self._select_task(connection, task_id)
        if row is None:
            raise KeyError(task_id)
        if expected_revision is not _UNSET and (isinstance(expected_revision, bool) or
                                             not isinstance(expected_revision, int) or
                                             row['revision'] != expected_revision):
            raise StateConflict('Task revision changed; refresh before retrying')
        return row

    def assign(self, task_id, executor, expected_revision, fresh_task, config, repo_alias=None,
               execution_overrides=None, workspace_choice=None, delivery_mode='auto', permission_profile='default', full_access_ack=False):
        from .result_protocol import validate_mode
        from .permissions import validate, resolve
        validate(executor, permission_profile, full_access_ack)
        validate_mode(delivery_mode)
        if delivery_mode=='answer': resolve(permission_profile,executor,answer=True)
        from .core import _due_date, _now, classify, normalize_task, task_fingerprint, task_key
        if not isinstance(executor, str) or not executor.strip():
            raise ValueError('Executor is required')
        executor = executor.strip()
        if executor != 'auto':
            _settings_executor(executor)
        overrides = _settings_object({} if execution_overrides is None else execution_overrides)
        choice = validate_workspace_choice({'mode': 'default'} if workspace_choice is None else workspace_choice)
        if executor == 'auto' and overrides:
            raise ValueError('Session settings require an explicitly selected executor')
        payload = normalize_task(fresh_task)
        if task_key(payload) != task_id:
            raise ValueError('Fresh source identity does not match task')
        if payload['status'] != 0:
            raise StateConflict('Only unfinished active source tasks can be assigned')
        policy = classify(payload, config)
        if repo_alias is not None:
            repositories = config.get('repositories') or {}
            selected = repositories.get(repo_alias) if isinstance(repo_alias, str) else None
            if not isinstance(selected, (str, Path)) or not str(selected).strip():
                raise ValueError('Repository must be a configured alias')
            policy['repo'], policy['kind'] = str(selected), 'code'
        fingerprint = task_fingerprint(payload)
        with self._transaction() as connection:
            old = self._required_task(connection, task_id, expected_revision)
            if permission_profile=='full' and old['fingerprint']!=fingerprint:
                raise StateConflict('任务内容已变化，请重新查看并确认完全访问范围。')
            if old['state'] not in {'ignored', 'queued', 'failed', 'needs_input', 'needs_review', 'revision_requested', 'cancelled'}:
                raise StateConflict(f"Cannot assign task in state {old['state']}")
            timestamp = _now()
            connection.execute('''INSERT INTO task_controls (task_id, manual_executor, delegated, paused, repo_alias,
                execution_overrides, workspace_choice) VALUES (?, ?, 1, 0, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET manual_executor=excluded.manual_executor,
                delegated=1, paused=0, repo_alias=excluded.repo_alias, execution_overrides=excluded.execution_overrides,
                workspace_choice=excluded.workspace_choice''',
                (task_id, None if executor == 'auto' else executor, repo_alias, _json(overrides), _json(choice)))
            connection.execute('UPDATE task_controls SET delivery_mode=?,input_version=1 WHERE task_id=?', (delivery_mode, task_id))
            connection.execute('UPDATE task_controls SET permission_profile=? WHERE task_id=?', (permission_profile,task_id))
            connection.execute('''UPDATE tasks SET payload=?, fingerprint=?, difficulty=?, kind=?, repo=?, reason=?,
                state='queued', attempts=?, run_dir=NULL, result=NULL, error=NULL, started_at=NULL, current_run_id=NULL,
                priority=?, due_date=?, source_status=0, source_checked_at=?,
                updated_at=?, queued_at=?, revision=revision+1 WHERE id=?''',
                (_json(payload), fingerprint, policy['difficulty'], policy['kind'], policy['repo'],
                 '手动委派；' + policy['reason'], 0,
                 payload['priority'], _due_date(payload.get('dueDate')), timestamp, timestamp, timestamp, task_id))
            self._event(connection, task_id,
                        f'Manually assigned: {executor}; new assignment attempt budget started; run history and daily starts retained')
            return self._row(self._select_task(connection, task_id))

    def withdraw(self, task_id, expected_revision):
        from .core import _now
        with self._transaction() as connection:
            row = self._required_task(connection, task_id, expected_revision)
            if row['state'] in BLOCKED_CONTROLS or row['state'] == 'needs_review':
                raise StateConflict(f"Cannot withdraw task in state {row['state']}")
            connection.execute('''INSERT INTO task_controls (task_id, delegated, paused) VALUES (?, 0, 1)
                ON CONFLICT(task_id) DO UPDATE SET delegated=0, paused=1''', (task_id,))
            state = 'cancelled' if row['state'] == 'cancelled' and row['paused'] else 'ignored'
            connection.execute('UPDATE tasks SET state=?, revision=revision+1, updated_at=? WHERE id=?', (state, _now(), task_id))
            self._event(connection, task_id, 'Manual delegation withdrawn; automatic queueing paused')
            return self._row(self._select_task(connection, task_id))

    def decide_review(self, task_id, expected_revision, run_id, fingerprint, manifest_digest, approve, note=''):
        from .core import _now
        if not isinstance(approve, bool):
            raise ValueError('Review decision must be a boolean')
        if not isinstance(note, str) or (not approve and not note.strip()):
            raise ValueError('Rejection requires feedback')
        with self._transaction() as connection:
            row = self._required_task(connection, task_id, expected_revision)
            run = connection.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if not run or run['task_id'] != task_id or row['current_run_id'] != run_id or row['fingerprint'] != fingerprint or run['fingerprint'] != fingerprint:
                raise StateConflict('Review no longer matches the current run and source fingerprint')
            result = json.loads(row['result']) if row['result'] else None
            digest = _digest_manifest(result)
            run_result = json.loads(run['result']) if run['result'] else None
            if manifest_digest != digest or _digest_manifest(run_result) != digest:
                raise StateConflict('Artifact manifest changed; refresh review')
            if approve:
                receipt = (result or {}).get('result_receipt') or {}
                if receipt.get('version') == 3:
                    from .task_inputs import context_digest
                    context = json.loads(row['input_context']) if row['input_context'] else None
                    if (receipt.get('input_version') != row['input_version'] or run['input_version'] != row['input_version'] or
                            receipt.get('input_context_sha256') != context_digest(context) or run['input_context'] != row['input_context']):
                        raise StateConflict('Review does not match the confirmed input version')
                if row['state'] != 'awaiting_review' or not result or result.get('status') != 'complete' or digest is None:
                    raise StateConflict('Only complete, current awaiting-review runs can be approved')
                state, review_status = 'approved', 'approved'
            else:
                if row['state'] not in {'awaiting_review', 'needs_review'}:
                    raise StateConflict('Task is not awaiting review')
                state, review_status = 'revision_requested', 'rejected'
                connection.execute('''INSERT INTO task_controls (task_id, paused, feedback) VALUES (?, 1, ?)
                    ON CONFLICT(task_id) DO UPDATE SET paused=1, feedback=excluded.feedback''', (task_id, note.strip()))
            timestamp = _now()
            connection.execute('''UPDATE runs SET state=?, review_status=?, review_note=?, reviewed_at=?, updated_at=?
                WHERE id=?''', (state, review_status, note.strip(), timestamp, timestamp, run_id))
            connection.execute('UPDATE tasks SET state=?, revision=revision+1, updated_at=? WHERE id=?', (state, timestamp, task_id))
            self._event(connection, task_id, f'Human review: {review_status}')
            return self._row(self._select_task(connection, task_id))

    @staticmethod
    def _run_row(row):
        if row is None:
            return None
        decoded = dict(row)
        decoded['payload'] = json.loads(decoded['payload'])
        decoded['result'] = json.loads(decoded['result']) if decoded['result'] is not None else None
        decoded['execution_settings'] = json.loads(decoded['execution_settings']) if decoded['execution_settings'] is not None else None
        decoded['permissions'] = json.loads(decoded['permissions']) if decoded.get('permissions') else None
        decoded['workspace_binding'] = json.loads(decoded['workspace_binding']) if decoded['workspace_binding'] is not None else None
        decoded['input_context'] = json.loads(decoded.get('input_context')) if decoded.get('input_context') else None
        decoded['source_writeback_allowed'] = (decoded['input_context'] or {}).get('source_writeback_allowed', True)
        return decoded

    def _sync_run(self, connection, row, state, fields, timestamp):
        run_id = row['current_run_id']
        if not run_id:
            return
        run = connection.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if run is None:
            raise StateConflict('Current run is missing')
        updates = {'state': state, 'updated_at': timestamp}
        for key in ('run_dir', 'result'):
            if key in fields:
                updates[key] = fields[key]
        if 'result' in fields and fields['result']:
            result = json.loads(fields['result'])
            if isinstance(result, dict) and result.get('executor'):
                updates['executor'] = result['executor']
            policy = result.get('permissions') or (result.get('execution') or {}).get('permissions')
            if policy is not None:
                if run['permissions'] is not None and json.loads(run['permissions']) != policy:
                    raise StateConflict('Permissions are immutable for an existing run')
                updates['permissions'] = _json(policy)
            snapshot = _execution_snapshot(result)
            if snapshot is not _UNSET:
                if run['execution_settings'] is not None and json.loads(run['execution_settings']) != json.loads(snapshot):
                    raise StateConflict('Execution settings are immutable for an existing run')
                updates['execution_settings'] = snapshot
            binding = _workspace_snapshot(result)
            if binding is not _UNSET:
                if run['workspace_binding'] is not None and _json(json.loads(run['workspace_binding'])) != binding:
                    raise StateConflict('Workspace binding is immutable for an existing run')
                updates['workspace_binding'] = binding
        if run['review_status'] == 'approved' and 'result' in fields and state in {'approved', 'writeback_pending', 'done'}:
            old_result = json.loads(run['result']) if run['result'] else None
            new_result = json.loads(fields['result']) if fields['result'] else None
            if manifest_digest(old_result) != manifest_digest(new_result):
                raise StateConflict('Approved artifact manifest cannot change without another human review')
        if state == 'awaiting_review':
            updates.update(review_status='pending', review_note=None, reviewed_at=None)
        elif state == 'done' and run['review_status'] != 'approved':
            updates['review_status'] = 'legacy'
        elif state in {'needs_review', 'cancelled'} and run['review_status'] in {'pending', 'approved'}:
            updates['review_status'] = 'stale'
        connection.execute('UPDATE runs SET ' + ', '.join(f'{key}=?' for key in updates) + ' WHERE id=?', (*updates.values(), run_id))

    def get_run(self, run_id):
        with self._connection() as connection:
            return self._run_row(connection.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())

    def list_runs(self, task_id=None, include_archived=True):
        clauses, values = [], []
        if task_id is not None:
            clauses.append('task_id=?')
            values.append(task_id)
        if not include_archived:
            clauses.append('archived_at IS NULL')
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self._connection() as connection:
            return [self._run_row(row) for row in connection.execute('SELECT * FROM runs' + where + ' ORDER BY created_at DESC, id', values)]

    def freeze_artifact_root(self, run_id, artifact_root, expected_run_dir):
        """Bind a host-verified historical snapshot without changing review evidence."""
        expected = _artifact_directory(expected_run_dir)
        frozen = _artifact_directory(artifact_root)
        if (frozen == expected or not frozen.is_relative_to(expected) or
                frozen.is_relative_to(expected / 'workspace')):
            raise ValueError('Artifact snapshot must stay inside its run and outside the live workspace')
        with self._transaction() as connection:
            row = connection.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            if _artifact_directory(row['run_dir']) != expected:
                raise StateConflict('Run directory changed before artifact snapshot binding')
            current = connection.execute('SELECT state FROM tasks WHERE current_run_id=?', (run_id,)).fetchone()
            if row['state'] in {'running', 'verifying'} or current and current['state'] in {'running', 'verifying'}:
                raise StateConflict('Running or verifying runs cannot freeze historical artifacts')
            if row['artifact_root'] is not None:
                if _artifact_directory(row['artifact_root']) != frozen:
                    raise StateConflict('Artifact root is immutable once frozen')
                return self._run_row(row)
            connection.execute('UPDATE runs SET artifact_root=? WHERE id=?', (str(frozen), run_id))
            return self._run_row(connection.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())

    def archive_run(self, run_id, archived=True):
        from .core import _now
        with self._transaction() as connection:
            row = connection.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            current = connection.execute('SELECT state FROM tasks WHERE current_run_id=?', (run_id,)).fetchone()
            if current and current['state'] in BLOCKED_ARCHIVE:
                raise StateConflict('Active or awaiting-review runs cannot be archived')
            connection.execute('UPDATE runs SET archived_at=? WHERE id=?', (_now() if archived else None, run_id))
            return self._run_row(connection.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())

    def import_run(self, task_id, run_dir, payload, result, state, executor):
        from .core import _now, normalize_task, task_fingerprint, task_key, STATES
        normalized = normalize_task(payload)
        if task_key(normalized) != task_id or state not in STATES or not str(run_dir).strip():
            raise ValueError('Invalid imported run identity, state, or directory')
        with self._transaction() as connection:
            row = connection.execute('SELECT * FROM runs WHERE run_dir=?', (str(run_dir),)).fetchone()
            if row:
                if row['task_id'] != task_id:
                    raise StateConflict('Run directory already belongs to another task')
                return self._run_row(row)
            run_id, timestamp = uuid.uuid4().hex, _now()
            connection.execute('''INSERT INTO runs (id, task_id, fingerprint, payload, executor, run_dir, result,
                state, review_status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'legacy', ?, ?)''',
                (run_id, task_id, task_fingerprint(normalized), _json(normalized), executor, str(run_dir),
                 _json(result) if result is not None else None, state, timestamp, timestamp))
            return self._run_row(connection.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone())

    @staticmethod
    def _command_row(row):
        if row is None:
            return None
        result = dict(row)
        result['payload'] = json.loads(result['payload'])
        result['result'] = json.loads(result['result']) if result['result'] is not None else None
        return result

    def submit_command(self, action, payload, idempotency_key):
        from .core import _now
        if not isinstance(action, str) or not action.strip() or not isinstance(payload, Mapping):
            raise ValueError('Command action and object payload are required')
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError('Idempotency key is required')
        encoded = _json(payload)
        with self._transaction() as connection:
            row = connection.execute('SELECT * FROM commands WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if row:
                if row['action'] != action or row['payload'] != encoded:
                    raise StateConflict('Idempotency key already used for a different command')
                return self._command_row(row)
            identifier, timestamp = uuid.uuid4().hex, _now()
            connection.execute('''INSERT INTO commands (id, action, payload, idempotency_key, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)''', (identifier, action, encoded, idempotency_key, timestamp, timestamp))
            return self._command_row(connection.execute('SELECT * FROM commands WHERE id=?', (identifier,)).fetchone())

    def next_command(self):
        from .core import _now
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM commands WHERE status='pending' ORDER BY created_at, id LIMIT 1").fetchone()
            if row is None:
                return None
            connection.execute("UPDATE commands SET status='processing', updated_at=? WHERE id=?", (_now(), row['id']))
            return self._command_row(connection.execute('SELECT * FROM commands WHERE id=?', (row['id'],)).fetchone())

    def finish_command(self, command_id, status, result=None, error=None, http_status=200):
        from .core import _now
        status = {'completed': 'done', 'succeeded': 'done'}.get(status, status)
        if status not in {'done', 'failed'}:
            raise ValueError('Invalid terminal command status')
        with self._transaction() as connection:
            row = connection.execute('SELECT * FROM commands WHERE id=?', (command_id,)).fetchone()
            if row is None:
                raise KeyError(command_id)
            if row['status'] != 'processing':
                raise StateConflict('Command is not processing')
            connection.execute('UPDATE commands SET status=?, result=?, error=?, http_status=?, updated_at=? WHERE id=?',
                (status, _json(result) if result is not None else None, error, int(http_status), _now(), command_id))
            return self._command_row(connection.execute('SELECT * FROM commands WHERE id=?', (command_id,)).fetchone())

    def get_command(self, command_id):
        with self._connection() as connection:
            return self._command_row(connection.execute('SELECT * FROM commands WHERE id=?', (command_id,)).fetchone())

    def command_for_request(self, request_id):
        with self._connection() as connection:
            return self._command_row(connection.execute('SELECT * FROM commands WHERE idempotency_key=?', (request_id,)).fetchone())

    def recover_commands(self):
        from .core import _now
        with self._transaction() as connection:
            result = connection.execute("""UPDATE commands SET status='failed', error=?, http_status=409,
                updated_at=? WHERE status='processing'""",
                ('Command interrupted by process restart; not replayed automatically. Refresh state before issuing a new command.', _now()))
            return result.rowcount
