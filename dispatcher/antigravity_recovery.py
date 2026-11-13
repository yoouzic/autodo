"""Fail-closed, read-only classification of completed Antigravity EOF runs.

This does not turn a vendor ERROR into SUCCESS. Its recovered report must still
pass independent verification and human review. Call only from the Antigravity
document execution path, after a normally collected process exit.
"""
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat

from .executors import parse_result_text


MAX_STREAM_BYTES = 4 * 1024 * 1024
MAX_EVENTS = 10000
MAX_ARTIFACTS = 32
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES = 16 * 1024 * 1024
_REPORT_FIELDS = {'status', 'summary', 'artifacts', 'checks', 'needs_input'}
_TOOLS = {'read_file', 'write_file', 'edit_file', 'finish'}
_STEPS = {'user_input', 'agent_response', 'tool', 'error_message', 'finish'}
_SESSION = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')
_EOF_ERRORS = {
    'API error (attempt 1): request failed: Post '
    '"https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse": EOF',
    'API error (attempt 1): stream reading error: unexpected EOF',
}
_WARNING = 'Antigravity 报告 EOF；已恢复完成产物，仍需独立验收和人工复审。'


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError('Non-finite JSON number')


def _has_failure(value):
    """Inspect structured failure flags, without interpreting document prose."""
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            for key, item in current.items():
                if key in ('error', 'is_error', 'denied_actions') and item:
                    return True
                if key in ('state', 'status') and isinstance(item, str) and item.upper() in (
                        'ERROR', 'FAILED', 'DENIED', 'CANCELLED', 'TIMEOUT', 'TIMED_OUT'):
                    return True
                if isinstance(item, (dict, list)):
                    pending.append(item)
        elif isinstance(current, list):
            pending.extend(current)
    return False


def _same_session(value, session):
    return 'conversation_id' not in value or value['conversation_id'] == session


def _is_reparse(path_stat):
    return bool(getattr(path_stat, 'st_file_attributes', 0) & 0x400)


def _regular_artifact(root, name):
    if not isinstance(name, str) or not name or len(name) > 1024:
        raise ValueError('Invalid artifact path')
    if any(char in name for char in '\\:<>"|?*') or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError('Invalid artifact path')
    relative = PurePosixPath(name)
    if (relative.is_absolute() or PureWindowsPath(name).drive or str(relative) != name
            or any(part in ('.', '..') or part.casefold() in ('.runtime', '.git')
                   or part.endswith((' ', '.'))
                   or re.match(r'(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', part)
                   for part in relative.parts)):
        raise ValueError('Unsafe artifact path')
    current = root
    for component in relative.parts:
        current = current / component
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            raise ValueError('Artifact traverses a link or reparse point')
    if not current.resolve(strict=True).is_relative_to(root):
        raise ValueError('Artifact escapes workspace')
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        raise ValueError('Artifact is not a nonempty regular file')
    if info.st_size > MAX_ARTIFACT_BYTES:
        raise ValueError('Artifact is too large')
    return current, info


def _file_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _workspace_root(workspace):
    original = Path(workspace)
    original_info = original.lstat()
    if stat.S_ISLNK(original_info.st_mode) or _is_reparse(original_info):
        raise ValueError('Workspace is a link')
    root = original.resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Workspace is not a directory')
    return root


def _validate_tool_path(root, parameters):
    if not isinstance(parameters, dict) or not isinstance(parameters.get('Path'), str):
        raise ValueError('Missing file tool path')
    value = parameters['Path']
    path = Path(value)
    if value.startswith(('\\\\', '//')):
        raise ValueError('Device and network paths are not recoverable')
    if path.is_absolute():
        name = path.relative_to(root).as_posix()
    else:
        name = value
    _regular_artifact(root, name)


