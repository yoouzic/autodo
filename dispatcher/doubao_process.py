"""Bounded Doubao client lifecycle; a client exit is not a remote-stop receipt.

Windows workers own a hidden console. Ctrl+C is attempted only while that console
contains the worker and its direct client. No process-tree kill or service stop
is performed. Every interrupted/abnormal outcome requires durable workspace
quarantine in the caller, including a graceful client exit after Ctrl+C.
"""
from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid


MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_LOG_BYTES = 8 * 1024 * 1024
POLL_SECONDS = .05


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _ordinary(path, *, directory=False):
    for part in (path, *path.parents):
        try: info = part.lstat()
        except FileNotFoundError: continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Doubao process metadata cannot use links or reparse points')
    if directory and not path.is_dir():
        raise ValueError('Doubao process directories must already exist')
    if path.exists() and not directory:
        info=path.lstat()
        if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_nlink', 1) != 1:
            raise ValueError('Doubao process metadata must be ordinary files')


def _directory(value):
    path=Path(value)
    if not path.is_absolute() or str(path).startswith(('\\\\','//')) or '\x00' in str(path):
        raise ValueError('Doubao process directories must be local absolute paths')
    _ordinary(path,directory=True)
    return path.resolve()


def _duration(value, name, *, minimum=0):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value < minimum:
        raise ValueError(f'{name} must be a finite bounded duration')
    return float(value)


def _read(path, maximum):
    _ordinary(path)
    with path.open('rb') as source:
        data=source.read(maximum+1)
    if len(data)>maximum:raise ValueError('Doubao process metadata exceeds its limit')
    return data


def _publish(path, body, secret):
    signed={'body':body,'hmac_sha256':hmac.new(secret.encode(),_json(body).encode(),hashlib.sha256).hexdigest()}
    temporary=path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
    with temporary.open('x',encoding='utf-8') as output:
        output.write(_json(signed));output.flush();os.fsync(output.fileno())
    _ordinary(path)
    temporary.replace(path)


def _initial_body(request, helper_pid=None):
    run=Path(request['run_dir']);prefix=request['invocation_id']
    return {'version':1,'invocation_id':prefix,'workspace_path':request['cwd'],
            'argv_sha256':hashlib.sha256(_json(request['argv']).encode()).hexdigest(),
            'helper_pid':helper_pid,'client_pid':None,'exit_code':None,'started':False,
            'client_exited':False,'timed_out':False,'cancelled':False,'parent_lost':False,
            'signal_attempted':False,'signal_sent':False,'signal_error':None,
            'force_terminated':False,'output_limit':False,'supervisor_error':False,
            'remote_stop_confirmed':False,'requires_quarantine':True,
            'stdout_path':str(run/(prefix+'.stdout.log')),
            'stderr_path':str(run/(prefix+'.stderr.log'))}


def _windows_console():
    """Return a scoped interrupt function and a callback reference to keep alive."""
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    callback_type=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.DWORD)
    callback=callback_type(lambda event: event==0)
    kernel.SetConsoleCtrlHandler.argtypes=[callback_type,wintypes.BOOL]
    kernel.SetConsoleCtrlHandler.restype=wintypes.BOOL
    if not kernel.SetConsoleCtrlHandler(callback,True):
        raise OSError('Cannot install private console control handler')
    kernel.GetConsoleProcessList.argtypes=[ctypes.POINTER(wintypes.DWORD),wintypes.DWORD]
    kernel.GetConsoleProcessList.restype=wintypes.DWORD
    kernel.GenerateConsoleCtrlEvent.argtypes=[wintypes.DWORD,wintypes.DWORD]
    kernel.GenerateConsoleCtrlEvent.restype=wintypes.BOOL

    def interrupt(client_pid):
        processes=(wintypes.DWORD*256)()
        count=kernel.GetConsoleProcessList(processes,256)
        if not count or count>256:
            return False,'console_members_unknown'
        if not set(processes[:count]).issubset({os.getpid(),client_pid}):
            return False,'unexpected_console_members'
        # CTRL_C_EVENT cannot be targeted to an individual process group. This
        # worker has its own console, and never ignores Ctrl+C via NULL handler
        # (that ignore flag would be inherited by the client).
        sent=bool(kernel.GenerateConsoleCtrlEvent(0,0))
        return sent,None if sent else 'console_signal_failed'
    return interrupt,callback


def _parent_monitor(pid):
    if os.name!='nt':
        def alive():
            try:os.kill(pid,0);return True
            except ProcessLookupError:return False
            except PermissionError:return True
        return alive,lambda:None
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    kernel.WaitForSingleObject.restype=wintypes.DWORD
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.OpenProcess(0x00100000,False,pid)  # SYNCHRONIZE only.
    if not handle:raise OSError('Cannot monitor the owning dispatcher process')
    return lambda:kernel.WaitForSingleObject(handle,0)==0x102,lambda:kernel.CloseHandle(handle)


