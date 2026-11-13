"""Restricted WorkBuddy CLI invocation and strict terminal-result validation.

Authentication stays in vendor-owned stores; .workbuddy selects CLI configuration,
not every credential/cache path. Only the explicit runtime is returned as an
additional writable sandbox root. This module never starts a process, reads
credentials, or marks a session as resumable.
"""
from __future__ import annotations

import json
import re
from pathlib import Path, PureWindowsPath

from .workspace_copy import _check_components, _directory


FILE_TOOLS = 'Read,Write,Edit,Glob,Grep'
EFFORTS = frozenset({'minimal', 'low', 'medium', 'high', 'xhigh', 'max'})
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_EVENTS = 10000
LOGIN_REQUIRED_MESSAGE = 'WorkBuddy CLI 需要登录，请先通过已验证的官方登录入口完成登录。'
PROTOCOL_ERROR_MESSAGE = 'WorkBuddy CLI 未返回完整、唯一且成功的任务结果。'
_MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z')
_SESSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9_:-]{0,255}\Z')
_REPORT_KEYS = frozenset({'status', 'summary', 'artifacts', 'checks', 'needs_input'})


class WorkBuddyLoginRequired(ValueError):
    def __init__(self):
        super().__init__(LOGIN_REQUIRED_MESSAGE)


def _local_path(value):
    if not isinstance(value, (str, Path)) or '\x00' in str(value):
        raise ValueError('WorkBuddy requires local absolute paths')
    path = Path(value)
    windows = PureWindowsPath(str(value))
    if (not path.is_absolute() or str(value).startswith(('\\\\', '//')) or
            windows.drive.startswith('\\\\') or ':' in str(value)[len(windows.drive):]):
        raise ValueError('WorkBuddy requires local absolute paths')
    _check_components(path)
    return path.resolve()


def build_invocation(command, workspace, run_dir, prompt, settings, runtime, *,
                     schema, max_turns=20, home=None):
    """Return (argv, child env overrides, stdin, additional writable roots)."""
    if (not isinstance(command, (list, tuple)) or not command or
            any(not isinstance(arg, str) or not arg or '\x00' in arg or arg.startswith('-')
                for arg in command)):
        raise ValueError('WorkBuddy command must contain only the executable and entrypoint')
    if not isinstance(prompt, str) or not prompt.strip() or '\x00' in prompt or len(prompt.encode('utf-8')) > 1024 * 1024:
        raise ValueError('WorkBuddy prompt must be bounded nonempty text')
    if type(max_turns) is not int or not 1 <= max_turns <= 30:
        raise ValueError('WorkBuddy max_turns must be between 1 and 30')
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise ValueError('WorkBuddy settings must be an object')
    model = settings.get('cli_model') or settings.get('model')
    effort = settings.get('effort')
    if model is not None and (not isinstance(model, str) or not _MODEL.fullmatch(model)):
        raise ValueError('WorkBuddy model ID is invalid')
    if effort is not None and (not isinstance(effort, str) or effort not in EFFORTS):
        raise ValueError('WorkBuddy effort is invalid')
    if not isinstance(schema, dict):
        raise ValueError('WorkBuddy requires a JSON Schema object')
    try:
        encoded_schema = json.dumps(schema, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise ValueError('WorkBuddy JSON Schema is invalid') from None
    if len(encoded_schema.encode('utf-8')) > 16 * 1024:
        raise ValueError('WorkBuddy JSON Schema exceeds the argument limit')
    workspace, run_dir, runtime = map(_local_path, (workspace, run_dir, runtime))
    _directory(workspace)
    _directory(run_dir)
    default_runtime = workspace == run_dir / 'workspace' and runtime == workspace / '.runtime'
    if runtime != run_dir / 'runtime' and not default_runtime:
        raise ValueError('WorkBuddy runtime must belong to this run')
    runtime.mkdir(exist_ok=True)
    _directory(runtime)
    profile = Path(home) if home is not None else Path.home()
    profile = _local_path(profile)
    env = {
        'CI': '1',
        'CODEBUDDY_CONFIG_DIR': str(profile / '.workbuddy'),
        'CODEBUDDY_PROJECT_CONFIG_DIR': str(runtime / '.codebuddy'),
        'CODEBUDDY_DISABLE_COMPILE_CACHE': '1',
        'CODEBUDDY_DISABLE_CRON': '1',
        'CODEBUDDY_CODE_DISABLE_BACKGROUND_TASKS': '1',
        'CODEBUDDY_CODE_DISABLE_AUTO_MEMORY': '1',
        'CODEBUDDY_DISABLE_AUTO_MEMORY': '1',
        'CODEBUDDY_MEMORY_ENABLED': 'false',
        'CODEBUDDY_CODE_ENABLE_TELEMETRY': 'false',
    }
    argv = list(command) + [
        '--print', '--input-format', 'text', '--output-format', 'stream-json',
        '--json-schema', encoded_schema,
        '--tools', FILE_TOOLS, '--allowedTools', FILE_TOOLS,
        '--permission-mode', 'dontAsk',
        '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
        # In the installed 2.156 loader an empty value falls back to all scopes.
        # A nonempty unselected scope normalizes to an empty source list.
        '--setting-sources', 'none', '--settings', '{"disableAllHooks":true,"autoUpdates":false}',
        '--no-session-persistence', '--max-turns', str(max_turns),
    ]
    if model is not None:
        argv.extend(['--model', model])
    if effort is not None:
        argv.extend(['--effort', effort])
    return argv, env, prompt, [runtime]


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON object key')
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError('Non-JSON numeric constant')


def _json(text):
    return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)