def _artifact_hashes(workspace, names):
    if not names or len(names) > MAX_ARTIFACTS:
        raise ValueError('Invalid artifact count')
    root = _workspace_root(workspace)
    hashes = {}
    seen = set()
    total = 0
    for name in names:
        key = name.casefold()
        if key in seen:
            raise ValueError('Duplicate artifact')
        seen.add(key)
        path, before = _regular_artifact(root, name)
        if total + before.st_size > MAX_TOTAL_ARTIFACT_BYTES:
            raise ValueError('Artifact read budget exceeded')
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        flags |= getattr(os, 'O_NONBLOCK', 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, 'rb') as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != _file_identity(before):
                raise ValueError('Artifact changed before read')
            digest = hashlib.sha256()
            size = 0
            while True:
                block = source.read(min(65536, MAX_ARTIFACT_BYTES - size + 1))
                if not block:
                    break
                size += len(block)
                if size > MAX_ARTIFACT_BYTES or total + size > MAX_TOTAL_ARTIFACT_BYTES:
                    raise ValueError('Artifact read budget exceeded')
                digest.update(block)
            if size != before.st_size or _file_identity(os.fstat(source.fileno())) != _file_identity(before):
                raise ValueError('Artifact changed during read')
        _, after = _regular_artifact(root, name)
        if _file_identity(after) != _file_identity(before):
            raise ValueError('Artifact changed after read')
        total += size
        hashes[name] = digest.hexdigest()
    return hashes


