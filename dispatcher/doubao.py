"""Fresh Doubao CLI tasks with host-bound, strictly validated file manifests.

This module does not start processes or authenticate. The public CLI has no JSON
stdout option, so a normal one-shot exit and a per-run challenge manifest are
required together. They establish execution evidence, never human approval.
The caller must freeze read_binding() BEFORE launching, check its process receipt,
and quarantine the workspace when cancellation/termination is unconfirmed. No
extra roots returned here imply that the shared service is sandboxed by its CLI.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import stat
import time
import unicodedata
from pathlib import Path, PureWindowsPath

from .workspace_copy import (_check_components, _directory, _identity, _no_link,
                             _read_file, _safe_path)
from .workspace_runtime import validate_artifact_path


BINDING_NAME='doubao-manifest-binding.json'
MANIFEST_PREFIX='.dispatcher-doubao-'
MAX_MANIFEST_BYTES=256*1024
MAX_BINDING_BYTES=32*1024
MAX_ARTIFACTS=256
MAX_ARTIFACT_TOTAL_BYTES=512*1024*1024
EFFORTS=frozenset({'none','minimal','low','medium','high','xhigh','max'})
_MODEL=re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z')
_RUN=re.compile(r'[A-Za-z0-9][A-Za-z0-9_:-]{0,127}\Z')
_NONCE=re.compile(r'[0-9a-f]{64}\Z')
_SESSION=re.compile(r'[A-Za-z0-9][A-Za-z0-9_:-]{0,255}\Z')
_REPORT_KEYS=frozenset({'run_id','nonce','status','summary','artifacts','checks','needs_input'})
_BINDING_KEYS=frozenset({'version','executor','run_id','nonce','workspace','run_dir','runtime',
    'manifest_name','created_ns','expires_ns','workspace_identity','run_identity',
    'command_sha256','prompt_sha256','permission_mode'})


class DoubaoRunIncomplete(ValueError):
    """A fixed diagnostic plus the caller's obligation to preserve exclusivity."""
    def __init__(self,reason='invalid_result',*,requires_quarantine=True,diagnostics=None):
        self.reason=reason
        self.requires_quarantine=requires_quarantine
        self.diagnostics=dict(diagnostics or {})
        super().__init__('豆包任务未取得可信的完整执行结果；'+
            ('工作区需要隔离检查后再复用。' if requires_quarantine else '请查看本次运行状态。'))


class DoubaoLoginRequired(DoubaoRunIncomplete):
    def __init__(self):
        super().__init__('login_required',requires_quarantine=False)
        self.args=('豆包 CLI 尚未登录；本次确认没有启动任务，请先完成官方登录。',)


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def _unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('Duplicate JSON key')
        result[key]=value
    return result


def _invalid_constant(_value):
    raise ValueError('Non-JSON constant')


def _decode(raw):
    return json.loads(raw.decode('utf-8-sig'),object_pairs_hook=_unique_object,parse_constant=_invalid_constant)


def _local_path(value):
    if not isinstance(value,(str,Path)) or not str(value).strip() or '\x00' in str(value):
        raise ValueError('Doubao paths must be local absolute paths')
    path=Path(value);windows=PureWindowsPath(str(value))
    if (not path.is_absolute() or str(value).startswith(('\\\\','//')) or
            windows.drive.startswith('\\\\') or ':' in str(value)[len(windows.drive):]):
        raise ValueError('Doubao paths must be local absolute paths')
    _check_components(path)
    path=path.resolve()
    _check_components(path)
    return path


def _ordinary_file(path,limit):
    _check_components(path)
    info=path.lstat();_no_link(info)
    if (not stat.S_ISREG(info.st_mode) or info.st_size<=0 or info.st_size>limit or
            getattr(info,'st_nlink',1)>1):
        raise ValueError('Expected bounded, nonempty, unlinked ordinary file')
    return info


def _read_json_file(path,limit):
    info=_ordinary_file(path,limit)
    output=io.BytesIO()
    digest=_read_file(path,_identity(info),output)
    return _decode(output.getvalue()),digest,info