def _events(stdout):
    try:
        parsed = _json(stdout)
    except json.JSONDecodeError:
        lines = [line for line in stdout.splitlines() if line.strip()]
        if not lines or len(lines) > MAX_EVENTS:
            raise ValueError('Invalid event stream')
        events = [_json(line) for line in lines]
    else:
        events = [parsed]
    if not all(isinstance(event, dict) for event in events):
        raise ValueError('Invalid event envelope')
    return events


def _has_error(value):
    if isinstance(value, dict):
        subtype = value.get('subtype')
        if (value.get('type') == 'error' or value.get('is_error') is True or
                isinstance(subtype, str) and subtype.startswith('error') or
                value.get('error') or value.get('errors') or value.get('permission_denials')):
            return True
        return any(_has_error(child) for child in value.values())
    return isinstance(value, list) and any(_has_error(child) for child in value)


def _business_report(value):
    if not isinstance(value, dict) or set(value) != _REPORT_KEYS:
        raise ValueError('Business report does not match schema')
    if value.get('status') not in {'complete', 'needs_input', 'failed'}:
        raise ValueError('Invalid business status')
    if any(not isinstance(value[key], str) for key in ('status', 'summary', 'needs_input')):
        raise ValueError('Invalid business text')
    if any(not isinstance(value[key], list) or any(not isinstance(item, str) for item in value[key])
           for key in ('artifacts', 'checks')):
        raise ValueError('Invalid business arrays')
    return value


def _noncompletion_validation_events(events):
    """Separate explicit tool failures from transport/protocol failures.

    Only for a terminal business report that is NOT complete. Originals remain
    untouched in the audit log and the returned report records the error count.
    """
    validation=[];count=0
    for event in events:
        message=event.get('message')
        if event.get('type')!='user' or not isinstance(message,dict) or not isinstance(message.get('content'),list):
            validation.append(event);continue
        content=[]
        for item in message['content']:
            if isinstance(item,dict) and item.get('type')=='tool_result' and item.get('is_error') is True:
                # rawResponse metadata may repeat the same failure flags.
                # Treat the observed failed tool result as one atomic event,
                # not as independent transport failures inside its payload.
                count+=1;content.append({'type':'tool_result','observed_failure':True})
            else:content.append(item)
        validation.append(dict(event,message=dict(message,content=content)))
    return validation,count


def normalize_result(stdout, *, stderr='', exit_code=0, allow_noncompletion_tool_errors=False, allow_answer=False):
    """Require one complete, successful terminal; never expose raw vendor errors."""
    if not isinstance(stdout, str):
        raise ValueError(PROTOCOL_ERROR_MESSAGE)
    if not stdout.strip():
        diagnostic = stderr[:65536].casefold() if isinstance(stderr, str) else ''
        if 'authentication required' in diagnostic or 'please use /login' in diagnostic:
            raise WorkBuddyLoginRequired()
        raise ValueError(PROTOCOL_ERROR_MESSAGE)
    try:
        if type(exit_code) is not int or exit_code != 0 or len(stdout.encode('utf-8')) > MAX_OUTPUT_BYTES:
            raise ValueError('Process or output boundary failed')
        events = _events(stdout)
        terminals = [event for event in events if event.get('type') == 'result']
        if len(terminals) != 1 or events[-1] is not terminals[0]:
            raise ValueError('Missing or conflicting terminal evidence')
        terminal = terminals[0]
        if (terminal.get('subtype') != 'success' or terminal.get('is_error') is not False or
                terminal.get('permission_denials', []) != []):
            raise ValueError('Terminal did not report clean success')
        canonical = terminal.get('structured_output')
        if canonical is None:
            if not isinstance(terminal.get('result'), str):
                raise ValueError('Missing business report')
            canonical = _json(terminal['result'])
        if allow_answer and isinstance(canonical, dict) and 'protocol_version' in canonical:
            from .result_protocol import validate_answer
            report = validate_answer(canonical, strict=True)
        else:
            report = _business_report(canonical)
        tool_error_count=0
        if _has_error(events):
            if not allow_noncompletion_tool_errors or report['status'] not in ('needs_input','failed'):
                raise ValueError('Execution errors prevent a completion claim')
            validation,tool_error_count=_noncompletion_validation_events(events)
            if not tool_error_count or _has_error(validation):
                raise ValueError('Transport/protocol errors remain')
        session_id = terminal.get('session_id')
        if session_id is not None and (not isinstance(session_id, str) or not _SESSION.fullmatch(session_id)):
            raise ValueError('Invalid diagnostic session ID')
        result=dict(report, executor='workbuddy', session_id=session_id, resume_available=False)
        if tool_error_count:result['tool_error_count']=tool_error_count
        return result
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        raise ValueError(PROTOCOL_ERROR_MESSAGE) from None
