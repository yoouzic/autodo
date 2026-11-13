"""Durable guards for workers whose remote execution may outlive a client.

These records supplement process-owned leases. Running and quarantined records
remain active after a crash or reboot; no timestamp or dead PID releases them.
Only this run's frozen identity can perform a known state transition.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
import unicodedata
import uuid

from .workspace_copy import _safe_path, _check_components


MAX_RECORDS=4096
MAX_RECORD_BYTES=16*1024
MAX_TOTAL_BYTES=16*1024*1024
LOCK_TIMEOUT_SECONDS=3
QUARANTINE_REASONS=frozenset({
    'timeout','cancelled','parent_lost','output_limit','client_failed',
    'supervisor_error','protocol_error','manifest_invalid','execution_exception',
    'remote_stop_unconfirmed','unknown_remote_state',
})
RELEASE_REASONS=frozenset({'verified_completion','not_started'})
_NAME=re.compile(r'[0-9a-f]{64}\.json\Z')
_TOKEN=re.compile(r'[0-9a-f]{64}\Z')
_EXECUTOR=re.compile(r'[a-z][a-z0-9_-]{0,31}\Z')
_FIELDS=frozenset({'version','guard_id','run_id','root','workspace','executor',
                   'status','reason','created_at','updated_at'})
_IMMUTABLE=('version','guard_id','run_id','root','workspace','executor','created_at')
_ERROR='工作区持续保护记录不可确认；请检查本机记录，当前目录不能复用。'


class WorkspaceGuardError(ValueError):
    pass


def _key(value):
    return os.path.normpath(str(value)).replace('\\','/').casefold().rstrip('/')


def _overlaps(first,second):
    a,b=_key(first),_key(second)
    return a==b or a.startswith(b+'/') or b.startswith(a+'/')


def _text(value,maximum):
    return (isinstance(value,str) and bool(value) and value==value.strip() and len(value)<=maximum
            and not any(unicodedata.category(char) in {'Cc','Cf','Cs'} for char in value))


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _timestamp(value):
    if not isinstance(value,str) or len(value)!=27:
        raise WorkspaceGuardError(_ERROR)
    try:datetime.strptime(value,'%Y-%m-%dT%H:%M:%S.%fZ')
    except ValueError:raise WorkspaceGuardError(_ERROR) from None


def _root(value):
    try:
        root=_safe_path(value)
        if not root.is_dir():raise ValueError('missing root')
        return root
    except (OSError,ValueError,RuntimeError,TypeError):
        raise WorkspaceGuardError(_ERROR) from None


def _registry(root,*,create):
    directory=root/'data/workspace-guards'
    try:
        for path in (directory.parent,directory):
            _check_components(path)
            if create:path.mkdir(exist_ok=True)
            _check_components(path)
            if path.exists() and not path.is_dir():raise ValueError('not a directory')
        if directory.resolve(strict=False)!=directory:
            raise ValueError('registry redirected')
        return directory
    except (OSError,ValueError,RuntimeError):
        raise WorkspaceGuardError(_ERROR) from None


def _workspace(value,directory):
    if not _text(str(value),4096):raise WorkspaceGuardError(_ERROR)
    try:
        path=_safe_path(value)
        if path.exists() and not path.is_dir():raise ValueError('not a directory')
        if _overlaps(path,directory):raise ValueError('overlapping metadata')
        return path
    except (OSError,ValueError,RuntimeError,TypeError):
        raise WorkspaceGuardError(_ERROR) from None


def _file_info(path,maximum=MAX_RECORD_BYTES):
    _check_components(path)
    info=path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_size>maximum or
            getattr(info,'st_nlink',1)!=1 or getattr(info,'st_file_attributes',0)&0x400):
        raise WorkspaceGuardError(_ERROR)
    return info


def _identity(info):
    return info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns


@contextmanager
def _locked(directory):
    handle=None
    try:
        path=directory/'.metadata.lock'
        _check_components(path)
        if path.exists():_file_info(path,1)
        flags=os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_BINARY',0)
        descriptor=os.open(path,flags,0o600)
        handle=os.fdopen(descriptor,'r+b')
        if _identity(os.fstat(handle.fileno()))!=_identity(_file_info(path,1)):
            raise WorkspaceGuardError(_ERROR)
        deadline=time.monotonic()+LOCK_TIMEOUT_SECONDS
        while True:
            try:
                handle.seek(0)
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES,errno.EAGAIN,errno.EDEADLK}:raise
                if time.monotonic()>=deadline:raise WorkspaceGuardError('工作区保护记录正在更新，请稍后重试。')
                time.sleep(.01)
        if _identity(os.fstat(handle.fileno()))!=_identity(_file_info(path,1)):
            raise WorkspaceGuardError(_ERROR)
        yield
    except WorkspaceGuardError:raise
    except (OSError,ValueError,RuntimeError):raise WorkspaceGuardError(_ERROR) from None
    finally:
        if handle is not None:handle.close()


def _unique(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise ValueError('duplicate key')
        value[key]=item
    return value


def _validate(record,root,directory,*,filename=None):
    if not isinstance(record,dict) or set(record)!=_FIELDS or type(record['version']) is not int or record['version']!=1:
        raise WorkspaceGuardError(_ERROR)
    if (not isinstance(record['guard_id'],str) or not _TOKEN.fullmatch(record['guard_id']) or
            not _text(record['run_id'],256) or not isinstance(record['executor'],str) or not _EXECUTOR.fullmatch(record['executor'])):
        raise WorkspaceGuardError(_ERROR)
    if record['root']!=str(root):raise WorkspaceGuardError(_ERROR)
    if not isinstance(record['workspace'],str) or str(_workspace(record['workspace'],directory))!=record['workspace']:
        raise WorkspaceGuardError(_ERROR)
    _timestamp(record['created_at']);_timestamp(record['updated_at'])
    state,reason=record['status'],record['reason']
    if not ((state=='running' and reason is None) or
            (state=='released' and isinstance(reason,str) and reason in RELEASE_REASONS) or
            (state=='quarantined' and isinstance(reason,str) and reason in QUARANTINE_REASONS)):
        raise WorkspaceGuardError(_ERROR)
    expected=hashlib.sha256(record['run_id'].encode('utf-8')).hexdigest()+'.json'
    if filename is not None and filename!=expected:raise WorkspaceGuardError(_ERROR)
    return dict(record)


def _records(root,directory):
    records={};total=0
    try:
        with os.scandir(directory) as entries:
            for index,entry in enumerate(entries):
                if index>MAX_RECORDS:raise WorkspaceGuardError(_ERROR)
                path=Path(entry.path)
                if entry.name=='.metadata.lock':
                    _file_info(path,1);continue
                if not _NAME.fullmatch(entry.name):raise WorkspaceGuardError(_ERROR)
                if len(records)>=MAX_RECORDS:raise WorkspaceGuardError(_ERROR)
                before=_file_info(path);total+=before.st_size
                if total>MAX_TOTAL_BYTES:raise WorkspaceGuardError(_ERROR)
                descriptor=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_BINARY',0))
                with os.fdopen(descriptor,'rb') as source:
                    if _identity(os.fstat(source.fileno()))!=_identity(before):raise WorkspaceGuardError(_ERROR)
                    raw=source.read(MAX_RECORD_BYTES+1)
                    if len(raw)>MAX_RECORD_BYTES or _identity(os.fstat(source.fileno()))!=_identity(before):raise WorkspaceGuardError(_ERROR)
                if _identity(_file_info(path))!=_identity(before):raise WorkspaceGuardError(_ERROR)
                record=json.loads(raw.decode('utf-8'),object_pairs_hook=_unique,
                                  parse_constant=lambda _:(_ for _ in ()).throw(ValueError('constant')))
                records[entry.name]=_validate(record,root,directory,filename=entry.name)
        return records
    except WorkspaceGuardError:raise
    except (OSError,ValueError,RuntimeError,UnicodeError,TypeError,RecursionError):
        raise WorkspaceGuardError(_ERROR) from None


def _encoded(record):
    value=json.dumps(record,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
    if len(value)>MAX_RECORD_BYTES:raise WorkspaceGuardError(_ERROR)
    return value


def _check_capacity(directory,records,name,payload):
    # Reject before publishing rather than leaving a newly valid record that
    # pushes the whole registry beyond the reader's aggregate bound.
    total=len(payload)
    for filename in records:
        if filename!=name:total+=_file_info(directory/filename).st_size
    if total>MAX_TOTAL_BYTES:raise WorkspaceGuardError(_ERROR)


def start_guard(root,run_id,workspace,executor='doubao'):
    root=_root(root)
    if not _text(run_id,256) or not isinstance(executor,str) or not _EXECUTOR.fullmatch(executor):
        raise WorkspaceGuardError('工作区保护需要有效的运行编号和执行器。')
    directory=_registry(root,create=True);workspace=_workspace(workspace,directory)
    name=hashlib.sha256(run_id.encode('utf-8')).hexdigest()+'.json'
    with _locked(directory):
        records=_records(root,directory)
        if name in records:raise WorkspaceGuardError('该运行已有保护记录，不能重复登记。')
        if len(records)>=MAX_RECORDS:raise WorkspaceGuardError(_ERROR)
        if any(record['status']!='released' and _overlaps(workspace,record['workspace']) for record in records.values()):
            raise WorkspaceGuardError('该目录或其上下级仍有未确认停止的任务，不能复用。')
        now=_now()
        record={'version':1,'guard_id':secrets.token_hex(32),'run_id':run_id,'root':str(root),
                'workspace':str(workspace),'executor':executor,'status':'running','reason':None,
                'created_at':now,'updated_at':now}
        _validate(record,root,directory,filename=name)
        payload=_encoded(record)
        _check_capacity(directory,records,name,payload)
        try:
            # Exclusive creation is deliberate. A crash during this first write
            # leaves a malformed record and subsequent readers fail closed.
            with (directory/name).open('xb') as output:
                output.write(payload);output.flush();os.fsync(output.fileno())
            _file_info(directory/name)
        except (OSError,ValueError,RuntimeError):raise WorkspaceGuardError(_ERROR) from None
    return dict(record)


def _transition(root,frozen,status,reason):
    root=_root(root);directory=_registry(root,create=False)
    frozen=_validate(frozen,root,directory)
    if not directory.is_dir():raise WorkspaceGuardError(_ERROR)
    name=hashlib.sha256(frozen['run_id'].encode('utf-8')).hexdigest()+'.json'
    with _locked(directory):
        records=_records(root,directory)
        record=records.get(name)
        if record is None or any(record[key]!=frozen[key] for key in _IMMUTABLE):
            raise WorkspaceGuardError('工作区保护身份已改变，拒绝更新。')
        if record['status']==status:
            if record['reason']!=reason:raise WorkspaceGuardError('工作区保护已记录不同结论，拒绝覆盖。')
            return dict(record)
        if record['status']!='running':
            raise WorkspaceGuardError('已结束或隔离的保护记录不能自动改写。')
        updated=dict(record,status=status,reason=reason,updated_at=_now())
        _validate(updated,root,directory,filename=name)
        payload=_encoded(updated)
        _check_capacity(directory,records,name,payload)
        temporary=directory/('.guard-'+uuid.uuid4().hex+'.tmp')
        try:
            with temporary.open('xb') as output:
                output.write(payload);output.flush();os.fsync(output.fileno())
            _check_components(directory);_file_info(directory/name);_file_info(temporary)
            temporary.replace(directory/name)
        except (OSError,ValueError,RuntimeError):raise WorkspaceGuardError(_ERROR) from None
    return dict(updated)


def release_guard(root,frozen,reason='verified_completion'):
    if not isinstance(reason,str) or reason not in RELEASE_REASONS:raise WorkspaceGuardError('未知的工作区保护解除原因。')
    return _transition(root,frozen,'released',reason)


def quarantine_guard(root,frozen,reason):
    if not isinstance(reason,str) or reason not in QUARANTINE_REASONS:raise WorkspaceGuardError('未知的工作区隔离原因。')
    return _transition(root,frozen,'quarantined',reason)


def active_guards(root):
    root=_root(root);directory=_registry(root,create=False)
    if not directory.exists():return []
    with _locked(directory):records=_records(root,directory)
    public=('workspace','run_id','executor','status','reason')
    return sorted(({key:record[key] for key in public} for record in records.values() if record['status']!='released'),
                  key=lambda item:(_key(item['workspace']),item['run_id']))
