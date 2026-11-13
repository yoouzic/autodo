"""Human-readable project/run index generated from the task store."""
from pathlib import Path


def workspace_paths(root, row, *, store=None):
    """Use host-frozen run fields, never agent-report fields, for navigation."""
    from .workspace_copy import _safe_path
    root = Path(root).resolve()
    folder = Path(row['run_dir']) if row.get('run_dir') else root
    if not folder.is_absolute():
        folder = root / folder
    folder = _safe_path(folder)
    trusted = row if store is None else {}
    if store is not None and row.get('current_run_id'):
        trusted = store.get_run(row['current_run_id'])
        if not isinstance(trusted, dict) or trusted.get('task_id') != row['id'] or not trusted.get('run_dir'):
            raise ValueError('运行记录与当前任务不一致，不能选择续接目录')
        saved_folder = Path(trusted['run_dir'])
        if not saved_folder.is_absolute():
            saved_folder = root / saved_folder
        if _safe_path(saved_folder) != folder:
            raise ValueError('运行目录与冻结记录不一致')
    binding = trusted.get('workspace_binding')
    if binding is not None and (not isinstance(binding, dict) or
                                not isinstance(binding.get('workspace_path'), str)):
        raise ValueError('运行工作目录绑定无效')
    legacy = folder / 'workspace' if row.get('run_dir') else root
    live = _safe_path(binding['workspace_path']) if binding is not None else legacy
    snapshot = _safe_path(trusted.get('artifact_root') or
                          (binding or {}).get('snapshot_path') or legacy)
    if row.get('run_dir') and not snapshot.is_relative_to(folder):
        raise ValueError('验收快照不在本次运行目录内')
    return _safe_path(live), snapshot


def write_index(root, rows, *, store=None):
    root = Path(root)
    destination = root/'workspaces'
    destination.mkdir(exist_ok=True)
    lines = ['# 项目与任务索引', '', '由派工器自动生成。新建或选择已有工作区会在执行目录原地修改；每次运行的日志与验收快照独立保留。', '']
    for row in rows:
        if not row.get('run_dir'):
            continue
        title = row['payload'].get('title', row['id']).replace('\n', ' ')
        folder = Path(row['run_dir'])
        result = row.get('result') or {}
        execution = result.get('execution') or result
        executor = execution.get('executor') or result.get('executor') or 'codex'
        route = execution.get('route') or result.get('route') or {}
        route_reason = route.get('reason') or execution.get('route_reason') or result.get('route_reason')
        lines += [f'## {title}', '', f'- 状态：{row["state"]}',
                  f'- 任务 ID：`{row["id"]}`', f'- 运行目录：[打开]({folder.as_posix()})']
        try:
            live, snapshot = workspace_paths(root, row, store=store)
            lines += [f'- 执行工作目录：[打开]({live.as_posix()})',
                      f'- 验收快照：[打开]({snapshot.as_posix()})']
        except (OSError, ValueError, RuntimeError) as exc:
            snapshot = None
            lines.append(f'- 目录绑定不可用：{exc}')
        lines.append(f'- 执行器：`{executor}`')
        if route_reason:
            lines.append(f'- 路由原因：{route_reason}')
        if executor == 'codex' and execution.get('thread_id'):
            lines += [f'- Codex 会话：`{execution["thread_id"]}`',
                      f'- 续接：`python -m dispatcher resume "{row["id"]}"`，随后输入 `/app`。']
        elif executor != 'codex':
            if execution.get('session_id'):
                lines.append(f'- {executor} 会话：`{execution["session_id"]}`')
            if execution.get('resume_available') is False:
                lines.append('- 此执行会话未持久化，session ID 仅供诊断；产物和执行日志仍保留。')
            elif (folder/'execution-resume.txt').is_file():
                lines += [f'- 恢复说明：[execution-resume.txt]({(folder/"execution-resume.txt").as_posix()})',
                          f'- 续接：`python -m dispatcher resume "{row["id"]}"`，按说明在厂商入口恢复。']
        for artifact in result.get('artifacts', []):
            if snapshot is None:
                continue
            from .workspace_runtime import validate_artifact_path
            try:
                artifact_path = validate_artifact_path(snapshot, artifact)
            except (OSError, ValueError, RuntimeError):
                continue
            lines.append(f'- 产物：[{artifact_path.name}]({artifact_path.as_posix()})')
        lines += ['']
    (destination/'INDEX.md').write_text('\n'.join(lines), encoding='utf-8')
