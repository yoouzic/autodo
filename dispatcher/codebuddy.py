"""Independent CodeBuddy Code CLI, using vendor auth and bounded file tools.

The stream format and file-tool envelope are shared with WorkBuddy's CLI,
but the account profile, provider identity and model catalogue are separate.
"""
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import workbuddy

# Grep launches a vendor subprocess that is unavailable in this Windows
# sandbox. Expose the file operations actually verified for this adapter.
TOOLS='Read,Write,Edit,StructuredOutput'


def runtime_limits(config,settings):
    from .execution_timeouts import resolve
    return resolve(config,'codebuddy',settings)


class ProgressFilter:
    """Keep a bounded heartbeat; never persist or return incremental thinking."""
    def __init__(self):
        self.last=0;self.phase=None

    def __call__(self,line):
        try:e=json.loads(line)
        except (ValueError,RecursionError):return line
        if not isinstance(e,dict):return line
        if e.get('type')=='assistant' and isinstance(e.get('message'),dict):
            content=e['message'].get('content')
            if isinstance(content,list):
                e['message']['content']=[b for b in content if not isinstance(b,dict) or b.get('type') not in {'thinking','redacted_thinking'}]
                return json.dumps(e,ensure_ascii=False)+'\n'
        if e.get('type')!='stream_event':return line
        event=e.get('event') or {};delta=event.get('delta') or {}
        if event.get('type')=='error':return '{"type":"error","message":"Vendor stream error"}\n'
        kind=delta.get('type')
        phase={'thinking_delta':'thinking','text_delta':'responding','input_json_delta':'tool_arguments'}.get(kind)
        if phase is None:return None
        now=time.monotonic()
        if phase==self.phase and now-self.last<1:return None
        self.phase=phase;self.last=now
        return json.dumps({'type':'dispatcher_progress','phase':phase,
            'observed_at':datetime.now(timezone.utc).isoformat()})+'\n'


def progress(run_dir):
    """Read only the bounded tail, return no provider text or reasoning content."""
    try:
        path=Path(run_dir)/'execution.events.jsonl'
        with path.open('rb') as f:
            f.seek(0,2);size=f.tell();f.seek(max(0,size-65536));data=f.read(65536)
        if size>65536:data=data.partition(b'\n')[2]
        found=None
        for line in data.decode('utf-8',errors='replace').splitlines():
            try:e=json.loads(line)
            except (ValueError,RecursionError):continue
            if not isinstance(e,dict):continue
            if e.get('type')=='dispatcher_progress' and e.get('phase') in {'thinking','responding','tool_arguments'}:
                stamp=e.get('observed_at')
                try:parsed=datetime.fromisoformat(stamp)
                except (ValueError,TypeError):continue
                if parsed.tzinfo is None:continue
                found={'phase':e['phase'],'last_activity_at':parsed.isoformat()}
        if found:
            found['label']={'thinking':'模型正在思考','responding':'模型正在生成回答','tool_arguments':'模型正在生成工具参数'}[found['phase']]
            idle=max(0,int((datetime.now(timezone.utc)-datetime.fromisoformat(found['last_activity_at'])).total_seconds()))
            if idle>=90:found['label']=f'等待后续输出（已静默 {idle} 秒）'
        return found
    except OSError:return None

class CodeBuddyLoginRequired(ValueError):
    def __init__(self):
        super().__init__('CodeBuddy CLI 需要登录，请在独立 CodeBuddy CLI 的官方登录入口完成授权。')


