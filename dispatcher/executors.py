"""Vendor runtime discovery and normalized execution reports."""
import json
import os
import re
import shutil
from pathlib import Path

from .adapters import codex_command, parse_json_output
from .routing import EXECUTORS


def parse_result_text(text):
    if isinstance(text, dict):
        result = text.copy()
    elif isinstance(text, str):
        value = text.strip()
        fences=re.findall(r'```(?:json)?\s*\n(.*?)```',value,re.S|re.I)
        if fences:
            if len(fences)!=1: raise ValueError('Agent returned multiple JSON blocks')
            value=fences[0].strip()
        result = json.loads(value)
    else:
        raise ValueError('Agent did not return a structured business report')
    if not isinstance(result, dict) or result.get('status') not in ('complete','needs_input','failed'):
        raise ValueError('Agent report has no valid status')
    for key in ('summary','needs_input'):
        if not isinstance(result.get(key), str):
            raise ValueError(f'Agent report requires string {key}')
    for key in ('artifacts','checks'):
        if not isinstance(result.get(key),list) or not all(isinstance(item,str) for item in result[key]):
            raise ValueError(f'Agent report requires string array {key}')
    return result


def normalize_result(executor, stdout, *, stderr='', workspace=None):
    if executor=='codebuddy':
        from .codebuddy import normalize_result as normalize_codebuddy
        return normalize_codebuddy(stdout,stderr=stderr,workspace=workspace)
    if executor=='workbuddy':
        from .workbuddy import normalize_result as normalize_workbuddy
        return normalize_workbuddy(stdout,stderr=stderr)
    data = parse_json_output(stdout)
    session_id = None
    if executor == 'dsh':
        events = data if isinstance(data,list) else [data]
        final = None
        completed = False
        for event in events:
            if not isinstance(event,dict): raise ValueError('Invalid DSH event')
            if event.get('type')=='session': session_id=event.get('sessionId')
            if event.get('type')=='error': raise ValueError('DSH reported an error')
            if event.get('type')=='status' and event.get('phase')=='turn_end':
                reason=event.get('reason')
                kind=reason.get('kind') if isinstance(reason,dict) else reason
                if kind!='completed': raise ValueError('DSH turn did not complete: '+str(kind))
                completed=True
            if event.get('type')=='final': final=event.get('text')
        if not completed: raise ValueError('DSH terminal completion event missing')
        result = parse_result_text(final)
    elif executor == 'zcode':
        if not isinstance(data,dict): raise ValueError('ZCode summary must be an object')
        if data.get('error'): raise ValueError('ZCode reported an error')
        session_id=data.get('sessionId')
        result=parse_result_text(data.get('response'))
    elif executor=='antigravity':
        if isinstance(data,list):
            terminals=[e.get('result') for e in data if isinstance(e,dict) and e.get('event')=='result']
            if not terminals: raise ValueError('Antigravity terminal result missing')
            data=terminals[-1]
        if not isinstance(data,dict) or data.get('status')!='SUCCESS':
            raise ValueError('Antigravity runtime did not report SUCCESS')
        session_id=data.get('conversation_id')
        canonical=data.get('structured_output')
        result=parse_result_text(canonical) if canonical is not None else None
        if not data.get('response') and data.get('denied_actions'):
            result={'status':'needs_input','summary':'Antigravity stopped at a tool permission request',
                    'artifacts':[],'checks':[],'needs_input':'Headless permission denied: '+json.dumps(data['denied_actions'],ensure_ascii=False)}
        elif result is None:
            result=parse_result_text(data.get('response'))
    elif executor=='qoder':
        events=data if isinstance(data,list) else [data]
        finals=[e for e in events if isinstance(e,dict) and (e.get('type')=='result' or 'result' in e)]
        if not finals: raise ValueError(f'{executor} did not return a terminal result')
        final=finals[-1]
        if final.get('is_error') or final.get('error') or final.get('subtype') not in (None,'success'):
            raise ValueError(f'{executor} reported an error')
        session_id=final.get('session_id') or final.get('sessionId')
        result=parse_result_text(final.get('result'))
    else:
        raise ValueError(f'Unsupported report parser: {executor}')
    result.update(executor=executor,session_id=session_id)
    return result


def sandbox_command(codex, command, workspace, sandbox='unelevated', state_roots=(), *, readonly=False):
    overrides=[]
    profile=':read-only' if readonly else ':workspace'
    if state_roots:
        # :workspace already grants these descendants. Re-encoding them as
        # filesystem keys misreads literal brackets in task titles as globs.
        base=Path(workspace).resolve()
        additional=[Path(p) for p in state_roots if readonly or not Path(p).resolve().is_relative_to(base)]
        filesystem=','.join(json.dumps(str(p).replace('\\','/'))+'="write"' for p in additional)
        overrides=['-c','permissions.dispatcher={extends='+json.dumps(profile)+',network={enabled=true},filesystem={'+filesystem+'}}']
        profile='dispatcher'
    return list(codex)+overrides+['-c',f'windows.sandbox="{sandbox}"','-c','sandbox_mode="read-only"' if readonly else 'sandbox_mode="workspace-write"',
                        '-c','approval_policy="never"','sandbox','--permission-profile',profile,
                        '-C',str(workspace),'--']+list(command)


def discover_executors(root, config):
    root=Path(root)
    roaming=Path(os.environ.get('APPDATA',str(Path.home()/'AppData/Roaming')))
    local=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData/Local')))
    node=shutil.which('node')
    paths={
        'dsh':roaming/'npm/node_modules/@deepseek-ai/dsh/lib/bin.js',
        'zcode':local/'Programs/ZCode/resources/glm/zcode.cjs',
        'qoder':root/'.tools/providers/qoder/qodercli.exe',
        'antigravity':root/'.tools/providers/antigravity/agy.exe',
        'workbuddy':local/'Programs/WorkBuddy/resources/app.asar.unpacked/cli/bin/codebuddy',
        'codebuddy':roaming/'npm/node_modules/@tencent-ai/codebuddy-code/bin/codebuddy',
        'doubao':root/'.tools/providers/doubao/2.31.3/doubao-cli-2.31.3-win-x64/doubao.exe',
    }
    commands={}
    try: commands['codex']=config.get('codex_command') or codex_command()
    except FileNotFoundError: commands['codex']=[]
    for name,path in paths.items():
        commands[name]=([node,str(path)] if name in ('dsh','zcode','workbuddy','codebuddy') and node else [str(path)]) if path.is_file() else []
    for name,command in config.get('executor_commands',{}).items(): commands[name]=command
    enabled=config.get('enabled_executors',['codex'])
    result={}
    for name in EXECUTORS:
        command=commands.get(name,[])
        detected=bool(command) and (Path(command[0]).is_file() or bool(shutil.which(command[0])))
        if detected and len(command)>1 and (Path(command[1]).suffix in ('.js','.cjs') or name in ('workbuddy','codebuddy')):
            detected=Path(command[1]).is_file()
        active=detected and name in enabled
        status='已启用' if active else ('已安装，未启用/待验证' if detected else '未发现独立 CLI')
        if name=='workbuddy' and detected and not active:
            status='已检测，待宿主认证验证'
        if name=='doubao' and detected and not active:
            status='内部集成 CLI，待账号兼容性验证'
        result[name]={'name':name,'command':command,'detected':detected,'enabled':active,
                      'status':status}
        if name in ('workbuddy','doubao') and config.get(name+'_transport')=='disabled':
            result[name].update(enabled=False,transport='disabled',status='已停用')
    return result
