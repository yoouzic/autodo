"""Deterministic repository checks owned by local configuration, not tasks."""

import json
import math
from pathlib import Path, PureWindowsPath

from . import adapters, executors


def _report_path(workspace, name):
    if not isinstance(name, str) or not name.strip():
        raise ValueError('Repository check report must be a relative file path')
    relative = Path(name)
    windows = PureWindowsPath(name)
    if relative.is_absolute() or windows.drive or windows.root or '..' in relative.parts or '..' in windows.parts:
        raise ValueError('Repository check report must stay inside workspace')
    path = workspace / relative
    if path.is_symlink():
        raise ValueError('Repository check report must not overwrite a symlink')
    from .workspace_runtime import validate_artifact_path
    validate_artifact_path(workspace, name)
    resolved = path.resolve()
    if resolved == workspace or not resolved.is_relative_to(workspace):
        raise ValueError('Repository check report resolves outside workspace')
    return path


def _block(value, language='text'):
    # Preserve arbitrary command output without letting embedded fences end it.
    fence = '```'
    while fence in value:
        fence += '`'
    return f'{fence}{language}\n{value}\n{fence}\n'


def run_repository_checks(row, run_dir, config, codex_argv, *, workspace=None):
    """Run configured argv in the run workspace and report only actual success."""
    configured = config.get('repository_checks') or {}
    source = row.get('repo')
    if not configured or not source:
        return None
    repositories = config.get('repositories') or {}
    source = Path(source).resolve()
    spec = None
    for alias, candidate in configured.items():
        repository = repositories.get(alias)
        if repository and Path(repository).resolve() == source:
            spec = candidate
            break
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise ValueError('Repository check configuration must be an object')
    commands = spec.get('commands')
    if not isinstance(commands, list) or not commands or any(
        not isinstance(argv, list) or not argv or
        any(not isinstance(arg, str) or '\x00' in arg for arg in argv) or not argv[0].strip()
        for argv in commands
    ):
        raise ValueError('Repository check commands must be nonempty argv arrays of strings')
    commands = [list(argv) for argv in commands]
    run_dir = Path(run_dir).resolve()
    workspace = Path(workspace).resolve() if workspace is not None else (run_dir / 'workspace').resolve()
    report_name = spec.get('report', 'REPORT.md')
    _report_path(workspace, report_name)
    timeout = config.get('task_timeout_seconds', 600)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Repository check timeout must be a positive finite number')
    timeout = min(timeout, 600)
    results = []
    for number, argv in enumerate(commands, 1):
        try:
            command = executors.sandbox_command(codex_argv, argv, workspace, config.get('windows_sandbox', 'unelevated'))
            stdout, stderr = adapters.run_process(command, cwd=workspace,
                                                 log_prefix=run_dir / f'check-{number:02d}', timeout=timeout)
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            raise RuntimeError(f'Repository check {number} failed: {exc}; logs: {run_dir / f"check-{number:02d}"}') from exc
        results.append({'argv': argv, 'stdout': stdout, 'stderr': stderr, 'exit_code': 0})
    lines = ['# Repository check results\n', 'All configured commands completed with exit code 0.\n']
    for number, result in enumerate(results, 1):
        lines.extend([f'## Check {number}\n', 'argv:\n', _block(json.dumps(result['argv'], ensure_ascii=False), 'json'),
                      'exit_code: 0\n', 'stdout:\n', _block(result['stdout']), 'stderr:\n', _block(result['stderr'])])
    report = _report_path(workspace, report_name)
    report.parent.mkdir(parents=True, exist_ok=True)
    report = _report_path(workspace, report_name)
    report.write_text('\n'.join(lines), encoding='utf-8')
    return {'commands': results, 'report': report.relative_to(workspace).as_posix()}
