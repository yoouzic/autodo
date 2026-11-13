"""Host-owned workspace bindings, leases and post-execution review snapshots."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import uuid
from pathlib import Path, PureWindowsPath

from .control_store import validate_workspace_choice
from .workspace_copy import _check_components, _directory, _excluded, _safe_path, _within, copy_workspace
from .workspace_lock import WorkspaceLease
from .workspace_guard import WorkspaceGuardError, active_guards


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _assert_unprotected(root, workspace):
    try:
        guards = active_guards(root)
    except WorkspaceGuardError:
        raise ValueError('工作区持续保护记录暂不可确认，不能准备新的执行目录。') from None
    for guard in guards:
        if _within(workspace, guard['workspace']) or _within(guard['workspace'], workspace):
            if guard['status'] == 'quarantined':
                raise ValueError('该目录或其上下级已隔离：上次任务未确认停止，不能复用。')
            raise ValueError('该目录或其上下级仍有未确认结束的任务保护记录，暂不能复用。')


def release_workspace(row):
    lease = row.pop('_workspace_lease', None)
    if lease is not None:
        lease.__exit__(None, None, None)


def _source_metadata(source):
    result = {}
    for name in ('id', 'path', 'label', 'origin', 'source_run_id', 'repo_alias', 'task_id', 'source_label',
                 'collection_label', 'collection_root', 'collection_agent', 'relative_path', 'category'):
        value = source.get(name)
        if isinstance(value, str):
            result[name] = value
    agents = source.get('agents')
    if isinstance(agents, list) and all(isinstance(value, str) for value in agents):
        result['agents'] = list(agents)
    return result


def prepare_workspace(root, row, executor, task_dir, stamp, clone_repo):
    root = Path(root).resolve()
    selection = row.get('workspace_choice')
    choice = validate_workspace_choice({'mode': 'default'} if selection is None else selection)
    mode = choice['mode']
    if row.get('_workspace_lease') is not None:
        raise ValueError('This run already holds a workspace lease')
    source = {}
    if mode == 'new':
        task_dir = root / 'workspaces' / executor / (choice['name'] + '--ws-' + uuid.uuid4().hex)
        actual = task_dir / 'project'
        name = choice['name']
    elif mode == 'existing':
        selected = row.get('_workspace_source')
        if (not isinstance(selected, dict) or selected.get('id') != choice['workspace_id'] or
                selected.get('available') is False or not isinstance(selected.get('path'), str)):
            raise ValueError('Existing workspace selection is missing, unavailable or changed')
        actual = _safe_path(selected['path'])
        _directory(actual)
        key = os.path.normpath(str(actual)).replace('\\', '/').casefold().rstrip('/')
        if 'ws_' + hashlib.sha256(key.encode('utf-8')).hexdigest()[:24] != choice['workspace_id']:
            raise ValueError('Existing workspace ID no longer matches its path')
        source = _source_metadata(selected)
        name = selected.get('label') or actual.name
    else:
        actual = task_dir / 'runs' / stamp / 'workspace'
        name = task_dir.name
    run_dir = task_dir / 'runs' / stamp
    snapshot = run_dir / 'workspace'
    actual = _safe_path(actual)
    _check_components(run_dir)
    if not run_dir.resolve().is_relative_to(root / 'workspaces'):
        raise ValueError('Run metadata escapes managed workspaces')
    if mode != 'default':
        if _within(actual, root / 'data') or _within(root / 'data', actual) or _within(run_dir, actual):
            raise ValueError('Workspace overlaps dispatcher metadata or the run snapshot')
    binding = {'version': 1, 'mode': mode, 'strategy': 'fresh' if mode == 'default' else 'in_place',
               'workspace_path': str(actual), 'snapshot_path': str(snapshot),
               'name': str(name), 'source': source}
    if isinstance(row.get('repo_alias'), str) and row['repo_alias'].strip():
        binding['repo_alias'] = row['repo_alias']
    if isinstance(row.get('repo'), (str, Path)) and str(row['repo']).strip():
        binding['repo_path'] = str(Path(row['repo']).resolve())
    _assert_unprotected(root, actual)
    try:
        # Fresh run directories also need a lease: discovery may expose a run
        # before its remote worker's durable guard has been registered.
        lease = WorkspaceLease(root, actual, str(row.get('current_run_id') or uuid.uuid4().hex))
        lease.__enter__()
        row['_workspace_lease'] = lease
        _assert_unprotected(root, actual)
        if mode == 'existing':
            store = row.get('_workspace_store')
            if store is None and _within(actual, root / 'workspaces'):
                raise ValueError('Managed workspace reuse requires its history store')
            if store is not None:
                from .workspace import WorkspaceManager
                WorkspaceManager(root, store).archive_for_reuse(actual)
        run_dir.mkdir(parents=True)
        if mode != 'existing':
            if row.get('repo'):
                repository = Path(row['repo']).resolve()
                if not repository.is_dir():
                    raise ValueError(f'Repository not found: {repository}')
                clone_repo(repository, actual)
            else:
                actual.mkdir()
        identity = _directory(actual)
        row['_workspace_identity'] = (identity.st_dev, identity.st_ino)
        (run_dir / 'workspace-binding.json').write_text(_json(binding), encoding='utf-8')
        row['_workspace_binding'] = copy.deepcopy(binding)
        return run_dir, task_dir
    except BaseException:
        release_workspace(row)
        raise


def workspace_binding(row, run_dir):
    path = Path(run_dir) / 'workspace-binding.json'
    frozen = row.get('_workspace_binding')
    if frozen is None:
        if path.exists():
            raise ValueError('Unbound workspace metadata cannot be used to resume a run')
        return None  # Compatibility for older direct invocation helpers.
    _check_components(path)
    if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('Workspace binding metadata is missing or invalid')
    if _json(json.loads(path.read_text(encoding='utf-8'))) != _json(frozen):
        raise ValueError('Workspace binding changed after this run was prepared')
    return copy.deepcopy(frozen)


def workspace_path(row, run_dir):
    binding = workspace_binding(row, run_dir)
    if binding is not None:
        lease = row.get('_workspace_lease')
        if lease is None or getattr(lease, '_handle', None) is None:
            raise ValueError('Execution requires the active workspace lease')
    path = Path(binding['workspace_path']) if binding else Path(run_dir) / 'workspace'
    path = _safe_path(path)
    info = _directory(path)
    if row.get('_workspace_identity') is not None and (info.st_dev, info.st_ino) != row['_workspace_identity']:
        raise ValueError('Workspace directory identity changed after preparation')
    return path


def validate_artifact_path(workspace, name):
    if not isinstance(name, str) or not name or '\x00' in name:
        raise ValueError('Invalid artifact path')
    windows = PureWindowsPath(name)
    parts = name.replace('\\', '/').split('/')
    if windows.drive or windows.root or name.startswith('/') or '..' in parts or ':' in name:
        raise ValueError('Artifacts must use relative paths within the workspace')
    parts = [part for part in parts if part not in {'', '.'}]
    if not parts or any(_excluded(part, index < len(parts) - 1) for index, part in enumerate(parts)):
        raise ValueError('Runtime, authentication and cache paths cannot be artifacts')
    path = Path(workspace).joinpath(*parts)
    _check_components(path)
    return path.resolve()


def snapshot_workspace(row, run_dir):
    binding = workspace_binding(row, run_dir)
    if binding is None or binding['strategy'] == 'fresh':
        return None
    source = workspace_path(row, run_dir)
    snapshot = Path(binding['snapshot_path'])
    if snapshot != Path(run_dir) / 'workspace':
        raise ValueError('Snapshot must belong to this run')
    receipt = copy_workspace(source, snapshot)
    (Path(run_dir) / 'workspace-snapshot.json').write_text(_json(receipt), encoding='utf-8')
    return receipt
