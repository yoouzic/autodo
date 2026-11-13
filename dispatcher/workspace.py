"""Read-only, bounded access to managed run history and declared artifacts."""

from __future__ import annotations

import copy
import json
import os
import re
import uuid
from pathlib import Path, PureWindowsPath

from .core import task_key
from .control_store import _artifact_directory, _reject_path_links
from .workspace_copy import _excluded, copy_workspace


JSON_LIMIT = 2 * 1024 * 1024
TEXT_LIMIT = 128 * 1024
BINARY_SUFFIXES = frozenset({
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.odt', '.ods',
    '.odp', '.zip', '.gz', '.7z', '.rar', '.exe', '.dll', '.sqlite', '.sqlite3',
    '.db', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.bmp', '.tif',
    '.tiff', '.mp3', '.mp4', '.wav', '.ogg', '.webm', '.woff', '.woff2', '.ttf',
})
SECRET_LINE = re.compile(r'authorization|bearer\s|\bsk-[\w-]+|api[_ -]?key|access[_ -]?token', re.I)


def _inside(path: Path, parent: Path, *, allow_root=False) -> bool:
    return path.is_relative_to(parent) and (allow_root or path != parent)


def _artifact_name(value: str) -> str:
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError('Artifact path must be a nonempty relative path')
    windows = PureWindowsPath(value)
    parts = value.replace('\\', '/').split('/')
    if windows.drive or windows.root or value.startswith('/') or '..' in parts or ':' in value:
        raise ValueError('Artifact path must stay inside the workspace')
    parts = [part for part in parts if part not in {'', '.'}]
    if any(part.endswith((' ', '.')) or _excluded(part, index < len(parts) - 1)
           for index, part in enumerate(parts)):
        raise ValueError('Runtime, authentication and private metadata paths cannot be artifacts')
    normalized = '/'.join(parts)
    if not normalized:
        raise ValueError('Artifact path must name a file')
    return normalized