def _validate_binding(binding):
    if not isinstance(binding,dict) or set(binding)!=_BINDING_KEYS:
        raise ValueError('Invalid host binding shape')
    if type(binding['version']) is not int or binding['version']!=1 or binding['executor']!='doubao' or binding['permission_mode']!='ask_always':
        raise ValueError('Invalid host binding mode')
    if (not isinstance(binding['run_id'],str) or not _RUN.fullmatch(binding['run_id']) or
            not isinstance(binding['nonce'],str) or not _NONCE.fullmatch(binding['nonce']) or
            binding['manifest_name']!=MANIFEST_PREFIX+binding['nonce']+'.json'):
        raise ValueError('Invalid host binding challenge')
    for key in ('created_ns','expires_ns'):
        if type(binding[key]) is not int or binding[key]<=0:raise ValueError('Invalid binding time')
    if not 0<binding['expires_ns']-binding['created_ns']<=86400*1_000_000_000:
        raise ValueError('Invalid binding lifetime')
    for key in ('command_sha256','prompt_sha256'):
        if not isinstance(binding[key],str) or not _NONCE.fullmatch(binding[key]):
            raise ValueError('Invalid binding digest')
    workspace=_safe_path(binding['workspace']);run_dir=_local_path(binding['run_dir'])
    runtime=_local_path(binding['runtime'])
    if run_dir==workspace or run_dir.is_relative_to(workspace):
        raise ValueError('Run metadata cannot be inside the execution directory')
    if runtime!=run_dir/'runtime' and not (workspace==run_dir/'workspace' and runtime==workspace/'.runtime'):
        raise ValueError('Runtime must belong to this run')
    for key,path in (('workspace_identity',workspace),('run_identity',run_dir)):
        identity=binding[key];current=_directory(path)
        if (not isinstance(identity,list) or len(identity)!=2 or
                any(type(part) is not int for part in identity) or
                identity!=[current.st_dev,current.st_ino]):
            raise ValueError('Workspace or run directory identity changed')
    return workspace,run_dir,runtime


def read_binding(run_dir):
    """Read and freeze this dict before launch; never infer it from agent output."""
    run_dir=_local_path(run_dir)
    binding,_,_=_read_json_file(run_dir/BINDING_NAME,MAX_BINDING_BYTES)
    _,saved_run,_=_validate_binding(binding)
    if saved_run!=run_dir:raise ValueError('Binding belongs to another run')
    return binding


