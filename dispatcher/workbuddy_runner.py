"""Dispatcher side of the authenticated desktop MCP worker.

MCP submission revokes its scoped file capability; it is not a verification or a
source completion. Native session model selection remains in the desktop host.
"""
from __future__ import annotations

import json
import re
import stat
import time
import unicodedata
from pathlib import Path

from .routing import RoutingError
from .workspace_runtime import validate_artifact_path
from .workspace_copy import _check_components, _directory, _identity, _no_link, _read_file, _safe_path


MAX_REPORT_BYTES = 256 * 1024
MAX_ARTIFACTS = 256
MAX_TEXT_CHARS = 65536
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_ARTIFACT_TOTAL_BYTES = 512 * 1024 * 1024
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_DEVICES = {'CON', 'PRN', 'AUX', 'NUL'} | {
    prefix + str(number) for prefix in ('COM', 'LPT') for number in range(1, 10)}


class DesktopWorkerUnavailable(RoutingError):
    pass


class WorkBuddyWorkerUnavailable(DesktopWorkerUnavailable):
    """Compatibility name for the original WorkBuddy worker."""


DESKTOP_PROVIDERS={'workbuddy':'WorkBuddy','doubao':'豆包'}


def _provider(executor):
    if executor not in DESKTOP_PROVIDERS:raise ValueError('Unsupported desktop executor')
    return DESKTOP_PROVIDERS[executor]


def _bounded_text(value, maximum):
    return isinstance(value, str) and len(value) <= maximum and '\x00' not in value


def _artifact_info(path):
    _check_components(path)
    info = path.lstat()
    _no_link(info)
    if (not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_ARTIFACT_BYTES or
            getattr(info, 'st_nlink', 1) != 1):
        raise ValueError('Desktop MCP artifact must be a bounded nonempty ordinary file without links')
    return info


def _validated_submission(workspace, state):
    """Freeze the bounded report and verify exact submitted bytes without following links."""
    workspace = _safe_path(workspace)
    workspace_identity = _identity(_directory(workspace))
    result = state.get('submission')
    keys = {'status', 'summary', 'artifacts', 'checks', 'needs_input'}
    if not isinstance(result, dict) or set(result) != keys:
        raise ValueError('Desktop MCP submission has an invalid report shape')
    if not isinstance(result['status'], str) or result['status'] not in ('complete', 'needs_input', 'failed'):
        raise ValueError('Desktop MCP submission has an invalid status')
    if any(not _bounded_text(result[key], MAX_TEXT_CHARS) for key in ('summary', 'needs_input')):
        raise ValueError('Desktop MCP submission has invalid or oversized text')
    if any(not isinstance(result[key], list) or len(result[key]) > MAX_ARTIFACTS or
           any(not _bounded_text(value, 4096) for value in result[key]) for key in ('artifacts', 'checks')):
        raise ValueError('Desktop MCP submission has invalid or oversized artifact/check lists')
    result = {key: list(value) if isinstance(value, list) else value for key, value in result.items()}
    try:
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, UnicodeError):
        raise ValueError('Desktop MCP submission text is invalid') from None
    if len(encoded) > MAX_REPORT_BYTES:
        raise ValueError('Desktop MCP submission exceeds its metadata limit')
    hashes = state.get('artifact_sha256')
    if (not isinstance(hashes, dict) or len(hashes) > MAX_ARTIFACTS or
            any(not _bounded_text(name, 4096) or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
                for name, digest in hashes.items())):
        raise ValueError('Desktop MCP submission requires a valid artifact hash manifest')
    hashes = dict(hashes)
    actual, paths, seen, total = {}, [], set(), 0
    for name in result['artifacts']:
        parts = name.split('/')
        if ('\\' in name or any(not part or part in {'.', '..'} or part != part.rstrip(' .') or
                part.split('.')[0].upper() in _DEVICES or
                any(unicodedata.category(char) in {'Cc', 'Cf', 'Cs'} for char in part) for part in parts)):
            raise ValueError('Desktop MCP artifact path is not a canonical relative path')
        path = validate_artifact_path(workspace, name)
        if not path.is_relative_to(workspace):
            raise ValueError('Desktop MCP artifact escapes its workspace')
        relative = path.relative_to(workspace).as_posix()
        if relative.casefold() in seen:
            raise ValueError('Desktop MCP artifact list contains aliases or duplicates')
        seen.add(relative.casefold())
        info = _artifact_info(path)
        total += info.st_size
        if total > MAX_ARTIFACT_TOTAL_BYTES:
            raise ValueError('Desktop MCP artifacts exceed their total byte limit')
        actual[relative] = _read_file(path, _identity(info))
        paths.append((path, relative))
    if actual != hashes or (result['status'] == 'complete' and not actual):
        raise ValueError('Desktop MCP artifact changed after submission')
    # Catch replacement or earlier-file modification during complete-set hashing.
    for path, relative in paths:
        if _read_file(path, _identity(_artifact_info(path))) != actual[relative]:
            raise ValueError('Desktop MCP artifact changed during validation')
    if _identity(_directory(workspace)) != workspace_identity:
        raise ValueError('Desktop MCP workspace changed during validation')
    return result, actual


def worker_status(runner, executor='workbuddy'):
    _provider(executor)
    broker = getattr(runner, executor+'_broker', None)
    if broker is None:
        return {'online': False, 'ready': False, 'reason': '桌面派工连接器尚未启动'}
    if getattr(broker,'provider','workbuddy')!=executor:
        return {'online': False, 'ready': False, 'reason': '桌面派工连接器身份不匹配'}
    return broker.status()