class WorkspaceManager:
    def __init__(self, root, store):
        self.root = Path(root).resolve()
        self.store = store

    def _managed_root(self):
        managed = self.root / 'workspaces'
        _reject_path_links(managed)
        managed = managed.resolve()
        if not _inside(managed, self.root):
            raise ValueError('Managed workspace root escapes the application root')
        return managed

    def _run_path(self, value):
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise ValueError('Run has no workspace directory')
        path = Path(value)
        if not path.is_absolute():
            path = self.root / path
        _reject_path_links(path)
        path = path.resolve()
        if not _inside(path, self._managed_root()):
            raise ValueError('Run directory is outside managed workspaces')
        if not path.is_dir():
            raise FileNotFoundError('Run directory does not exist')
        return path

    def _record(self, run_id):
        row = self.store.get_run(run_id)
        if row is None:
            raise KeyError(run_id)
        return row

    def _workspace(self, row):
        run = self._run_path(row.get('run_dir'))
        artifact_root = row.get('artifact_root')
        workspace = _artifact_directory(run / 'workspace' if artifact_root is None else artifact_root)
        if not _inside(workspace, run):
            raise ValueError('Workspace directory escapes its run')
        if not workspace.is_dir():
            raise FileNotFoundError('Workspace directory does not exist')
        return run, workspace

    def resolve_run(self, run_id) -> Path:
        run, _ = self._workspace(self._record(run_id))
        return run

    @staticmethod
    def _declared(row):
        result = row.get('result')
        if not isinstance(result, dict):
            return []
        artifacts = result.get('artifacts')
        if not isinstance(artifacts, list):
            execution = result.get('execution')
            artifacts = execution.get('artifacts') if isinstance(execution, dict) else []
        if not isinstance(artifacts, list):
            return []
        names = []
        for value in artifacts:
            try:
                name = _artifact_name(value)
            except ValueError:
                continue
            if name not in names:
                names.append(name)
        return names

    @staticmethod
    def _candidate(workspace, name):
        candidate = workspace / name
        _reject_path_links(candidate)
        candidate = candidate.resolve()
        if not _inside(candidate, workspace):
            raise ValueError('Artifact resolves outside its workspace')
        _artifact_name(candidate.relative_to(workspace).as_posix())
        return candidate

    @staticmethod
    def _archive_hashes(row):
        result = row.get('result') if isinstance(row.get('result'), dict) else {}
        execution = result.get('execution') if isinstance(result.get('execution'), dict) else {}
        hashes = {}
        for evidence in (result, execution):
            if 'sha256' not in evidence:
                continue
            if not isinstance(evidence['sha256'], dict):
                raise ValueError('Historical artifact hash manifest is invalid')
            for raw_name, digest in evidence['sha256'].items():
                name = _artifact_name(raw_name)
                if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', digest):
                    raise ValueError('Historical artifact hash manifest is invalid')
                digest = digest.lower()
                if name in hashes and hashes[name] != digest:
                    raise ValueError('Historical artifact hash manifests disagree')
                hashes[name] = digest
        if row.get('review_status') == 'approved' and not hashes:
            from .result_receipt import answer_digest
            if not answer_digest(result):
                raise ValueError('Approved history requires an artifact manifest or answer receipt')
        return hashes

    def archive_for_reuse(self, actual_path):
        """Preserve exact old live roots before in-place reuse under WorkspaceLease.

        Parent or child selections are not expanded here. The runtime must reject
        them before mutation. Failed copies remain as unbound candidates; no source
        is changed.
        """
        actual = _artifact_directory(actual_path)
        plans = []
        for row in self.store.list_runs(include_archived=True):
            if not row.get('run_dir'):
                continue
            run_value = Path(row['run_dir'])
            if not run_value.is_absolute():
                run_value = self.root / run_value
            candidate = row.get('artifact_root') or run_value / 'workspace'
            if Path(os.path.abspath(candidate)) != actual:
                continue
            run, workspace = self._workspace(row)
            if workspace != actual:
                raise ValueError('Historical workspace identity changed before archiving')
            if row.get('state') in {'running', 'verifying'}:
                raise ValueError('Running or verifying history cannot be reused')
            if row.get('artifact_root'):
                raise ValueError('Frozen artifact snapshots cannot be reused in place')
            plans.append((row, run, self._archive_hashes(row)))
        receipts = []
        for row, run, hashes in plans:
            parent = run / 'artifact-archive'
            _reject_path_links(parent)
            parent.mkdir(exist_ok=True)
            _artifact_directory(parent)
            destination = parent / uuid.uuid4().hex
            copied = copy_workspace(actual, destination)
            manifest = {entry['path']: entry['sha256'] for entry in copied['manifest']}
            if (any(manifest.get(name) != digest for name, digest in hashes.items()) or
                    any(name not in manifest for name in self._declared(row))):
                raise ValueError('Snapshot does not match the historical declared artifact manifest')
            frozen = self.store.freeze_artifact_root(row['id'], str(destination), str(run))
            receipts.append({'run_id': row['id'], 'artifact_root': frozen['artifact_root'],
                             'manifest_sha256': copied['manifest_sha256']})
        return receipts

    def _enrich(self, row):
        info = copy.deepcopy(row)
        result = info.get('result') or {}
        execution = result.get('execution') or result
        info['executor'] = info.get('executor') or execution.get('executor') or ('codex' if execution.get('thread_id') else None)
        payload = info.get('payload')
        info['title'] = str(payload.get('title') or '') if isinstance(payload, dict) else ''
        info['artifacts'] = []
        try:
            _, workspace = self._workspace(row)
        except (ValueError, OSError, RuntimeError) as exc:
            info['workspace'] = None
            info['workspace_error'] = str(exc)
            return info
        info['workspace'] = str(workspace)
        for name in self._declared(row):
            try:
                path = self._candidate(workspace, name)
                if path.exists() and not path.is_file():
                    continue
                exists = path.is_file()
                info['artifacts'].append({'path': name, 'size': path.stat().st_size if exists else None, 'exists': exists})
            except (ValueError, OSError, RuntimeError):
                continue
        return info

    def list_runs(self, task_id=None, include_archived=True):
        return [self._enrich(row) for row in self.store.list_runs(task_id=task_id, include_archived=include_archived)]

    def get_run(self, run_id):
        return self._enrich(self._record(run_id))

    def resolve_artifact(self, run_id, path) -> Path:
        row = self._record(run_id)
        name = _artifact_name(path)
        if name not in self._declared(row):
            raise ValueError('Artifact was not declared by this run')
        _, workspace = self._workspace(row)
        candidate = self._candidate(workspace, name)
        if not candidate.is_file():
            raise FileNotFoundError('Declared artifact file does not exist')
        return candidate

    def preview(self, run_id, path):
        artifact = self.resolve_artifact(run_id, path)
        size = artifact.stat().st_size
        result = {'name': artifact.name, 'text': '', 'size': size, 'truncated': False, 'binary': False}
        if artifact.suffix.lower() in BINARY_SUFFIXES:
            result['binary'] = True
            return result
        with artifact.open('rb') as stream:
            data = stream.read(TEXT_LIMIT)
        result['truncated'] = size > TEXT_LIMIT
        if b'\x00' in data:
            result['binary'] = True
            return result
        try:
            # An incomplete code point at the preview boundary is not corruption.
            import codecs
            decoder = codecs.getincrementaldecoder('utf-8')('strict')
            result['text'] = decoder.decode(data, final=not result['truncated'])
        except UnicodeDecodeError:
            result['binary'] = True
        return result

    def log(self, run_id, phase, stream):
        if phase not in {'execution', 'verification'} or stream not in {'events', 'stderr'}:
            raise ValueError('Unknown log phase or stream')
        run = self.resolve_run(run_id)
        filename = f'{phase}.events.jsonl' if stream == 'events' else f'{phase}.stderr.log'
        log_path = (run / filename).resolve()
        if not _inside(log_path, run):
            raise ValueError('Log resolves outside its run')
        if not log_path.is_file():
            return {'text': '', 'truncated': False, 'message': 'Log file does not exist'}
        size = log_path.stat().st_size
        truncated = size > TEXT_LIMIT
        with log_path.open('rb') as handle:
            if truncated:
                handle.seek(-TEXT_LIMIT, os.SEEK_END)
            data = handle.read(TEXT_LIMIT)
        if truncated:
            # Never expose a partial secret line whose identifying prefix was cut off.
            data = data.partition(b'\n')[2]
        lines = data.decode('utf-8', errors='replace').replace('\r\n', '\n').splitlines(keepends=True)
        text = ''.join(line for line in lines if not SECRET_LINE.search(line))
        return {'text': text, 'truncated': truncated}

    def open(self, run_id):
        _, workspace = self._workspace(self._record(run_id))
        if not hasattr(os, 'startfile'):
            raise OSError('Opening workspaces is supported on Windows only')
        os.startfile(str(workspace))
        return {'workspace': str(workspace), 'opened': True}

    @staticmethod
    def _read_json(run, name, required=False):
        path = (run / name).resolve()
        if not _inside(path, run):
            raise ValueError('Run metadata resolves outside its run')
        if not path.exists() and not required:
            return {}
        if not path.is_file() or path.stat().st_size > JSON_LIMIT:
            raise ValueError('Run metadata is not a bounded JSON file')
        with path.open('rb') as handle:
            data = handle.read(JSON_LIMIT + 1)
        if len(data) > JSON_LIMIT:
            raise ValueError('Run metadata exceeds size limit')
        value = json.loads(data.decode('utf-8-sig'))
        if not isinstance(value, dict):
            raise ValueError('Run metadata must be a JSON object')
        return value

    def import_history(self):
        counts = {'scanned': 0, 'imported': 0, 'existing': 0, 'unknown_task': 0, 'invalid': 0}
        tasks = {row['id']: row for row in self.store.list_tasks()}
        existing = set()
        for row in self.store.list_runs(include_archived=True):
            try:
                existing.add(self._run_path(row.get('run_dir')))
            except (ValueError, OSError, RuntimeError):
                continue
        managed = self._managed_root()
        for task_file in managed.glob('*/*/runs/*/task.json'):
            counts['scanned'] += 1
            try:
                run = self._run_path(task_file.parent)
                if run in existing:
                    counts['existing'] += 1
                    continue
                payload = self._read_json(run, 'task.json', required=True)
                identifier = task_key(payload)
                if identifier not in tasks:
                    counts['unknown_task'] += 1
                    continue
                execution = self._read_json(run, 'execution.json')
                verification = self._read_json(run, 'verification.json')
                routing = self._read_json(run, 'routing.json')
                current = tasks[identifier]
                is_current = False
                if current.get('run_dir'):
                    try:
                        is_current = self._run_path(current['run_dir']) == run
                    except (ValueError, OSError, RuntimeError):
                        pass
                if is_current:
                    result, state = current.get('result'), current.get('state', 'needs_review')
                else:
                    result = verification or execution or None
                    states = {execution.get('status'), verification.get('status')}
                    state = ('failed' if states & {'failed', 'error'} else
                             'done' if verification.get('status') == 'complete' else 'needs_review')
                    if execution.get('status') == 'recovered':
                        # Files alone cannot recreate the human approval record.
                        state = 'needs_review'
                        result = dict(result or {}, execution=execution,
                                      runtime_warning=execution.get('runtime_warning'))
                executor = ((result or {}).get('executor') or execution.get('executor') or routing.get('executor'))
                self.store.import_run(task_id=identifier, run_dir=str(run), payload=payload,
                                      result=result, state=state, executor=executor)
                existing.add(run)
                counts['imported'] += 1
            except (ValueError, OSError, RuntimeError, TypeError):
                counts['invalid'] += 1
        return counts