def load_catalog(root):
    """Only public model metadata; never inspect vendor credential files."""
    path=Path(root)/'data/codebuddy-model-catalog.json'
    if not path.exists(): return []
    if path.stat().st_size > 1024*1024: raise ValueError('CodeBuddy model catalogue too large')
    value=json.loads(path.read_text(encoding='utf-8'))
    rows=value.get('models') if isinstance(value,dict) else None
    if not isinstance(rows,list) or len(rows)>1000: raise ValueError('Invalid CodeBuddy model catalogue')
    result=[]; seen=set()
    for row in rows:
        if not isinstance(row,dict): raise ValueError('Invalid CodeBuddy model metadata')
        name=row.get('id'); label=row.get('label'); levels=row.get('efforts')
        if (not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}',name)
                or name in seen or not isinstance(label,str) or not label or len(label)>256
                or not isinstance(levels,list) or any(not isinstance(level,str) or level not in workbuddy.EFFORTS for level in levels)):
            raise ValueError('Invalid CodeBuddy model metadata')
        seen.add(name); result.append({'id':name,'label':label,'efforts':list(dict.fromkeys(levels))})
    return result


def build_invocation(command,workspace,run_dir,prompt,settings,runtime,*,schema,max_turns=20,home=None,readonly=False,permissions=None,limits=None):
    argv,env,stdin,roots=workbuddy.build_invocation(command,workspace,run_dir,prompt,settings,runtime,
        schema=schema,max_turns=max_turns,home=home)
    argv.append('--include-partial-messages')
    # The independent CLI must never select the WorkBuddy host account profile.
    profile=Path(home) if home is not None else Path.home()
    env['CODEBUDDY_CONFIG_DIR']=str(profile/'.codebuddy')
    # 2.181 starts a trace writer even with session persistence disabled.
    # Keep process output in dispatcher-owned logs, without granting profile writes.
    env['CODEBUDDY_DISABLE_TRACE_COLLECTOR']='1'
    env['CODEBUDDY_DISABLE_FILE_LOGS']='1'
    # Long active reasoning is distinct from a dead request/stream.
    limits=limits or runtime_limits({},settings)
    env['CODEBUDDY_FIRST_TOKEN_TIMEOUT_MS']=str(limits['first_token_seconds']*1000)
    env['CODEBUDDY_STREAM_TIMEOUT_MS']=str(limits['stream_idle_seconds']*1000)
    if 'recovery_first_token_seconds' in limits:
        env['CODEBUDDY_RECOVERY_FIRST_TOKEN_TIMEOUT_MS']=str(limits['recovery_first_token_seconds']*1000)
    env['CODEBUDDY_MAX_RETRIES']='1'
    # 2.181 headless permission checks consult permissions.allow. Pass rules
    # for this invocation only; do not modify the user's global settings.
    index=argv.index('--settings')+1
    options=json.loads(argv[index])
    # JSON-schema output uses this pure report tool in 2.181. Without it the
    # runtime repeatedly asks the model for a tool that was filtered out.
    argv[argv.index('--tools')+1]='Read,StructuredOutput' if readonly else TOOLS
    # Installed 2.181 matches absolute file paths but leaves ./ patterns
    # relative. Escape glob metacharacters in the literal workspace name.
    literal=str(Path(workspace).resolve()).replace('\\','/')
    pattern=''.join('['+char+']' if char in '*?[]{}()!+@' else char for char in literal)+'/**'
    allow=['StructuredOutput'] if readonly else [f'Write({pattern})',f'Edit({pattern})','StructuredOutput']
    # JSON preserves commas in a task/workspace title as literal path data.
    flag=argv.index('--allowedTools');del argv[flag:flag+2]
    index=argv.index('--settings')+1
    # Read retains the native trusted-workspace boundary; a blanket
    # Read allow rule would authorize reads beyond that boundary.
    options['permissions']={'allow':allow}
    argv[index]=json.dumps(options,separators=(',',':'))
    # Never inherit the vendor's high-risk sandbox-full-pass flag from the host.
    env['CODEBUDDY_IS_SANDBOX']=''
    profile_mode=(permissions or {}).get('profile','default')
    if profile_mode in {'auto','full'}:
        if readonly: raise ValueError('Text tasks must remain read-only')
        argv[argv.index('--permission-mode')+1]='auto' if profile_mode=='auto' else 'bypassPermissions'
        argv[argv.index('--tools')+1]=TOOLS+',Bash'
    # The official CLI refreshes OAuth via an atomic lock/temp-file write in
    # this dedicated vendor store. Model tools still cannot read/write it:
    # no added trusted directory, scoped writes and dontAsk remain in force.
    auth_store=profile/'AppData/Local/CodeBuddyExtension/Data/Public/auth'
    if os.name=='nt':
        workbuddy._local_path(auth_store)
        roots.append(auth_store)
    return argv,env,stdin,roots


