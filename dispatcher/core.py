"""Deterministic task classification and a transactionally claimed SQLite queue."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .control_store import ControlStoreMixin, StateConflict, TASK_COLUMNS, manifest_digest


SEMANTIC_FIELDS = (
    "title", "content", "desc", "tags", "items", "parentId", "childIds",
    "repeatFlag", "startDate", "dueDate", "priority", "status",
)
STATES = frozenset({
    "queued", "ignored", "running", "verifying", "needs_input", "needs_review",
    "writeback_pending", "done", "failed", "cancelled",
    "awaiting_review", "approved", "revision_requested",
})
ACTIVE_STATES = frozenset({"running", "verifying", "writeback_pending"})
PROTECTED_STATES = ACTIVE_STATES | {"awaiting_review", "approved", "needs_review"}


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (ValueError, TypeError, OverflowError):
        return default


def _task_status(task: Mapping[str, Any]) -> int | None:
    """Unknown or malformed status must not silently become active status 0."""
    value = task.get("status")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value)
    return None


def task_key(task: Mapping[str, Any]) -> str:
    """Identity is local to a project; reject incomplete upstream records."""
    if not isinstance(task, Mapping):
        raise ValueError("Task must be an object")
    project_id, identifier = task.get("projectId"), task.get("id")
    if project_id is None or identifier is None or not str(project_id).strip() or not str(identifier).strip():
        raise ValueError("Task requires both projectId and id")
    return f"{project_id}:{identifier}"


def task_tags(task: Mapping[str, Any]) -> list[str]:
    """Accept CLI string tags and name-bearing tag records."""
    raw = task.get("tags") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    result = []
    for item in raw:
        if isinstance(item, Mapping):
            item = item.get("name", "")
        if not isinstance(item, str):
            continue
        value = item.strip().lstrip("#").strip()
        if value and value not in result:
            result.append(value)
    return result


def normalize_task(task: Mapping[str, Any]) -> dict[str, Any]:
    """Produce an independent JSON-like snapshot without mutating source data."""
    task_key(task)
    value = copy.deepcopy(dict(task))
    value["id"] = str(value["id"])
    value["projectId"] = str(value["projectId"])
    for field in ("title", "content", "desc"):
        value[field] = str(value.get(field) or "")
    value["tags"] = task_tags(value)
    value["priority"] = _integer(value.get("priority"))
    value["status"] = _task_status(value)
    for field in ("items", "childIds"):
        if not isinstance(value.get(field), list):
            value[field] = []
    return value


def _without_etag(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _without_etag(item) for key, item in value.items() if key != "etag"}
    if isinstance(value, (list, tuple)):
        return [_without_etag(item) for item in value]
    return value


def task_fingerprint(task: Mapping[str, Any]) -> str:
    """Hash semantic content, excluding transport timestamps and etags."""
    normalized = normalize_task(task)
    semantic = {field: _without_etag(normalized.get(field)) for field in SEMANTIC_FIELDS}
    semantic["tags"] = sorted(semantic["tags"])
    encoded = json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def task_text(task: Mapping[str, Any]) -> str:
    parts = [str(task.get(field) or "") for field in ("title", "content", "desc")]
    for item in task.get("items") or []:
        if isinstance(item, Mapping):
            parts.append(str(item.get("title") or ""))
    return "\n".join(part for part in parts if part)


def is_completed(task: Mapping[str, Any]) -> bool:
    return _task_status(task) == 2


def classify(task: Mapping[str, Any], config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Scope, complexity, and workspace selection are independent decisions."""
    config = config or {}
    tags = task_tags(task)
    folded_tags = [tag.casefold() for tag in tags]
    eligibility_tag = str(config.get("eligibility_tag", "AI") or "").lstrip("#").casefold()
    configured_projects = {str(value) for value in (config.get("project_ids") or [])}
    eligible = (bool(eligibility_tag) and eligibility_tag in folded_tags) or str(task.get("projectId", "")) in configured_projects
    if is_completed(task):
        eligible, scope_reason = False, "源任务已完成"
    elif _task_status(task) != 0:
        eligible, scope_reason = False, "源任务不是可执行的未完成状态"
    elif eligible:
        scope_reason = "已进入允许执行范围"
    else:
        scope_reason = "不在允许的标签或清单范围"

    explicit = {
        "difficulty:easy": "easy", "difficulty:medium": "medium", "difficulty:hard": "hard",
        "简单": "easy", "中等": "medium", "复杂": "hard",
    }
    choices = {explicit[tag] for tag in folded_tags if tag in explicit}
    text = task_text(task)
    steps = len(task.get("items") or []) + len(re.findall(r"(?m)^\s*(?:[-*•]|\d+[.)、])\s*\S", text))
    if choices:
        difficulty = max(choices, key={"easy": 0, "medium": 1, "hard": 2}.get)
        difficulty_reason = "显式难度标签" if len(choices) == 1 else "难度标签冲突，采用最高难度"
    elif re.search(r"重构|架构|跨模块|迁移数据库|安全审计|分布式|\b(?:refactor(?:ing)?|architecture|distributed)\b", text, re.I):
        difficulty, difficulty_reason = "hard", "包含架构、重构或跨模块工作"
    elif len(text) >= 1600 or steps >= 8:
        difficulty, difficulty_reason = "hard", "长说明或较多步骤"
    elif len(text) >= 400 or steps >= 3:
        difficulty, difficulty_reason = "medium", "多步骤或较详细说明"
    else:
        difficulty, difficulty_reason = "easy", "短小且步骤较少"

    repo_tags = [tag.split(":", 1)[1].strip() for tag in tags if tag.casefold().startswith("repo:")]
    repositories = config.get("repositories") or {}
    repo = None
    if len(set(repo_tags)) == 1:
        selected = repositories.get(repo_tags[0])
        if isinstance(selected, (str, Path)) and str(selected).strip():
            repo = str(selected)
    code = bool(repo_tags) or bool(re.search(
        r"代码|源码|编程|函数|脚本|编译|重构|数据库|\b(?:python|javascript|typescript|react|bug|api|pytest|unittest|sql)\b",
        text, re.I,
    ))
    return {
        "eligible": bool(eligible), "difficulty": difficulty,
        "reason": f"{scope_reason}；{difficulty_reason}",
        "kind": "code" if code else "document", "repo": repo,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _due_date(value: Any) -> str | None:
    if not value:
        return None
    try:
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except ValueError:
        return None


class Store(ControlStoreMixin):
    """Each operation owns its connection; claims use an immediate transaction.

    Execution snapshots never change when the source completes. The service must compare the
    source fingerprint before writeback, and perform any scope recheck there.
    A missing source row is not evidence of completion or deletion.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._uri = self.path == ":memory:"
        self._memory_lock = threading.RLock()
        self._database = f"file:dispatcher-{uuid.uuid4().hex}?mode=memory&cache=shared" if self._uri else self.path
        self._anchor = self._connect() if self._uri else None
        if not self._uri:
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    difficulty TEXT NOT NULL, kind TEXT NOT NULL, repo TEXT, reason TEXT NOT NULL,
                    state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    run_dir TEXT, result TEXT, error TEXT, first_seen TEXT NOT NULL,
                    updated_at TEXT NOT NULL, priority INTEGER NOT NULL, due_date TEXT,
                    started_at TEXT, queued_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS queue_order ON tasks(state, priority DESC, due_date, queued_at);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS starts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, started_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS starts_by_time ON starts(started_at);
            """)
        self._migrate_controls()

    def close(self):
        anchor = getattr(self, '_anchor', None)
        if anchor is not None:
            anchor.close()
            self._anchor = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database, timeout=30, isolation_level=None, uri=self._uri)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _connection(self):
        # Shared-cache memory databases raise SQLITE_LOCKED without observing
        # busy_timeout. Serialize every memory connection, including readers.
        # File databases retain SQLite's cross-instance/process transaction lock.
        with self._memory_lock if self._uri else nullcontext():
            connection = self._connect()
            try:
                yield connection
            finally:
                connection.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["result"] = json.loads(result["result"]) if result["result"] is not None else None
        result["delegated"] = bool(result.get("delegated", False))
        result["paused"] = bool(result.get("paused", False))
        result["execution_overrides"] = json.loads(result.get("execution_overrides") or '{}')
        result["workspace_choice"] = json.loads(result.get("workspace_choice") or '{"mode":"default"}')
        result['input_context'] = json.loads(result['input_context']) if result.get('input_context') else None
        result['source_writeback_allowed'] = (result['input_context'] or {}).get('source_writeback_allowed', True)
        return result

    @staticmethod
    def _event(connection: sqlite3.Connection, task_id: str | None, message: str) -> None:
        connection.execute("INSERT INTO events(task_id, message, created_at) VALUES (?, ?, ?)", (task_id, message, _now()))

    def sync(self, tasks: Iterable[Mapping[str, Any]], config: Mapping[str, Any] | None = None) -> dict[str, int]:
        counts = dict.fromkeys(("seen", "added", "updated", "unchanged", "queued", "ignored", "cancelled", "deferred", "invalid"), 0)
        with self._transaction() as connection:
            for task in tasks:
                counts["seen"] += 1
                try:
                    payload = normalize_task(task)
                    identifier = task_key(payload)
                    fingerprint = task_fingerprint(payload)
                    encoded = json.dumps(payload, ensure_ascii=False)
                except (ValueError, TypeError):
                    counts["invalid"] += 1
                    self._event(connection, None, "Skipped source task without valid identity or JSON data")
                    continue
                policy = classify(payload, config)
                old = self._select_task(connection, identifier)
                if old is not None:
                    if old['delegated'] and payload['status'] == 0:
                        policy['eligible'] = True
                        policy['reason'] = '手动委派；' + policy['reason']
                    if old['paused']:
                        policy['eligible'] = False
                    if old['repo_alias']:
                        selected_repo = (config or {}).get('repositories', {}).get(old['repo_alias'])
                        policy['repo'] = str(selected_repo) if isinstance(selected_repo, (str, Path)) else None
                        policy['kind'] = 'code'
                timestamp = _now()
                values = (encoded, fingerprint, policy["difficulty"], policy["kind"], policy["repo"], policy["reason"], payload["priority"], _due_date(payload.get("dueDate")))
                if old is None:
                    state = "cancelled" if is_completed(payload) else ("queued" if policy["eligible"] else "ignored")
                    connection.execute("""INSERT INTO tasks (
                        payload, fingerprint, difficulty, kind, repo, reason, priority, due_date,
                        id, state, first_seen, updated_at, queued_at, source_status, source_checked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (*values, identifier, state,
                        timestamp, timestamp, timestamp, payload['status'], timestamp if payload['status'] is not None else None))
                    if state == 'cancelled':
                        connection.execute('INSERT INTO task_controls (task_id, paused) VALUES (?, 1)', (identifier,))
                    counts["added"] += 1
                    counts[state] += 1
                    self._event(connection, identifier, f"Source task added: {state}")
                    continue
                source_changed = payload['status'] is not None and old['source_status'] != payload['status']
                if payload['status'] is not None:
                    connection.execute('''UPDATE tasks SET source_status=?, source_checked_at=?,
                        revision=revision+? WHERE id=?''', (payload['status'], timestamp, int(source_changed), identifier))
                from .task_inputs import local_reply
                if local_reply(old):
                    # This is an explicitly requested answer about an already
                    # completed source. Never reopen, auto-requeue or write it.
                    if fingerprint != old['fingerprint'] and (old['state'] != 'cancelled' or not old['paused']):
                        connection.execute("UPDATE tasks SET state='cancelled',error=?,updated_at=?,revision=revision+1 WHERE id=?",
                                           ('Source changed; explicitly reassign before continuing', timestamp, identifier))
                        connection.execute('UPDATE task_controls SET paused=1 WHERE task_id=?', (identifier,))
                        # The completed/approved reply belongs to its original
                        # source snapshot. Do not rewrite its historical review.
                        if old['state'] in {'running', 'verifying'}:
                            self._sync_run(connection, old, 'cancelled', {}, timestamp)
                        counts['updated'] += 1
                    else:
                        counts['unchanged'] += 1
                    continue
                if is_completed(payload) and old['state'] != 'done':
                    # Source completion revokes this delegation, not the recorded
                    # execution evidence. A later reopen requires explicit assignment.
                    cancelled = old['state'] != 'cancelled' or not old['paused']
                    if cancelled or source_changed:
                        connection.execute('''INSERT INTO task_controls (task_id, paused) VALUES (?, 1)
                            ON CONFLICT(task_id) DO UPDATE SET paused=1''', (identifier,))
                        connection.execute("""UPDATE tasks SET state='cancelled', updated_at=?,
                            revision=revision+? WHERE id=?""", (timestamp, int(not source_changed), identifier))
                        self._sync_run(connection, old, 'cancelled', {}, timestamp)
                        self._event(connection, identifier,
                                    'Source task completed; delegation cancelled and execution history retained')
                        counts['updated'] += 1
                        counts['cancelled'] += int(cancelled)
                    else:
                        counts['unchanged'] += 1
                    continue
                if old['state'] == 'cancelled' and old['paused']:
                    # Keep the cancelled run and its pause through 0 -> 2 -> 0,
                    # including changes to the reopened task's content.
                    if source_changed:
                        connection.execute('UPDATE tasks SET updated_at=? WHERE id=?', (timestamp, identifier))
                        self._event(connection, identifier,
                                    'Source status changed; cancelled delegation still requires explicit reassignment')
                        counts['updated'] += 1
                    else:
                        counts['unchanged'] += 1
                    continue
                changed = old["fingerprint"] != fingerprint
                if old["state"] in PROTECTED_STATES:
                    counts["deferred" if changed else "unchanged"] += 1
                    if changed:
                        if old['state'] in {'awaiting_review', 'approved', 'needs_review'}:
                            error = 'Source changed after execution; review is stale. Refresh and explicitly reassign before approval.'
                            if old['state'] != 'needs_review' or old['error'] != error:
                                connection.execute("UPDATE tasks SET state='needs_review', error=?, revision=revision+1, updated_at=? WHERE id=?",
                                                   (error, timestamp, identifier))
                                self._sync_run(connection, old, 'needs_review', {}, timestamp)
                        self._event(connection, identifier, "Source changed while active; kept execution snapshot")
                    continue
                if changed and old["state"] == "done" and is_completed(payload):
                    completed_snapshot = json.loads(old["payload"])
                    completed_snapshot["status"] = 2
                    if task_fingerprint(completed_snapshot) == fingerprint:
                        # Our successful completion writeback changes status.
                        # Acknowledge it without erasing the verified run record.
                        connection.execute(
                            "UPDATE tasks SET payload=?, fingerprint=?, updated_at=?, revision=revision+1 WHERE id=?",
                            (encoded, fingerprint, timestamp, identifier),
                        )
                        counts["updated"] += 1
                        self._event(connection, identifier, "Source completion acknowledged; successful run retained")
                    else:
                        # A completed task may subsequently be edited. Retain
                        # the executed snapshot so old artifacts never appear
                        # to verify this new, unexecuted content.
                        counts["deferred"] += 1
                        self._event(connection, identifier, "Completed source edited; successful execution snapshot retained")
                    continue
                if changed:
                    connection.execute("UPDATE task_controls SET input_version=1,permission_profile='default' WHERE task_id=?", (identifier,))
                    state = "queued" if policy["eligible"] else ("cancelled" if old["state"] == "queued" or is_completed(payload) else "ignored")
                    if old['paused']:
                        state = 'revision_requested' if old['state'] == 'revision_requested' else 'ignored'
                    connection.execute("""UPDATE tasks SET payload=?, fingerprint=?, difficulty=?, kind=?, repo=?, reason=?, priority=?, due_date=?,
                        state=?, attempts=0, run_dir=NULL, result=NULL, error=NULL, started_at=NULL, current_run_id=NULL,
                        updated_at=?, queued_at=?, revision=revision+1 WHERE id=?""", (*values, state, timestamp, timestamp, identifier))
                    counts["updated"] += 1
                    if state in counts:
                        counts[state] += 1
                    self._event(connection, identifier, f"Source version changed: {state}")
                    continue
                state = old["state"]
                if state == "queued" and not policy["eligible"]:
                    state = "cancelled"
                    counts[state] += 1
                elif state == "ignored" and policy["eligible"]:
                    state = "queued"
                    counts[state] += 1
                counts["unchanged"] += 1
                meaningful_change = state != old['state'] or any(old[field] != value for field, value in zip(
                    ('difficulty', 'kind', 'repo', 'reason', 'priority', 'due_date'), values[2:]))
                connection.execute("""UPDATE tasks SET payload=?, fingerprint=?, difficulty=?, kind=?, repo=?, reason=?, priority=?, due_date=?,
                    state=?, updated_at=?, queued_at=?, revision=revision+? WHERE id=?""", (*values, state, timestamp,
                    timestamp if state != old["state"] else old["queued_at"], int(meaningful_change), identifier))
                if state != old["state"]:
                    self._event(connection, identifier, f"Scope changed: {state}")
        return counts

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            return self._row(self._select_task(connection, task_id))

    def list_tasks(self, *, states=None) -> list[dict[str, Any]]:
        where, parameters = '', ()
        if states is not None:
            parameters = tuple(states)
            if any(state not in STATES for state in parameters):
                raise ValueError('Unknown task state filter')
            if not parameters:
                return []
            where = ' WHERE t.state IN (' + ','.join('?' for _ in parameters) + ')'
        with self._connection() as connection:
            return [self._row(row) for row in connection.execute(f"SELECT {TASK_COLUMNS} FROM tasks t LEFT JOIN task_controls c ON c.task_id=t.id{where} ORDER BY first_seen, id", parameters)]

    def claim(self) -> dict[str, Any] | None:
        with self._transaction() as connection:
            row = connection.execute(f"""SELECT {TASK_COLUMNS} FROM tasks t LEFT JOIN task_controls c ON c.task_id=t.id
                WHERE state='queued' AND COALESCE(c.paused, 0)=0
                ORDER BY priority DESC, due_date IS NULL, due_date ASC, queued_at ASC, id ASC LIMIT 1""").fetchone()
            if row is None:
                return None
            timestamp = _now()
            run_id = uuid.uuid4().hex
            connection.execute("""UPDATE tasks SET state='running', attempts=attempts+1,
                started_at=?, updated_at=?, current_run_id=?, run_dir=NULL, result=NULL, error=NULL,
                revision=revision+1 WHERE id=?""", (timestamp, timestamp, run_id, row["id"]))
            context = json.loads(row['input_context']) if row['input_context'] else {}
            number = connection.execute('SELECT COUNT(*)+1 FROM runs WHERE task_id=?', (row['id'],)).fetchone()[0]
            connection.execute('''INSERT INTO runs (id, task_id, fingerprint, payload, executor, state, created_at, updated_at,
                input_version,parent_run_id,input_context,round_number)
                VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)''',
                (run_id, row['id'], row['fingerprint'], row['payload'], row['manual_executor'], timestamp, timestamp,
                 row['input_version'], context.get('parent_run_id'), row['input_context'], number))
            connection.execute("INSERT INTO starts(task_id, fingerprint, started_at) VALUES (?, ?, ?)", (row["id"], row["fingerprint"], timestamp))
            self._event(connection, row["id"], "Claimed for execution")
            claimed = self._row(self._select_task(connection, row['id']))
            # Consent is for one launch, never an automatic retry or future source edit.
            connection.execute("UPDATE task_controls SET permission_profile='default' WHERE task_id=?", (row['id'],))
            self._event(connection, row['id'], '本次工具权限：'+claimed['permission_profile']+'；结果仍需人工复审')
            return claimed

    def mark(self, task_id: str, state: str, expected_run_id: str | None = None, **fields: Any) -> dict[str, Any]:
        if state not in STATES:
            raise ValueError(f"Unknown task state: {state}")
        unknown = set(fields) - {"run_dir", "result", "error", "reason"}
        if unknown:
            raise ValueError(f"Unsupported task fields: {', '.join(sorted(unknown))}")
        encoded = dict(fields)
        if "result" in encoded and encoded["result"] is not None:
            if not isinstance(encoded["result"], dict):
                raise ValueError("Task result must be a dictionary or None")
            encoded["result"] = json.dumps(encoded["result"], ensure_ascii=False)
        if isinstance(encoded.get("run_dir"), Path):
            encoded["run_dir"] = str(encoded["run_dir"])
        encoded.update(state=state, updated_at=_now())
        with self._transaction() as connection:
            row = self._required_task(connection, task_id)
            if expected_run_id is not None and row['current_run_id'] != expected_run_id:
                raise StateConflict('Current run changed; stale execution result rejected')
            if row['state'] == 'cancelled' and row['paused'] and state != 'cancelled':
                raise StateConflict('Source completion cancelled this delegation; explicitly reassign before continuing')
            changed = any(row[name] != value for name, value in encoded.items() if name != 'updated_at')
            assignments = ", ".join(f"{name}=?" for name in encoded)
            connection.execute(f"UPDATE tasks SET {assignments}, revision=revision+? WHERE id=?", (*encoded.values(), int(changed), task_id))
            self._sync_run(connection, row, state, encoded, encoded['updated_at'])
            self._event(connection, task_id, f"State changed: {state}")
            return self._row(self._select_task(connection, task_id))

    def retry(self, task_id: str) -> dict[str, Any]:
        with self._transaction() as connection:
            row = self._select_task(connection, task_id)
            if row is None:
                raise KeyError(task_id)
            if row["state"] not in {"failed", "needs_review", "needs_input"}:
                raise ValueError(f"Cannot retry task in state {row['state']}")
            if row['paused']:
                raise StateConflict('Paused tasks require explicit fresh-source assignment')
            timestamp = _now()
            connection.execute("UPDATE tasks SET state='queued', error=NULL, updated_at=?, queued_at=?, revision=revision+1 WHERE id=?", (timestamp, timestamp, task_id))
            self._event(connection, task_id, "Manual retry queued; previous attempt count retained")
            return self._row(self._select_task(connection, task_id))

    def recover(self) -> int:
        with self._transaction() as connection:
            rows = connection.execute("SELECT * FROM tasks WHERE state IN ('running', 'verifying')").fetchall()
            timestamp = _now()
            for row in rows:
                connection.execute("UPDATE tasks SET state='needs_review', updated_at=?, revision=revision+1 WHERE id=?", (timestamp, row["id"]))
                self._sync_run(connection, row, 'needs_review', {}, timestamp)
                self._event(connection, row["id"], "Interrupted execution retained for review; no automatic retry")
            return len(rows)

    def event(self, task_id: str | None, message: str) -> None:
        with self._connection() as connection:
            self._event(connection, task_id, message)

    def events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (max(0, int(limit)),))
            return [dict(row) for row in rows]

    def count_started_today(self) -> int:
        local_now = datetime.now().astimezone()
        start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        bounds = (start.astimezone(timezone.utc).isoformat(timespec="microseconds"), end.astimezone(timezone.utc).isoformat(timespec="microseconds"))
        with self._connection() as connection:
            return connection.execute("SELECT COUNT(*) FROM starts WHERE started_at>=? AND started_at<?", bounds).fetchone()[0]