def _collector(pipe, path, over_limit, failures):
    try:
        retained=0
        with path.open('xb',buffering=0) as output:
            while True:
                chunk=os.read(pipe.fileno(),65536)
                if not chunk:break
                available=max(0,MAX_LOG_BYTES-retained)
                if available:
                    part=chunk[:available];output.write(part);retained+=len(part)
                if len(chunk)>available:over_limit.set()
    except (OSError,ValueError):
        failures.append(True)
        over_limit.set()


def _worker(request):
    """Runs in an owned helper process. Do not invoke in the server process."""
    run=_directory(request['run_dir']);cwd=_directory(request['cwd'])
    prefix=request['invocation_id'];secret=request['secret']
    receipt_path=run/(prefix+'.json');cancel_path=run/(prefix+'.cancel')
    stdout_path=run/(prefix+'.stdout.log');stderr_path=run/(prefix+'.stderr.log')
    body=_initial_body(request,os.getpid())
    client=None;close_parent=lambda:None
    try:
        parent_alive,close_parent=_parent_monitor(request['parent_pid'])
        callback=None
        if os.name=='nt':interrupt,callback=_windows_console()
        else:
            def interrupt(pid):
                try:os.kill(pid,signal.SIGINT);return True,None
                except OSError:return False,'client_signal_failed'
        client=subprocess.Popen(request['argv'],cwd=cwd,stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
        body.update(client_pid=client.pid,started=True)
        over_limit=threading.Event();failures=[]
        readers=[threading.Thread(target=_collector,args=(pipe,path,over_limit,failures),daemon=True)
                 for pipe,path in ((client.stdout,stdout_path),(client.stderr,stderr_path))]
        for reader in readers:reader.start()
        deadline=time.monotonic()+request['timeout'];cancel_deadline=None
        while client.poll() is None:
            now=time.monotonic()
            if cancel_deadline is None:
                reason=('parent_lost' if not parent_alive() else
                        'cancelled' if cancel_path.exists() else
                        'output_limit' if over_limit.is_set() else
                        'timed_out' if now>=deadline else None)
                if reason:
                    body[reason]=True;body['signal_attempted']=True
                    body['signal_sent'],body['signal_error']=interrupt(client.pid)
                    cancel_deadline=now+request['grace_seconds']
            elif now>=cancel_deadline:
                # Kill this direct client only. The shared service can outlive
                # it, hence quarantine remains mandatory regardless of rc.
                client.kill();body['force_terminated']=True
                break
            time.sleep(POLL_SECONDS)
        try:body['exit_code']=client.wait(timeout=2)
        except subprocess.TimeoutExpired:body['supervisor_error']=True
        body['client_exited']=client.poll() is not None
        for reader in readers:reader.join(timeout=1)
        if any(reader.is_alive() for reader in readers) or failures:
            body['supervisor_error']=True
        body['output_limit']=body['output_limit'] or over_limit.is_set()
        body['requires_quarantine']=not (body['exit_code']==0 and body['client_exited'] and
            not any(body[key] for key in ('timed_out','cancelled','parent_lost','output_limit','supervisor_error','force_terminated')))
    except BaseException:
        body['supervisor_error']=True
        if client is not None and client.poll() is None:
            try:client.kill();body['force_terminated']=True;client.wait(timeout=2)
            except (OSError,subprocess.TimeoutExpired):pass
            body['client_exited']=client.poll() is not None
            body['exit_code']=client.returncode
        if not body['started']:body['requires_quarantine']=False
    finally:
        close_parent()
    _publish(receipt_path,body,secret)
    return 0


def run_doubao_process(argv, *, cwd, run_dir, timeout=600, env=None,
                       input_text=None, cancel_event=None, grace_seconds=8):
    """Return a host-authenticated lifecycle receipt and bounded private logs.

    The caller MUST establish a durable in-flight workspace guard before calling
    this function, then retain/quarantine it on any nonclean outcome or exception.
    A successful receipt only proves the client completed; validate the task's
    nonce-bound result manifest before clearing that guard or accepting success.
    """
    if (not isinstance(argv,(list,tuple)) or not argv or
            any(not isinstance(arg,str) or not arg or '\x00' in arg for arg in argv)):
        raise ValueError('Doubao requires a literal argv array')
    if input_text is not None:
        raise ValueError('Doubao one-shot input must use its explicit -p argument')
    timeout=_duration(timeout,'timeout',minimum=.01)
    grace_seconds=_duration(grace_seconds,'grace_seconds',minimum=.1)
    if grace_seconds>30:raise ValueError('Grace period must not exceed 30 seconds')
    cwd,run=_directory(cwd),_directory(run_dir)
    if env is not None and (not isinstance(env,dict) or any(not isinstance(k,str) or not isinstance(v,str) or '\x00' in k+v for k,v in env.items())):
        raise ValueError('Doubao environment overrides must be strings')
    invocation='doubao-process-'+uuid.uuid4().hex
    secret=secrets.token_hex(32)
    payload={'invocation_id':invocation,'secret':secret,'argv':list(argv),'cwd':str(cwd),
             'run_dir':str(run),'parent_pid':os.getpid(),'timeout':timeout,'grace_seconds':grace_seconds}
    encoded=_json(payload).encode('utf-8')
    if len(encoded)>MAX_REQUEST_BYTES:raise ValueError('Doubao invocation exceeds its input limit')
    receipt_path=run/(invocation+'.json');cancel_path=run/(invocation+'.cancel')
    helper_errors=run/(invocation+'.helper.log')
    if cancel_event is not None and cancel_event.is_set():
        body=_initial_body(payload)
        body.update(cancelled=True,requires_quarantine=False)
        _publish(receipt_path,body,secret)
        return dict(body,stdout='',stderr='',receipt_path=str(receipt_path))
    flags=0;startup=None
    if os.name=='nt':
        flags=subprocess.CREATE_NEW_CONSOLE
        startup=subprocess.STARTUPINFO();startup.dwFlags|=subprocess.STARTF_USESHOWWINDOW;startup.wShowWindow=0
    with helper_errors.open('xb') as diagnostics:
        helper=subprocess.Popen([sys.executable,'-I','-B',str(Path(__file__).resolve()),'--worker'],
            stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=diagnostics,cwd=run,
            env=None if env is None else dict(os.environ,**env),creationflags=flags,startupinfo=startup)
        try:
            helper.stdin.write(encoded);helper.stdin.close()
            deadline=time.monotonic()+timeout+grace_seconds+12
            while helper.poll() is None:
                if cancel_event is not None and cancel_event.is_set() and not cancel_path.exists():
                    with cancel_path.open('xb') as cancel:cancel.write(b'cancel')
                if time.monotonic()>=deadline:
                    # The direct client PID is deliberately not rediscovered by
                    # name and no shared service/tree is killed on uncertainty.
                    raise TimeoutError('Doubao process supervisor did not finish; workspace quarantine is required')
                time.sleep(POLL_SECONDS)
        except BaseException:
            if not cancel_path.exists():
                try:
                    with cancel_path.open('xb') as cancel:cancel.write(b'cancel')
                except OSError:pass
            try:helper.wait(timeout=grace_seconds+5)
            except subprocess.TimeoutExpired:pass
            raise
    if helper.returncode!=0 or not receipt_path.is_file():
        raise RuntimeError('Doubao process supervisor failed; workspace quarantine is required')
    envelope=json.loads(_read(receipt_path,65536).decode('utf-8'))
    body=envelope.get('body')
    expected=hmac.new(secret.encode(),_json(body).encode(),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(str(envelope.get('hmac_sha256','')),expected):
        raise ValueError('Doubao lifecycle receipt integrity failed; workspace quarantine is required')
    if (body.get('invocation_id')!=invocation or body.get('workspace_path')!=str(cwd) or
            body.get('argv_sha256')!=hashlib.sha256(_json(list(argv)).encode()).hexdigest()):
        raise ValueError('Doubao lifecycle receipt does not match this run')
    if cancel_event is not None and cancel_event.is_set() and not body['cancelled']:
        # A caller cancellation racing with client exit must not be accepted as
        # success. Re-attest this parent observation before returning the logs.
        body.update(cancelled=True,cancellation_observed_after_exit=True,
                    requires_quarantine=body['started'])
        _publish(receipt_path,body,secret)
    for stream in ('stdout','stderr'):
        path=run/(invocation+f'.{stream}.log')
        body[stream]=_read(path,MAX_LOG_BYTES).decode('utf-8',errors='replace') if path.exists() else ''
    body['receipt_path']=str(receipt_path)
    return body


def _main():
    if sys.argv[1:]!=['--worker']:return 2
    raw=sys.stdin.buffer.read(MAX_REQUEST_BYTES+1)
    if len(raw)>MAX_REQUEST_BYTES:return 2
    return _worker(json.loads(raw.decode('utf-8')))


if __name__=='__main__':
    # Bounded stdout collector threads may be waiting on a handle inherited by
    # an unknown descendant. Do not let interpreter shutdown hang indefinitely.
    os._exit(_main())