def timeout_diagnostic(run_dir,seconds):
    """Record bounded event metadata only; absence of output is not a denial."""
    data={'executor':'codebuddy','timeout_seconds':seconds,'stage':'startup','event_count':0,
          'assistant_events':0,'tool_calls':0,'tool_results':0,'permission_denials':0,'terminal':False}
    try:
        with (Path(run_dir)/'execution.events.jsonl').open(encoding='utf-8',errors='replace') as f:
            raw=f.read(4*1024*1024+1)
        data['truncated']=len(raw)>4*1024*1024
        for line in raw[:4*1024*1024].splitlines():
            try: e=json.loads(line)
            except (ValueError,RecursionError): continue
            if not isinstance(e,dict): continue
            data['event_count']+=1
            if e.get('type')=='system' and e.get('subtype')=='init': data['stage']='waiting_first_response'
            if e.get('type')=='assistant':data['assistant_events']+=1
            if e.get('type')=='result':data['terminal']=True
            if isinstance(e.get('permission_denials'),list):data['permission_denials']+=len(e['permission_denials'])
            content=(e.get('message') or {}).get('content',[]) if isinstance(e.get('message'),dict) else []
            for item in content if isinstance(content,list) else []:
                if not isinstance(item,dict):continue
                if item.get('type')=='tool_use':data['tool_calls']+=1
                if item.get('type')=='tool_result':data['tool_results']+=1
        if data['assistant_events']:data['stage']='model_output'
        if data['tool_calls'] or data['tool_results']:data['stage']='tool_activity'
        if data['terminal']:data['stage']='terminal_without_exit'
        if data['permission_denials']:data['stage']='permission_denied'
        active=progress(run_dir)
        if active:
            data['last_activity']=active
            if data['stage']=='waiting_first_response':data['stage']=active['phase']
        (Path(run_dir)/'execution-diagnostics.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    except OSError: pass
    if data.get('last_activity') and data['stage'] in {'thinking','responding','tool_arguments'}:
        active=data['last_activity']
        return f'CodeBuddy 超过 {seconds} 秒运行上限；最后阶段：{active["label"]}，最后活跃于 {active["last_activity_at"]}。已保留诊断；未提高权限或切换模型。'
    if data['stage']=='waiting_first_response':
        return f'CodeBuddy 等待首个模型输出超过 {seconds} 秒；未观察到回答、工具调用或权限拒绝。请稍后重试或明确选择其他模型；提高权限不能据此解决问题。原运行已保留。'
    return f'CodeBuddy 运行超过 {seconds} 秒，阶段：{data["stage"]}；已保留日志和现有文件，未自动重试或提高权限。'


def normalize_result(stdout,*,stderr='',exit_code=0,workspace=None):
    try:
        result=workbuddy.normalize_result(stdout,stderr=stderr,exit_code=exit_code,allow_noncompletion_tool_errors=True,allow_answer=True)
    except workbuddy.WorkBuddyLoginRequired:
        raise CodeBuddyLoginRequired() from None
    except ValueError:
        from .codebuddy_recovery import completed_directory_probe
        result=completed_directory_probe(stdout,workspace,stderr=stderr,exit_code=exit_code) if workspace is not None else None
        if result is None:raise ValueError('CodeBuddy CLI 未返回完整、唯一且成功的任务结果。') from None
    return dict(result,executor='codebuddy',resume_available=False)
