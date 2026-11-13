"""Bounded ZCode NDJSON session client; credentials stay with the vendor runtime.

Runs inside the dispatcher's existing sandbox. Only session model controls and
five file tools are admitted. Raw protocol messages and stderr are never printed.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid

FILE_TOOLS = ['Read', 'Write', 'Edit', 'Glob', 'Grep']
DENIED_TOOLS = ['Bash', 'Shell', 'PowerShell', 'NodeREPL', 'WebSearch', 'WebFetch', 'Task', 'Agent', 'js']
MAX_LINE = 16 * 1024 * 1024
MAX_RESPONSE = 2 * 1024 * 1024
MAX_MESSAGES = 20000


class ProtocolError(RuntimeError):
    """Errors are fixed codes; never include untrusted vendor diagnostics."""


def _text(value, maximum=256):
    return isinstance(value, str) and 0 < len(value) <= maximum and not any(ord(c) < 32 for c in value)


def _read_json(path):
    source = Path(path)
    if source.stat().st_size > MAX_RESPONSE:
        raise ProtocolError('request_too_large')
    return json.loads(source.read_text(encoding='utf-8'))


def model_catalog(snapshot):
    """Project only model identifiers and supported levels; no provider config."""
    available = snapshot.get('settings', {}).get('model', {}).get('available', [])
    if not isinstance(available, list) or len(available) > 1000:
        raise ProtocolError('invalid_catalog')
    result, seen = [], set()
    for entry in available:
        ref = entry.get('ref', {}) if isinstance(entry, dict) else {}
        provider, model = ref.get('providerId'), ref.get('modelId')
        if not _text(provider) or not _text(model):
            raise ProtocolError('invalid_catalog')
        key = provider + '/' + model
        if key in seen:
            raise ProtocolError('duplicate_catalog_model')
        seen.add(key)
        levels = (entry.get('reasoning') or {}).get('levels', [])
        if not isinstance(levels, list):
            raise ProtocolError('invalid_catalog')
        efforts = [level.get('value') for level in levels if isinstance(level, dict)]
        if len(efforts) != len(levels) or any(not _text(level, 40) for level in efforts):
            raise ProtocolError('invalid_catalog')
        default = (entry.get('reasoning') or {}).get('defaultLevel')
        result.append({'id': key, 'provider': provider, 'model': model,
                       'label': entry.get('label') if _text(entry.get('label')) else model,
                       'efforts': efforts, 'default_effort': default if default in efforts else None,
                       'available': not bool(entry.get('disabledReason'))})
    return result


class Connection:
    def __init__(self, command, workspace, timeout):
        self.deadline = time.monotonic() + timeout
        self.messages = queue.Queue(maxsize=128)
        self.sequence = 0
        self.received = 0
        self.fatal = None
        self.process = subprocess.Popen(command, cwd=workspace, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
            creationflags=(getattr(subprocess, 'CREATE_NO_WINDOW', 0) |
                           getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)) if os.name == 'nt' else 0,
            start_new_session=os.name != 'nt')
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.stderr_reader = threading.Thread(target=self._discard_stderr, daemon=True)
        self.reader.start()
        self.stderr_reader.start()

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(MAX_LINE + 1)
                if not line:
                    self.messages.put(None)
                    return
                if len(line) > MAX_LINE or not line.endswith(b'\n'):
                    self.messages.put(ProtocolError('invalid_protocol_line'))
                    return
                try:
                    item = json.loads(line)
                except (ValueError, UnicodeError):
                    self.messages.put(ProtocolError('invalid_protocol_json'))
                    return
                self.messages.put(item)
        except (OSError, ValueError):
            self.messages.put(ProtocolError('protocol_read_failed'))

    def _discard_stderr(self):
        try:
            while self.process.stderr.read(65536):
                pass
        except (OSError, ValueError):
            pass

    def send(self, value):
        try:
            self.process.stdin.write((json.dumps(value, ensure_ascii=False) + '\n').encode('utf-8'))
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            raise ProtocolError('protocol_closed') from None

    def _receive(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ProtocolError('timeout')
        try:
            item = self.messages.get(timeout=remaining)
        except queue.Empty:
            raise ProtocolError('timeout') from None
        self.received += 1
        if self.received > MAX_MESSAGES:
            raise ProtocolError('protocol_limit')
        if item is None:
            raise ProtocolError('protocol_closed')
        if isinstance(item, Exception):
            raise item
        if not isinstance(item, dict):
            raise ProtocolError('invalid_protocol_message')
        return item

    def _server_message(self, item):
        method = item.get('method')
        if 'id' in item and method:
            if method == 'session/requestRuntimePreferences':
                self.send({'id': item['id'], 'result': {
                    'additionalDirectories': [], 'nativeSearchEnhancementsEnabled': False,
                    'toolSearchMode': 'off', 'subagentRuntimeConfigEnabled': False,
                    'memoryEnabled': False, 'askUserQuestionAutoResolutionEnabled': False}})
                return
            self.send({'id': item['id'], 'error': {'code': -32601,
                       'message': 'Dispatcher does not permit this interaction'}})
            self.fatal = 'interaction_denied'
        if method == 'state.updated' and (item.get('params') or {}).get('reason') == 'prompt_failed':
            self.fatal = 'turn_failed'
        if self.fatal:
            raise ProtocolError(self.fatal)

    def call(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self.send({'id': request_id, 'method': method, 'params': params})
        while True:
            item = self._receive()
            if 'method' in item:
                self._server_message(item)
                continue
            if item.get('id') != request_id:
                raise ProtocolError('unexpected_protocol_response')
            if 'error' in item:
                code = (item.get('error') or {}).get('code')
                suffix = str(code) if type(code) is int else 'unknown'
                phase = method.replace('/', '_')
                raise ProtocolError('vendor_rpc_error_' + phase + '_' + suffix)
            if 'result' not in item:
                raise ProtocolError('missing_protocol_result')
            return item['result']

    def close(self, require_success=False):
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            code = self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == 'nt':
                subprocess.run(['taskkill.exe', '/PID', str(self.process.pid), '/T', '/F'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            else:
                os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)
            code = -1
        finally:
            for stream in (self.process.stdout, self.process.stderr):
                stream.close()
        if require_success and code != 0:
            raise ProtocolError('vendor_process_exit')


def _selection(snapshot):
    return snapshot.get('settings', {}).get('model', {}).get('current') or {}


def _public_selection(snapshot):
    selection = _selection(snapshot)
    result = {key: selection[key] for key in ('providerId', 'modelId')
              if _text(selection.get(key))}
    level = (selection.get('options') or {}).get('reasoningLevel')
    if _text(level, 40):
        result['options'] = {'reasoningLevel': level}
    return result


def _check_configuration(snapshot, selected, effort=None):
    current = _selection(snapshot)
    actual = snapshot.get('settings', {}).get('thoughtLevel', {}).get('current')
    if current.get('providerId') != selected['provider'] or current.get('modelId') != selected['model']:
        raise ProtocolError('configuration_mismatch')
    if effort is not None and actual != effort:
        raise ProtocolError('configuration_mismatch')
    return actual


def _choose_model(catalog, value, current):
    if value:
        matches = [m for m in catalog if m['available'] and m['id'] == value]
        if not matches:
            matches = [m for m in catalog if m['available'] and m['model'] == value]
    else:
        matches = [m for m in catalog if m['available'] and
                   m['provider'] == current.get('providerId') and m['model'] == current.get('modelId')]
    if not matches:
        raise ProtocolError('model_unavailable')
    if len(matches) != 1:
        raise ProtocolError('model_ambiguous')
    return matches[0]


def _response(snapshot, selected, effort):
    _check_configuration(snapshot, selected, effort)
    projection, runtime = snapshot.get('projection') or {}, snapshot.get('runtime') or {}
    if (runtime.get('activeTurnId') or runtime.get('pendingRequestIds') or
            projection.get('pendingPermissions') or projection.get('activeToolCalls') or
            projection.get('backgroundJobs') or projection.get('lastError') or
            projection.get('status') not in ('idle', 'completed')):
        raise ProtocolError('incomplete_turn')
    assistants = []
    for message in snapshot.get('messages', []):
        info = message.get('info') or {}
        if info.get('error'):
            raise ProtocolError('message_error')
        for part in message.get('parts', []):
            if part.get('type') == 'tool':
                if part.get('tool') not in FILE_TOOLS or (part.get('state') or {}).get('status') != 'completed':
                    raise ProtocolError('tool_not_completed')
        if info.get('role') == 'assistant':
            assistants.append(message)
    if not assistants:
        raise ProtocolError('missing_assistant_response')
    final = assistants[-1]
    if final['info'].get('finish') not in ('stop', 'end_turn'):
        raise ProtocolError('incomplete_assistant_response')
    reported = final['info'].get('model') or {}
    if reported.get('providerId') != selected['provider'] or reported.get('modelId') != selected['model']:
        raise ProtocolError('response_model_mismatch')
    text = ''.join(p.get('text', '') for p in final.get('parts', [])
                   if p.get('type') == 'text' and not p.get('ignored') and not p.get('synthetic'))
    if not text.strip() or len(text.encode('utf-8')) > MAX_RESPONSE:
        raise ProtocolError('invalid_assistant_response')
    return text


def run_session(command, request, *, append_server_args=True):
    if (not isinstance(command, list) or not command or
            any(not isinstance(arg, str) or not arg or '\x00' in arg for arg in command)):
        raise ProtocolError('invalid_command')
    if not isinstance(request, dict) or set(request) - {'workspace', 'prompt', 'model', 'effort', 'timeout_seconds', 'catalog_only'}:
        raise ProtocolError('invalid_request')
    workspace = Path(request.get('workspace', '')).resolve()
    if not workspace.is_dir():
        raise ProtocolError('invalid_workspace')
    timeout = request.get('timeout_seconds', 600)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 86400:
        raise ProtocolError('invalid_timeout')
    for key in ('model', 'effort'):
        value = request.get(key)
        if value not in (None, '') and not _text(value):
            raise ProtocolError('invalid_' + key)
    if not request.get('catalog_only') and (not isinstance(request.get('prompt'), str) or not request['prompt'].strip() or len(request['prompt'].encode('utf-8')) > MAX_RESPONSE):
        raise ProtocolError('invalid_prompt')
    argv = list(command) + (['app-server'] if append_server_args else [])
    connection = Connection(argv, workspace, timeout)
    successful = False
    try:
        snapshot = connection.call('session/create', {
            'workspace': {'workspacePath': str(workspace), 'workspaceKey': str(workspace)},
            'mode': 'edit', 'persistence': 'deferred', 'titleGenerationEnabled': False,
            'toolAllowlist': FILE_TOOLS, 'toolDenylist': DENIED_TOOLS,
            'dynamicWorkflowEnabled': False, 'dynamicWorkflowMode': 'disabled', 'offPeakToolEnabled': False})
        sid = snapshot.get('session', {}).get('sessionId')
        if not _text(sid):
            raise ProtocolError('invalid_session')
        catalog = model_catalog(snapshot)
        if request.get('catalog_only'):
            connection.call('session/close', {'sessionId': sid})
            successful = True
            return {'models': catalog, 'current': _public_selection(snapshot)}
        selected = _choose_model(catalog, request.get('model'), _selection(snapshot))
        effort = request.get('effort') or None
        if effort and effort not in selected['efforts']:
            raise ProtocolError('effort_unavailable')
        model_ref = {'providerId': selected['provider'], 'modelId': selected['model']}
        # Registry models with reasoning require the option in setModel itself;
        # a later setThoughtLevel call cannot repair an invalid model selection.
        if selected['efforts']:
            current = _selection(snapshot)
            previous = (current.get('options') or {}).get('reasoningLevel')
            same_model = current.get('providerId') == selected['provider'] and current.get('modelId') == selected['model']
            level = effort or (previous if same_model and previous in selected['efforts'] else selected['default_effort'])
            if level not in selected['efforts']:
                raise ProtocolError('effort_default_unavailable')
            model_ref['options'] = {'reasoningLevel': level}
        snapshot = connection.call('session/setModel', {'sessionId': sid, 'model': model_ref,
                                                       'persistAsWorkspaceLastUsed': False})
        _check_configuration(snapshot, selected, (model_ref.get('options') or {}).get('reasoningLevel'))
        if effort:
            snapshot = connection.call('session/setThoughtLevel', {'sessionId': sid, 'thoughtLevel': effort,
                                                                   'persistAsWorkspaceLastUsed': False})
        actual_effort = _check_configuration(snapshot, selected, effort)
        cursor = snapshot.get('runtime', {}).get('eventSeq', 0)
        accepted = connection.call('session/send', {'sessionId': sid, 'content': request['prompt'],
            'inputId': 'dispatcher-' + uuid.uuid4().hex, 'expectedRevision': snapshot['runtime']['stateRevision'],
            'toolDenylist': DENIED_TOOLS})
        if accepted.get('accepted') is not True or accepted.get('sessionId') != sid:
            raise ProtocolError('prompt_not_accepted')
        turn, completed, tool_audit = None, False, []
        while not completed:
            data = connection.call('session/events', {'sessionId': sid, 'afterSeq': cursor, 'limit': 1000})
            events = data.get('events')
            if not isinstance(events, list):
                raise ProtocolError('invalid_events')
            for event in events:
                if event.get('sessionId') != sid or type(event.get('seq')) is not int or event['seq'] <= cursor:
                    raise ProtocolError('invalid_event_sequence')
                cursor = event['seq']
                kind, payload = event.get('type'), event.get('payload') or {}
                if kind == 'turn.started':
                    if turn is not None:
                        raise ProtocolError('unexpected_second_turn')
                    turn = event.get('turnId')
                    if not _text(turn):
                        raise ProtocolError('missing_turn_id')
                elif kind == 'turn.completed':
                    if not turn or event.get('turnId') != turn:
                        raise ProtocolError('unexpected_terminal_turn')
                    if payload.get('resultType') != 'success':
                        raise ProtocolError('unsuccessful_terminal_result')
                    completed = True
                elif kind in ('turn.failed', 'session.closed', 'permission.requested', 'userInput.requested'):
                    raise ProtocolError('turn_failed')
                elif kind == 'permission.resolved' and payload.get('decision') in ('deny', 'denied'):
                    raise ProtocolError('permission_denied')
                elif kind == 'tool.updated':
                    name = payload.get('toolName') or payload.get('tool')
                    tool_result = payload.get('result')
                    nested_failure = isinstance(tool_result, dict) and tool_result.get('success') is False
                    if (name and name not in FILE_TOOLS) or payload.get('kind') == 'error' or payload.get('error') or payload.get('isError') or nested_failure:
                        raise ProtocolError('tool_failed_or_disallowed')
                    if name and payload.get('kind') in ('scheduled', 'started', 'progress', 'result'):
                        tool_audit.append({'tool': name, 'phase': payload['kind']})
            if not completed:
                time.sleep(min(.25, max(0, connection.deadline - time.monotonic())))
        snapshot = connection.call('session/read', {'sessionId': sid, 'contentProfile': 'full'})
        response = _response(snapshot, selected, actual_effort)
        connection.call('session/close', {'sessionId': sid})
        successful = True
        return {'sessionId': sid, 'response': response, 'effective_configuration': {
            'provider': selected['provider'], 'model': selected['model'], 'effort': actual_effort},
            'audit': {'terminal_result_type': 'success', 'tools': tool_audit}}
    finally:
        connection.close(require_success=successful)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--command-json', required=True)
    parser.add_argument('--request', required=True)
    parser.add_argument('--catalog', action='store_true')
    args = parser.parse_args()
    try:
        command = _read_json(args.command_json)
        request = _read_json(args.request)
        if args.catalog:
            request['catalog_only'] = True
        result = run_session(command, request)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except ProtocolError as exc:
        print(json.dumps({'error': str(exc), 'response': ''}))
        return 1
    except (OSError, ValueError, TypeError, KeyError, AttributeError, KeyboardInterrupt):
        print(json.dumps({'error': 'zcode_bridge_failed', 'response': ''}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