def refresh_worker_registry(registry, config, runner):
    """Configured support and a waiting native chat are separate facts."""
    selected=[name for name in DESKTOP_PROVIDERS if config.get(name+'_transport') in ('mcp','disabled')]
    if not selected:return registry
    result = {name: dict(spec) for name, spec in registry.items()}
    for name in selected:
        if config.get(name+'_transport')=='disabled':
            spec=result.setdefault(name,{'name':name,'command':[],'detected':False})
            spec.update(enabled=False,transport='disabled',status='已停用')
            continue
        status=worker_status(runner,name)
        configured=name in config.get('enabled_executors',[])
        enabled=configured and status.get('ready') is True
        message=('桌面会话已就绪' if enabled else '桌面会话已连接，待启用或等待任务' if status.get('online')
                 else '请在 '+_provider(name)+' 启动专用派工会话')
        if status.get('active_run_id'):
            message='桌面会话正在执行任务'
        elif configured and name=='doubao' and status.get('online') and not enabled:
            if status.get('wait_phase')=='gap':
                message='豆包正在续等，下一段开始后可派工'
            elif status.get('wait_phase')=='closed' and status.get('wait_window_id'):
                message=('上项已提交，等待豆包会话继续接单'
                         if status.get('wait_end_reason')=='task_submitted'
                         else '豆包等待已结束，请重新连接并启动派工会话')
        spec=result.setdefault(name,{'name':name,'command':[]})
        spec.update(enabled=enabled,detected=True,transport='desktop_mcp',
            status=message)
    return result


def execute_desktop(runner, row, run_dir, *, executor='workbuddy'):
    label=_provider(executor)
    unavailable=WorkBuddyWorkerUnavailable if executor=='workbuddy' else DesktopWorkerUnavailable
    if runner.config.get(executor+'_transport')=='disabled':
        raise unavailable(label+' 已停用，不能派工。')
    run_dir = Path(run_dir)
    broker = getattr(runner, executor+'_broker', None)
    if not worker_status(runner,executor).get('ready'):
        raise unavailable('请先在已登录的 '+label+' 专用会话启动派工助手，等候任务后再分配。')
    settings = runner.execution_settings(row, run_dir, executor)
    if settings.get('model') or settings.get('effort'):
        raise unavailable(label+' 桌面连接使用客户端当前模型；请清空此智能体及任务的模型覆盖，在客户端会话中选择模型和思考强度。')
    binding = runner.workspace_binding(row, run_dir)
    mode = (binding or {}).get('mode') or row.get('workspace_choice', {}).get('mode', 'default')
    if mode not in ('default', 'new'):
        raise unavailable(label+' 桌面连接当前仅支持默认或新建工作区，请为本次任务选择一个独立目录。')
    workspace = runner.workspace_path(row, run_dir)
    run_id = row.get('current_run_id') or run_dir.name
    prompt = runner.prompt(row)
    (run_dir/'execution.prompt.txt').write_text(prompt, encoding='utf-8')
    from .execution_timeouts import snapshot as timeout_snapshot
    timeout = timeout_snapshot(row,run_dir,executor,runner.config,settings)['total_seconds']
    cancel = row.get('_cancel_event')
    if cancel is not None and cancel.is_set():
        raise unavailable('派工已取消，本次未提交给桌面连接器。')
    from .workbuddy_mcp import BridgeError
    try:
        broker.submit(run_id, workspace, prompt, workspace_mode=mode, timeout=timeout,
                      priority=int(row.get('priority') or 0))
    except BridgeError as error:
        if executor=='doubao' and error.reason=='worker_not_waiting':
            raise unavailable('豆包刚进入续等间隔或等待已结束，本次尚未派出；请待会话重新显示就绪后再分配。') from None
        raise
    terminal = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise unavailable('派工已取消，桌面连接器的任务读写权限已撤销。')
            state = broker.result(run_id)
            if state['state'] == 'submitted':
                terminal = True
                result, actual = _validated_submission(workspace, state)
                receipt = {'provider':executor,'transport': 'desktop_mcp', 'state': 'submitted', 'artifact_sha256': actual,
                           'model_source': 'native_session', 'scoped_tools_revoked': True}
                (run_dir/(executor+'-receipt.json')).write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
                (run_dir/'execution-resume.txt').write_text(label+' desktop MCP submission. Continue in a new dispatched task; the completed lease cannot be resumed.\n', encoding='utf-8')
                return dict(result, executor=executor, session_id=None, resume_available=False,
                            **{executor+'_receipt':receipt})
            if state['state'] in ('cancelled', 'expired', 'disconnected'):
                terminal = True
                raise unavailable(label+' 桌面派工中断，任务读写权限已撤销；请重新启动专用会话后重试。')
            if state['state'] not in ('queued', 'leased'):
                raise ValueError('Unknown desktop broker state')
            if time.monotonic() >= deadline:
                raise unavailable(label+' 桌面派工超时，任务读写权限已撤销。')
            time.sleep(0.1)
    finally:
        if not terminal:
            broker.cancel(run_id)


def execute_workbuddy(runner,row,run_dir):
    """Preserve existing WorkBuddy callers while sharing the audited execution path."""
    return execute_desktop(runner,row,run_dir,executor='workbuddy')
