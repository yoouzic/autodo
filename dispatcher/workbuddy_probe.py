"""Fixed local setup probe only; not an arbitrary host execution interface.

Private HMAC inbox: no HTTP admin endpoint, worker credentials or user task-store
access. A daemon epoch accepts one request at a time; only a fixed new workspace
and fixed file operation are permitted. Lookups never create state.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import stat
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

from .workspace_copy import _check_components, _directory, _identity, _no_link, _read_file, _safe_path
from .workbuddy_mcp import _decode, _json, _pins, _private_acl, TEXT_FRAME_PREFIX, TEXT_FRAME_SUFFIX
from .mcp_namespace import mcp_namespace


_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
_HEX32 = re.compile(r'[0-9a-f]{32}\Z')
_LIMIT = 64 * 1024
_TERMINAL = {'passed', 'failed'}
_REQUEST_KEYS = {'version', 'epoch', 'request_id', 'operation', 'expires_at', 'signature'}
_END_REASONS = {'idle_continue', 'cancelled', 'gap_expired', 'window_expired', 'segment_limit',
                'task_leased', 'task_submitted', 'disconnected', 'heartbeat_timeout',
                'idle_timeout', 'request_cancelled', 'shutdown', 'connection_closed', 'not_started'}
_SAFE_FAILURE_REASONS = {'metadata_unavailable','metadata_too_large','unsafe_metadata','daemon_changed',
    'waiting_identity_changed','waiting_session_changed','wait_window_closed','invalid_wait_evidence',
    'wait_gap_invalid','worker_offline','claim_wait_mismatch','invalid_report'}
_PROMPT = f'''这是派工台的固定隔离连通性验收。只使用 todo-dispatcher 连接器提供的 read_file、write_file、publish_result 工具；不得使用原生文件工具、命令、网络或其他连接器，也不得操作其他任务。
先 read_file 读取虚拟工作区 seed.txt，必须精确为 KEEP 后跟一个换行。保持 seed.txt 不变。
然后 write_file 创建 probe.txt，必须显式传 text_format="framed"。text 必须用两个固定 ASCII 标记包住正文，正文精确为 READY 后跟一个换行；标记外不要添加任何内容。
写入参数的 JSON 示例（将换行作为字符串内的换行，不能写成反斜杠和字母 n 两个正文字符）：{{"path":"probe.txt","text_format":"framed","text":"{TEXT_FRAME_PREFIX}READY\\n{TEXT_FRAME_SUFFIX}"}}。另外传本次 lease_nonce。
工具只移除这两个固定外层标记，保留正文尾部换行；不要用 plain，也不要删除正文尾部换行。再 read_file 回读 probe.txt，必须确认内容精确为 READY 后跟一个换行。
最后 publish_result 提交 status=complete，summary=固定文件验收完成，artifacts=["probe.txt"]，checks=["seed.txt保持KEEP，probe.txt回读READY"]，needs_input=""。所有工具必须使用本次领取的 lease_nonce。
提交后再调用 wait_for_task 等待下一项；收到 idle_timeout 或连接失效就停止，不自行循环重试，不创建定时任务。'''


class ProbeError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__('WorkBuddy setup probe: ' + reason)


def _read(path):
    try:
        _check_components(path)
        info = path.lstat()
        _no_link(info)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= _LIMIT:
            raise ValueError()
        with path.open('rb') as stream:
            if _identity(os.fstat(stream.fileno())) != _identity(info): raise ValueError()
            raw = stream.read(_LIMIT + 1)
            if len(raw) > _LIMIT or _identity(os.fstat(stream.fileno())) != _identity(info): raise ValueError()
        _check_components(path)
        if _identity(path.lstat()) != _identity(info): raise ValueError()
        value = _decode(raw)
        if not isinstance(value, dict): raise ValueError()
        return value
    except (OSError, ValueError, TypeError, RecursionError):
        raise ProbeError('metadata_unavailable') from None


def _atomic(path, value):
    raw = _json(value).encode('utf-8')
    if len(raw) > _LIMIT: raise ProbeError('metadata_too_large')
    with _pins(path.parent):
        _check_components(path)
        if path.exists() and path.lstat().st_nlink != 1: raise ProbeError('unsafe_metadata')
        temporary = path.parent / ('probe-' + secrets.token_hex(12) + '.tmp')
        with temporary.open('xb') as stream:
            _private_acl(temporary)
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)


def _signature(value, token):
    payload = {key: item for key, item in value.items() if key != 'signature'}
    return hmac.new(bytes.fromhex(token), json.dumps(payload, sort_keys=True,
        ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8'), hashlib.sha256).hexdigest()


def _sign(value, token):
    return dict(value, signature=_signature(value, token))


def _valid_signature(value, control):
    candidate = value.get('signature')
    return (value.get('epoch') == control['epoch'] and value.get('version') == control['version'] and
            value.get('provider', 'workbuddy') == control.get('provider', 'workbuddy') and isinstance(candidate, str) and
            bool(_HEX64.fullmatch(candidate)) and
            hmac.compare_digest(candidate, _signature(value, control['host_token'])))


def _control(root, provider='workbuddy'):
    namespace = mcp_namespace(provider)
    path = Path(root) / 'data' / namespace.probe_directory
    value = _read(path / 'control.json')
    keys = {'version', 'epoch', 'host_token'}
    legacy = provider == 'workbuddy' and set(value) == keys and value.get('version') == 1
    current = (set(value) == keys | {'provider'} and value.get('version') == 2 and value.get('provider') == provider)
    if (not (legacy or current) or type(value['version']) is not int or
            not isinstance(value['epoch'], str) or not _HEX64.fullmatch(value['epoch']) or
            not isinstance(value['host_token'], str) or not _HEX64.fullmatch(value['host_token'])):
        raise ProbeError('invalid_control')
    return path, value


@contextmanager
def _request_lock(directory):
    descriptor = None
    locked = False
    try:
        with _pins(directory):
            path = directory / 'request.lock'
            _check_components(path)
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            info = os.fstat(descriptor)
            _no_link(info)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1: raise ValueError()
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        yield
    except (OSError, ValueError) as error:
        if isinstance(error, ProbeError): raise
        raise ProbeError('probe_busy_or_lock_unavailable') from None
    finally:
        if descriptor is not None:
            if locked:
                if os.name == 'nt':
                    import msvcrt
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def request_probe(root, *, provider='workbuddy'):
    """Request the fixed isolated probe; no caller prompt/path/settings allowed."""
    directory, control = _control(root, provider)
    with _request_lock(directory):
        # Freeze the daemon identity again after winning the writer lock.
        _, current = _control(root, provider)
        if current != control: raise ProbeError('daemon_changed')
        request_path = directory / 'request.json'
        if request_path.exists():
            previous = _read(request_path)
            if previous.get('epoch') == control['epoch']:
                if not _valid_request(previous, control): raise ProbeError('invalid_existing_request')
                receipt = get_probe_result(root, previous['request_id'], provider=provider)
                if receipt['state'] not in _TERMINAL and previous['expires_at'] > time.time():
                    raise ProbeError('probe_busy')
        request_id = secrets.token_hex(16)
        value = {'version': control['version'], 'epoch': control['epoch'], 'request_id': request_id,
                 'operation': 'validate_workspace', 'expires_at': time.time() + 600}
        if 'provider' in control: value['provider'] = provider
        _atomic(request_path, _sign(value, control['host_token']))
        return request_id


def _valid_request(value, control):
    keys = _REQUEST_KEYS | {'provider'} if 'provider' in control else _REQUEST_KEYS
    return (set(value) == keys and type(value.get('version')) is int and value.get('version') == control['version'] and
            isinstance(value.get('request_id'), str) and bool(_HEX32.fullmatch(value['request_id'])) and
            value.get('operation') == 'validate_workspace' and type(value.get('expires_at')) in (int, float) and
            _valid_signature(value, control))


def get_probe_result(root, request_id, *, provider='workbuddy'):
    """Read only the most recent bounded receipt; never creates paths or state."""
    if not isinstance(request_id, str) or not _HEX32.fullmatch(request_id): raise ProbeError('invalid_request_id')
    directory, control = _control(root, provider)
    path = directory / 'receipt.json'
    if not path.exists(): return {'request_id': request_id, 'state': 'pending'}
    value = _read(path)
    if value.get('epoch') != control['epoch']: return {'request_id': request_id, 'state': 'stale_epoch'}
    if not _valid_signature(value, control): raise ProbeError('invalid_receipt')
    if value.get('request_id') != request_id: return {'request_id': request_id, 'state': 'not_found'}
    return {key: item for key, item in value.items() if key != 'signature'}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _status_evidence(value):
    """Only fixed diagnostics, never broker credentials, prompts or raw errors."""
    if not isinstance(value, dict):return {'available':False}
    result={'available':True}
    for key in ('online','ready','wait_active','progress_token_seen'):
        result[key]=value.get(key) is True
    result['has_active_run']=bool(value.get('active_run_id'))
    for key in ('connection_generation','wait_calls','queued_count','progress_count','wait_segment_count',
                'wait_window_started','wait_window_deadline','wait_active_seconds','wait_gap_deadline','wait_gap_seconds'):
        item=value.get(key)
        result[key]=item if _number(item) and 0<=item<=10**12 else None
    result['provider']=value.get('provider') if value.get('provider') in ('workbuddy','doubao') else None
    result['wait_policy']=value.get('wait_policy') if value.get('wait_policy') in ('single_call','segmented') else None
    result['wait_phase']=value.get('wait_phase') if value.get('wait_phase') in ('active','gap','closed') else None
    window=value.get('wait_window_id')
    result['wait_window_id']=window if isinstance(window,str) and _HEX32.fullmatch(window) else None
    reason=value.get('wait_end_reason')
    result['wait_end_reason']=reason if isinstance(reason,str) and reason in _END_REASONS else None
    return result


class ProbeInbox:
    def __init__(self, root, epoch, broker, *, provider='workbuddy', clock=time.monotonic):
        self.namespace = mcp_namespace(provider)
        self.provider = self.namespace.provider
        if getattr(broker, 'provider', self.provider) != self.provider: raise ProbeError('broker_provider_mismatch')
        self.root = _safe_path(root)
        self.directory = self.root / 'data' / self.namespace.probe_directory
        self.directory.mkdir(parents=True, exist_ok=True)
        _directory(self.directory)
        _private_acl(self.directory)
        if not isinstance(epoch, str) or not _HEX64.fullmatch(epoch): raise ProbeError('invalid_epoch')
        self.control = {'version': 2, 'provider': self.provider, 'epoch': epoch, 'host_token': secrets.token_hex(32)}
        _atomic(self.directory / 'control.json', self.control)
        self.broker, self.clock = broker, clock
        self.active = None
        self.seen = set()

    def _receipt(self, state, **extra):
        active = self.active
        _, current = _control(self.root, self.provider)
        if current != self.control: raise ProbeError('daemon_changed')
        if state == 'failed' and 'end_status' not in extra:
            try:extra['end_status']=_status_evidence(self.broker.status())
            except (OSError,ValueError,TypeError):extra['end_status']={'available':False}
        value = {'version': 2, 'provider': self.provider, 'epoch': self.control['epoch'], 'request_id': active['request']['request_id'],
                 'state': state, 'updated_at': time.time(), **extra}
        if 'evidence' in active: value['wait_evidence'] = active['evidence']
        if 'workspace' in active: value['workspace'] = str(active['workspace'])
        _atomic(self.directory / 'receipt.json', _sign(value, self.control['host_token']))
        if state in _TERMINAL: self.active = None

    def tick(self):
        try:
            self._tick()
        except ProbeError as error:
            reason=error.reason if error.reason in _SAFE_FAILURE_REASONS else 'probe_validation_failed'
            self._fail_active(reason)
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            self._fail_active('probe_validation_failed')

    def _fail_active(self, reason):
        if self.active:
            if self.active.get('run_id'):
                self.broker.cancel(self.active['run_id'])
            try: self._receipt('failed', reason=reason)
            except (OSError, ValueError): self.active = None

    def _segmented_ready(self, active, status, *, final=False):
        """A server-owned window may span normal gaps; only active time counts."""
        if (status.get('provider')!='doubao' or status.get('epoch')!=self.control['epoch'] or
                getattr(self.broker,'provider',None)!='doubao' or
                getattr(self.broker,'epoch',None)!=self.control['epoch'] or
                status.get('wait_policy')!='segmented'):
            raise ProbeError('waiting_identity_changed')
        if status.get('active_run_id') is not None or status.get('queued_count')!=0:
            raise ProbeError('waiting_session_changed')
        phase=status.get('wait_phase')
        if ('wait_window_id' not in active and status.get('wait_window_id') is None and
                phase=='closed' and status.get('wait_end_reason')=='not_started' and
                type(status.get('wait_segment_count')) is int and status['wait_segment_count']==0 and
                status.get('wait_active') is False and status.get('ready') is False and not final):
            return False  # Connected, but not armed for its first wait yet.
        if phase not in ('active','gap'):
            raise ProbeError('wait_window_closed')
        now=self.clock()
        window=status.get('wait_window_id');started=status.get('wait_window_started')
        deadline=status.get('wait_window_deadline');seconds=status.get('wait_active_seconds')
        count=status.get('wait_segment_count');call=status.get('wait_calls')
        generation=status.get('connection_generation')
        if (not isinstance(window,str) or not _HEX32.fullmatch(window) or
                not all(_number(item) for item in (now,started,deadline,seconds)) or
                not started<=now<deadline or not 0<deadline-started<=900 or
                type(count) is not int or not 1<=count<=24 or
                type(call) is not int or call<1 or type(generation) is not int or generation<1 or
                not 0<=seconds<=min(900,now-started+.01,40*count+.01)):
            raise ProbeError('invalid_wait_evidence')
        if phase=='gap':
            gap_end=status.get('wait_gap_deadline');gap_seconds=status.get('wait_gap_seconds')
            if (final or status.get('ready') is not False or status.get('wait_active') is not False or
                    status.get('wait_end_reason')!='idle_continue' or
                    not _number(gap_end) or not now<gap_end<=now+15.01 or
                    not _number(gap_seconds) or not 0<=gap_seconds<15 or
                    abs(seconds-40*count)>.01):
                raise ProbeError('wait_gap_invalid')
        elif (status.get('wait_active') is not True or status.get('wait_end_reason') is not None or
              (final and status.get('ready') is not True)):
            raise ProbeError('waiting_session_changed')
        if 'wait_window_id' not in active:
            if phase!='active' or status.get('ready') is not True:return False
            active.update(wait_window_id=window,window_started=started,window_deadline=deadline,
                connection_generation=generation,baseline_seconds=seconds,baseline_segments=count,
                baseline_call=call,last_seconds=seconds,last_segments=count,last_call=call,
                last_phase=phase,since=now)
        elif (window!=active['wait_window_id'] or started!=active['window_started'] or
              deadline!=active['window_deadline'] or generation!=active['connection_generation'] or
              seconds<active['last_seconds'] or count<active['last_segments'] or call<active['last_call'] or
              count-active['baseline_segments']!=call-active['baseline_call'] or
              (phase==active['last_phase']=='gap' and count==active['last_segments'] and
               seconds!=active['last_seconds'])):
            raise ProbeError('waiting_session_changed')
        elapsed=seconds-active['baseline_seconds']
        if elapsed>now-active['since']+.01:raise ProbeError('invalid_wait_evidence')
        observed=count-active['baseline_segments']+1
        active.update(last_seconds=seconds,last_segments=count,last_call=call,last_phase=phase)
        active['evidence']={'wait_policy':'segmented','window_id':window,'wait_calls':call,
            'connection_generation':generation,'active_seconds':round(elapsed,3),
            'segment_count':observed,'window_active_seconds':round(seconds,3),
            'window_segment_count':count,'max_gap_seconds':15,
            'progress_token_seen':status.get('progress_token_seen') is True,
            'progress_count':status.get('progress_count',0)}
        qualified=phase=='active' and status.get('ready') is True and observed>=2 and elapsed>=70
        if qualified:
            if final and call!=active['wait_call']:raise ProbeError('waiting_session_changed')
            active['wait_call']=call
        elif final:
            raise ProbeError('waiting_session_changed')
        return qualified

    def _tick(self):
        if self.active is None:
            path = self.directory / 'request.json'
            if not path.exists(): return
            value = _read(path)
            if not _valid_request(value, self.control) or value['request_id'] in self.seen: return
            if len(self.seen) >= 256: return  # bounded per daemon epoch
            self.seen.add(value['request_id'])
            self.active = {'request': value}
            if not time.time() < value['expires_at'] <= time.time() + 601:
                self._receipt('failed', reason='request_expired'); return
            self._receipt('waiting')
        active = self.active
        if active['request']['expires_at'] <= time.time():
            if active.get('run_id'): self.broker.cancel(active['run_id'])
            self._receipt('failed', reason='request_expired'); return
        if active.get('run_id'):
            self._check_result(active)
            return
        status = self.broker.status()
        if not status.get('online'):
            self._receipt('failed', reason='worker_offline'); return
        if self.provider=='doubao':
            if not self._segmented_ready(active,status):return
        else:
            ready = (status.get('ready') is True and status.get('wait_active') is True and
                     status.get('active_run_id') is None and status.get('queued_count', 0) == 0 and
                     type(status.get('wait_calls')) is int and status['wait_calls'] > 0)
            if 'wait_call' not in active:
                if not ready: return
                active.update(wait_call=status['wait_calls'], since=self.clock(),
                              connection_generation=status.get('connection_generation', 0))
            elif (not ready or status['wait_calls'] != active['wait_call'] or
                  status.get('connection_generation', 0) != active['connection_generation']):
                self._receipt('failed', reason='waiting_session_changed'); return
            elapsed = self.clock() - active['since']
            active['evidence'] = {'wait_policy':'single_call','wait_calls': status['wait_calls'],
                                  'idle_wait_seconds': round(elapsed, 3),
                                  'connection_generation': active['connection_generation'],
                                  'progress_token_seen': status.get('progress_token_seen') is True,
                                  'progress_count': status.get('progress_count', 0)}
            if elapsed < 70: return
        # The production queue can move between status observations. Hold the
        # same broker lock as submit/poll/cancel for the final compare-and-submit.
        # Test doubles need no lock; the concrete WorkBuddyBroker always has it.
        with getattr(self.broker, '_lock', nullcontext()):
            latest = self.broker.status()
            if latest.get('online') is not True:raise ProbeError('worker_offline')
            if self.provider=='doubao':
                self._segmented_ready(active,latest,final=True)
            elif (latest.get('ready') is not True or latest.get('online') is not True or
                    latest.get('wait_active') is not True or latest.get('active_run_id') is not None or
                    latest.get('queued_count', 0) != 0 or latest.get('wait_calls') != active['wait_call'] or
                    latest.get('connection_generation', 0) != active['connection_generation']):
                self._receipt('failed', reason='waiting_session_changed'); return
            active['evidence'].update(progress_token_seen=latest.get('progress_token_seen') is True,
                                      progress_count=latest.get('progress_count', 0))
            request_id = active['request']['request_id']
            managed = self.root / 'workspaces'
            _directory(managed)
            with _pins(managed):
                parent = managed / self.namespace.probe_workspaces
                parent.mkdir(exist_ok=True)
                _directory(parent)
                with _pins(parent):
                    run_root = parent / request_id
                    run_root.mkdir()  # never reuse/overwrite a previous probe workspace
                    workspace = run_root / 'workspace'
                    workspace.mkdir()
                    with _pins(workspace):
                        with (workspace / 'seed.txt').open('xb') as stream: stream.write(b'KEEP\n')
            active.update(workspace=workspace, run_id='probe-' + request_id, started=self.clock())
            prompt = _PROMPT.replace('todo-dispatcher 连接器', self.namespace.connector_name + ' 连接器', 1)
            self.broker.submit(active['run_id'], workspace, prompt, workspace_mode='default', timeout=180, priority=0)
            binding={'window_id':active['wait_window_id']} if self.provider=='doubao' else {}
            self.broker._bind_probe_wait(active['run_id'], active['wait_call'], active['connection_generation'],**binding)
            self._receipt('running')

    def _check_result(self, active):
        result = self.broker.result(active['run_id'])
        if result['state'] in {'cancelled', 'expired', 'disconnected'}:
            self._receipt('failed', reason='worker_' + result['state'], result=result); return
        if result['state'] != 'submitted':
            if self.clock() - active['started'] >= 180:
                self.broker.cancel(active['run_id'])
                self._receipt('failed', reason='job_timeout')
            return
        claim = result.get('claim_evidence')
        expected = {'wait_calls': active['wait_call'], 'connection_generation': active['connection_generation']}
        if self.provider=='doubao':expected['window_id']=active['wait_window_id']
        if (not isinstance(claim, dict) or claim.get('matched') is not True or
                claim.get('wait_active') is not True or claim.get('preempted') is not False or
                claim.get('expected') != expected or claim.get('actual') != expected):
            raise ProbeError('claim_wait_mismatch')
        report = result.get('submission')
        if (not isinstance(report, dict) or set(report) != {'status', 'summary', 'artifacts', 'checks', 'needs_input'} or
                report['status'] != 'complete' or report['artifacts'] != ['probe.txt'] or report['needs_input'] != ''):
            raise ProbeError('invalid_report')
        hashes = {}
        for name, expected in (('seed.txt', b'KEEP\n'), ('probe.txt', b'READY\n')):
            path = active['workspace'] / name
            _check_components(path)
            info = path.lstat()
            _no_link(info)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != len(expected): raise ValueError()
            output = io.BytesIO()
            hashes[name] = _read_file(path, _identity(info), output)
            if output.getvalue() != expected: raise ValueError()
        if result.get('artifact_sha256') != {'probe.txt': hashes['probe.txt']}: raise ValueError()
        self._receipt('passed', result=result, verified_sha256=hashes)