def build_invocation(command,workspace,run_dir,prompt,settings,runtime,*,run_id,nonce=None,max_age_seconds=3600):
    """Return argv/env/stdin/write-roots and exclusively create host binding.

    Only the executable may be configured. No resume, background or service-start
    flags can enter this command. The returned extra writable roots are empty;
    the caller must independently validate the native shared-service boundary.
    """
    if not isinstance(command,(list,tuple)) or len(command)!=1 or not isinstance(command[0],str):
        raise ValueError('Doubao command must contain exactly one executable')
    executable=_local_path(command[0])
    if executable.suffix.lower()!='.exe' or not executable.is_file():
        raise ValueError('Doubao requires an existing native executable')
    if (not isinstance(prompt,str) or not prompt.strip() or '\x00' in prompt or
            len(prompt.encode('utf-16-le'))//2>20000):
        raise ValueError('Doubao prompt must be bounded nonempty text')
    if not isinstance(run_id,str) or not _RUN.fullmatch(run_id):raise ValueError('Invalid run ID')
    nonce=secrets.token_hex(32) if nonce is None else nonce
    if not isinstance(nonce,str) or not _NONCE.fullmatch(nonce):raise ValueError('Invalid task nonce')
    if type(max_age_seconds) is not int or not 1<=max_age_seconds<=86400:
        raise ValueError('Invalid manifest lifetime')
    settings={} if settings is None else settings
    if not isinstance(settings,dict):raise ValueError('Invalid Doubao settings')
    model=settings.get('cli_model') or settings.get('model');effort=settings.get('effort')
    if model is not None and (not isinstance(model,str) or not _MODEL.fullmatch(model)):
        raise ValueError('Invalid Doubao model ID')
    if effort is not None and (not isinstance(effort,str) or effort not in EFFORTS):
        raise ValueError('Invalid Doubao effort')
    workspace=_safe_path(workspace);run_dir=_local_path(run_dir);runtime=_local_path(runtime)
    workspace_info=_directory(workspace);run_info=_directory(run_dir)
    if runtime!=run_dir/'runtime' and not (workspace==run_dir/'workspace' and runtime==workspace/'.runtime'):
        raise ValueError('Runtime must belong to this run')
    if run_dir==workspace or run_dir.is_relative_to(workspace):
        raise ValueError('Run metadata cannot be inside the execution directory')
    name=MANIFEST_PREFIX+nonce+'.json'
    if os.path.lexists(workspace/name):raise ValueError('This manifest name already exists; never reuse a task challenge')
    skeleton={'run_id':run_id,'nonce':nonce,'status':'complete','summary':'实际完成内容',
              'artifacts':['交付物的相对路径'],'checks':['已实际执行的检查'],'needs_input':''}
    delegated=(prompt+'\n\n派工器结果协议（仅用于本次任务）：\n'
        '只在指定工作目录内执行，不启动后台、定时或远程控制任务，不请求不受限权限。'
        '不要读取认证文件、访问其他目录、发送消息或发布内容。'
        '完成并读取核验交付物后，最后写入当前目录下的 UTF-8 JSON 文件 '+name+'。'
        '该清单是执行报告，不代表人工审核通过。严格只写下列七个字段；run_id 和 nonce 必须逐字相同。'
        'status 只能是 complete、needs_input、failed；缺信息或被拒绝时如实填写后两者。'
        'summary、needs_input 是字符串；artifacts、checks 是字符串数组。'
        'artifacts 只列本次实际交付文件的相对路径，不列本清单或其他 .dispatcher-doubao-* 协议文件。'
        '若未完成，不能把残留文件当作成功；按实际情况返回 needs_input 或 failed。\n'+_json(skeleton))
    argv=[str(executable),'-p',delegated,'-C',str(workspace),'--permission-mode','ask_always']
    if model is not None:argv+=['--model',model]
    if effort is not None:argv+=['--effort',effort]
    if sum(len(value.encode('utf-16-le'))//2+3 for value in argv)>30000:
        raise ValueError('Doubao command exceeds the safe Windows argument budget')
    now=time.time_ns()
    binding={'version':1,'executor':'doubao','run_id':run_id,'nonce':nonce,
        'workspace':str(workspace),'run_dir':str(run_dir),'runtime':str(runtime),
        'manifest_name':name,'created_ns':now,'expires_ns':now+max_age_seconds*1_000_000_000,
        'workspace_identity':[workspace_info.st_dev,workspace_info.st_ino],
        'run_identity':[run_info.st_dev,run_info.st_ino],
        'command_sha256':hashlib.sha256(_json(argv).encode('utf-8')).hexdigest(),
        'prompt_sha256':hashlib.sha256(delegated.encode('utf-8')).hexdigest(),
        'permission_mode':'ask_always'}
    encoded=_json(binding).encode('utf-8')
    if len(encoded)>MAX_BINDING_BYTES:raise ValueError('Host binding is too large')
    destination=run_dir/BINDING_NAME;_check_components(destination)
    descriptor=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_BINARY',0)|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(descriptor,'wb') as output:
        output.write(encoded);output.flush();os.fsync(output.fileno())
    return argv,{'CI':'1'},None,[]


def _artifact_name(name):
    if not isinstance(name,str) or not name or len(name)>4096:raise ValueError('Invalid artifact path')
    parts=[part for part in name.replace('\\','/').split('/') if part not in {'','.'}]
    if any(part!=part.rstrip(' .') or part.casefold().startswith(MANIFEST_PREFIX) or
           any(unicodedata.category(char) in {'Cc','Cf','Cs'} for char in part) for part in parts):
        raise ValueError('Control files or Windows path aliases cannot be artifacts')
    return name


def normalize_result(binding,*,exit_code,timed_out=False,cancelled=False,remote_stop_confirmed=False,
                     task_started=None,session_id=None,stderr='',now_ns=None):
    """Validate host lifecycle facts and manifest; never parse completion from stdout.

    remote_stop_confirmed/task_started are trusted caller evidence, never values
    from the manifest. A timeout/cancel is never accepted as successful completion,
    even if the agent already wrote a valid report. Guard release belongs to caller.
    """
    for value in (timed_out,cancelled,remote_stop_confirmed):
        if type(value) is not bool:raise ValueError('Lifecycle facts must be boolean')
    if task_started is not None and type(task_started) is not bool:raise ValueError('Invalid task start evidence')
    if exit_code is not None and type(exit_code) is not int:raise ValueError('Invalid process exit code')
    diagnostics={'client_exit_code':exit_code,'timed_out':timed_out,'cancelled':cancelled,
                 'remote_stop_confirmed':remote_stop_confirmed,'task_started':task_started}
    if exit_code!=0 or timed_out or cancelled:
        notice=stderr[:65536].casefold() if isinstance(stderr,str) else ''
        if (exit_code==4 and task_started is False and not timed_out and not cancelled and
                ('未登录' in notice or 'not logged in' in notice or 'login required' in notice)):
            raise DoubaoLoginRequired()
        reason='timeout' if timed_out else 'cancelled' if cancelled else 'client_exit'
        raise DoubaoRunIncomplete(reason,requires_quarantine=not(remote_stop_confirmed or task_started is False),diagnostics=diagnostics)
    try:
        workspace,run_dir,_=_validate_binding(binding)
        saved=read_binding(run_dir)
        if _json(saved)!=_json(binding):raise ValueError('Host binding changed')
        now=time.time_ns() if now_ns is None else now_ns
        if type(now) is not int or not binding['created_ns']<=now<=binding['expires_ns']:
            raise ValueError('Task manifest challenge expired')
        manifest=workspace/binding['manifest_name']
        report,manifest_hash,info=_read_json_file(manifest,MAX_MANIFEST_BYTES)
        if info.st_mtime_ns<binding['created_ns']-2_000_000_000 or info.st_mtime_ns>now+2_000_000_000:
            raise ValueError('Manifest predates the task or has a future timestamp')
        if not isinstance(report,dict) or set(report)!=_REPORT_KEYS:
            raise ValueError('Invalid business manifest schema')
        if report['run_id']!=binding['run_id'] or report['nonce']!=binding['nonce']:
            raise ValueError('Manifest belongs to another invocation')
        if report['status'] not in {'complete','failed','needs_input'}:
            raise ValueError('Invalid business status')
        if any(not isinstance(report[key],str) or '\x00' in report[key] for key in ('status','summary','needs_input')):
            raise ValueError('Invalid business text')
        for key in ('artifacts','checks'):
            if (not isinstance(report[key],list) or len(report[key])>MAX_ARTIFACTS or
                    any(not isinstance(value,str) or '\x00' in value for value in report[key])):
                raise ValueError('Invalid business arrays')
        if report['status']=='complete' and not report['artifacts']:
            raise ValueError('A complete task must have actual deliverables')
        if session_id is not None and (not isinstance(session_id,str) or not _SESSION.fullmatch(session_id)):
            raise ValueError('Invalid diagnostic session ID')
        hashes={};seen=set();total=0;artifact_paths=[]
        for name in report['artifacts']:
            path=validate_artifact_path(workspace,_artifact_name(name))
            if not path.is_relative_to(workspace):raise ValueError('Artifact escapes the selected workspace')
            relative=path.relative_to(workspace).as_posix();key=relative.casefold()
            if key in seen:raise ValueError('Duplicate artifact alias')
            seen.add(key)
            info=_ordinary_file(path,64*1024*1024)
            total+=info.st_size
            if total>MAX_ARTIFACT_TOTAL_BYTES:raise ValueError('Artifact total exceeds limit')
            hashes[relative]=_read_file(path,_identity(info))
            artifact_paths.append((path,relative))
        # Bind the manifest itself through all artifact checks, preventing an
        # in-progress writer from changing the declaration while hashing files.
        _,latest_manifest_hash,_=_read_json_file(manifest,MAX_MANIFEST_BYTES)
        if latest_manifest_hash!=manifest_hash:raise ValueError('Manifest changed during validation')
        for path,relative in artifact_paths:
            info=_ordinary_file(path,64*1024*1024)
            if _read_file(path,_identity(info))!=hashes[relative]:
                raise ValueError('Artifact changed during complete-set validation')
        _,latest_manifest_hash,_=_read_json_file(manifest,MAX_MANIFEST_BYTES)
        if latest_manifest_hash!=manifest_hash:raise ValueError('Manifest changed during complete-set validation')
        _validate_binding(binding)
        finished=time.time_ns() if now_ns is None else now_ns
        if not binding['created_ns']<=finished<=binding['expires_ns']:
            raise ValueError('Manifest expired during validation')
        return {key:report[key] for key in ('status','summary','artifacts','checks','needs_input')} | {
            'executor':'doubao','session_id':session_id,'resume_available':False,'requires_quarantine':False,
            'doubao_receipt':{'version':1,'run_id':binding['run_id'],'manifest_name':binding['manifest_name'],
                'manifest_sha256':manifest_hash,'artifact_sha256':hashes,'file_count':len(hashes),
                'client_exit_code':0,'completion_evidence':'normal_oneshot_exit_and_bound_manifest'}}
    except (OSError,ValueError,TypeError,KeyError,RecursionError,UnicodeError) as exc:
        raise DoubaoRunIncomplete('invalid_manifest',diagnostics=diagnostics) from None