def _classify(stdout, workspace):
    if not isinstance(stdout, str) or len(stdout) > MAX_STREAM_BYTES:
        return None
    encoded = stdout.encode('utf-8')
    if len(encoded) > MAX_STREAM_BYTES:
        return None
    lines = stdout.splitlines()
    if not lines or len(lines) > MAX_EVENTS:
        return None
    events = [json.loads(line, object_pairs_hook=_unique_object,
                         parse_constant=_invalid_constant) for line in lines]
    if not all(isinstance(event, dict) for event in events):
        return None
    kinds = [event.get('event') for event in events]
    if (kinds[0] != 'init' or kinds[-1] != 'result' or kinds.count('init') != 1
            or kinds.count('result') != 1 or any(kind != 'step_update' for kind in kinds[1:-1])):
        return None
    initial = events[0]
    session = initial.get('conversation_id')
    init = initial.get('init')
    terminal = events[-1].get('result')
    if (not isinstance(session, str) or not _SESSION.fullmatch(session)
            or not isinstance(init, dict) or init.get('model') != 'gemini-3.8-flash-medium'
            or not _same_session(init, session) or _has_failure(initial)
            or not isinstance(terminal, dict) or terminal.get('conversation_id') != session
            or not _same_session(events[-1], session)
            or terminal.get('status') != 'ERROR' or terminal.get('finished_reason') != 'error'
            or type(terminal.get('num_turns')) is not int or terminal['num_turns'] != 1
            or not isinstance(terminal.get('error'), str) or terminal['error'] not in _EOF_ERRORS):
        return None
    safe_terminal = {key: value for key, value in terminal.items() if key not in ('error', 'status')}
    if _has_failure(safe_terminal) or _has_failure({key: value for key, value in events[-1].items()
                                                 if key != 'result'}):
        return None
    canonical = terminal.get('structured_output')
    if not isinstance(canonical, dict) or set(canonical) != _REPORT_FIELDS:
        return None
    if 'response' in terminal:
        response = terminal['response']
        if (not isinstance(response, str) or json.loads(response, object_pairs_hook=_unique_object,
                parse_constant=_invalid_constant) != canonical):
            return None
    report = parse_result_text(canonical)
    if (report['status'] != 'complete' or report['needs_input'] != ''
            or not report['summary'].strip() or not report['checks'] or not report['artifacts']
            or any(not check.strip() for check in report['checks'])):
        return None

    latest = {}
    identities = {}
    tool_parameters = {}
    finishes = {}
    errors = []
    previous_index = -1
    root = _workspace_root(workspace)
    for position, event in enumerate(events[1:-1], 1):
        detail = event.get('step_update')
        if (not isinstance(detail, dict) or not _same_session(event, session)
                or not _same_session(detail, session) or _has_failure(event)):
            return None
        index, state, kind = detail.get('step_index'), detail.get('state'), detail.get('step_type')
        if (type(index) is not int or not 0 <= index <= MAX_EVENTS
                or index not in (previous_index, previous_index + 1)
                or not isinstance(kind, str) or kind not in _STEPS or state not in ('ACTIVE', 'DONE')):
            return None
        previous_index = index
        if index in latest and latest[index]['state'] == 'DONE' and state != 'DONE':
            return None
        identity = kind
        info = detail.get('tool_info')
        if kind in ('tool', 'finish'):
            if info is not None and not isinstance(info, dict):
                return None
            names = [value for value in (detail.get('tool_name'), (info or {}).get('name'))
                     if value is not None]
            if kind == 'finish':
                names.append('finish')
            if not names or any(not isinstance(name, str) or name not in _TOOLS for name in names):
                return None
            if len(set(names)) != 1:
                return None
            tool = names[0]
            identity = 'tool:' + tool
            if tool != 'finish':
                parameters = (info or {}).get('parameters')
                _validate_tool_path(root, parameters)
                if index in tool_parameters and tool_parameters[index] != parameters:
                    return None
                tool_parameters[index] = parameters
            if tool == 'finish':
                parameters = (info or {}).get('parameters')
                if info is not None and 'parameters' in info:
                    if not isinstance(parameters, dict) or parameters != canonical:
                        return None
                    finishes[index] = parameters
                elif index not in finishes and kind == 'tool':
                    return None
        elif info is not None or 'tool_name' in detail:
            return None
        if kind == 'error_message':
            if (state != 'DONE'
                    or set(event) - {'event', 'conversation_id', 'step_update'}
                    or set(detail) - {'conversation_id', 'step_index', 'state', 'step_type',
                                      'duration_seconds'}):
                return None
            duration = detail.get('duration_seconds', 0)
            if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
                return None
            errors.append((index, position))
        if index in identities and identities[index] != identity:
            return None
        identities[index] = identity
        latest[index] = detail

    if not latest or any(detail['state'] != 'DONE' for detail in latest.values()):
        return None
    user_input_indices = [index for index, identity in identities.items() if identity == 'user_input']
    if user_input_indices != [0]:
        return None
    finish_indices = [index for index, identity in identities.items() if identity == 'tool:finish']
    if len(errors) != 1 or len(finish_indices) != 1:
        return None
    finish_index = finish_indices[0]
    error_index, error_position = errors[0]
    if (finish_index not in finishes or finish_index != previous_index
            or error_index >= finish_index or error_position >= len(events) - 2):
        return None
    hashes = _artifact_hashes(workspace, report['artifacts'])
    return {
        'status': 'recovered', 'reported_status': 'complete', 'executor': 'antigravity',
        'session_id': session, 'summary': report['summary'], 'artifacts': report['artifacts'][:],
        'checks': report['checks'][:], 'needs_input': '', 'vendor_status': 'ERROR',
        'runtime_warning': {'kind': 'completed_eof', 'message': _WARNING},
        'recovered_artifact_sha256': hashes,
        'completion_evidence': {
            'error_step_index': error_index, 'finish_step_index': finish_index,
            'error_event_index': error_position, 'finish_done_event_index': len(events) - 2,
            'event_count': len(events), 'stream_sha256': hashlib.sha256(encoded).hexdigest(),
            'artifact_sha256': hashes.copy(),
        },
    }


def recover_completed_eof(stdout, workspace, *, process_exit_code, stderr='',
                          kind='document', require_human_review=True):
    """Return a bounded recovered report, or None when any evidence is missing.

    No diagnostics or other files are written. Provider errors and arbitrary
    vendor metadata never enter the returned report. Exit status must be supplied
    by the process collector, never inferred from the provider's JSONL output.
    """
    if (type(process_exit_code) is not int or process_exit_code != 0 or stderr != ''
            or kind != 'document' or require_human_review is not True):
        return None
    try:
        return _classify(stdout, workspace)
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        return None
