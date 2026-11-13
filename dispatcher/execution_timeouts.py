"""Validated, invocation-local deadlines. Unknown vendor controls stay unknown."""
import copy,json,re
from pathlib import Path

FIELDS={
 'codex':{'total_seconds','stream_idle_seconds'},
 'codebuddy':{'total_seconds','first_token_seconds','stream_idle_seconds','recovery_first_token_seconds'},
 'dsh':{'total_seconds','stream_idle_seconds'},
 'zcode':{'total_seconds','http_request_seconds','stream_idle_seconds'},
 'qoder':{'total_seconds'},'antigravity':{'total_seconds'},
 'workbuddy':{'total_seconds'},'doubao':{'total_seconds'},
}

def validate(config):
    policies=config.get('agent_timeouts',{})
    if not isinstance(policies,dict) or set(policies)-set(FIELDS):raise ValueError('Unknown agent timeout policy')
    for name,policy in policies.items():
        if not isinstance(policy,dict) or set(policy)-FIELDS[name]:raise ValueError(f'Unsupported timeout controls for {name}')
        for key,value in policy.items():
            if type(value) is not int or not 30<=value<=86400:raise ValueError('Timeouts must be integer seconds between 30 and 86400')
        if config.get(name+'_transport')=='mcp' and policy.get('total_seconds',600)>3600:
            raise ValueError('Desktop MCP task lease cannot exceed 3600 seconds')
    legacy=config.get('codebuddy_model_timeouts',{})
    if not isinstance(legacy,dict):raise ValueError('codebuddy_model_timeouts must be an object')
    for name,value in legacy.items():
        if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}',name) or type(value) is not int or not 30<=value<=86400:
            raise ValueError('CodeBuddy model timeouts must be 30–86400 seconds')

def resolve(config,executor,settings=None):
    validate(config)
    if executor not in FIELDS:raise ValueError('Unsupported executor')
    limits={'total_seconds':config.get('task_timeout_seconds',1800 if executor=='codex' else 600)}
    if executor=='codebuddy':
        limits['total_seconds']=config.get('codebuddy_model_timeouts',{}).get((settings or {}).get('model'),limits['total_seconds'])
        limits.update(first_token_seconds=180,stream_idle_seconds=180,request_retries=1)
    limits.update(config.get('agent_timeouts',{}).get(executor,{}))
    # Legacy short test/CLI budgets remain valid; explicit policies must compose.
    if executor in config.get('agent_timeouts',{}):
        if any(v>limits['total_seconds'] for k,v in limits.items() if k in {'first_token_seconds','http_request_seconds','stream_idle_seconds','recovery_first_token_seconds'}):
            raise ValueError('Outer task deadline must cover each configured model wait')
        if limits.get('recovery_first_token_seconds',0)>limits.get('first_token_seconds',limits['total_seconds']):
            raise ValueError('Recovery first-output deadline cannot exceed normal first-output deadline')
    return limits

def freeze(row,run_dir,executor,config,settings):
    limits=resolve(config,executor,settings)
    row['_execution_limits']=copy.deepcopy(limits)
    (Path(run_dir)/'execution-limits.json').write_text(json.dumps(limits),encoding='utf-8')
    return limits

def snapshot(row,run_dir,executor,config,settings):
    path=Path(run_dir)/'execution-limits.json'
    if '_execution_limits' not in row:
        if path.exists():raise ValueError('Cannot adopt unbound runtime limits')
        return freeze(row,run_dir,executor,config,settings)
    limits=row['_execution_limits']
    if not path.is_file() or json.loads(path.read_text(encoding='utf-8'))!=limits:raise ValueError('Runtime limits changed after prepare')
    return copy.deepcopy(limits)

def codex_options(limits):
    seconds=limits.get('stream_idle_seconds')
    if seconds is None:return []
    # 0.162.1 reserves built-in IDs. A process-local official-provider alias
    # retains native OpenAI auth and endpoint selection; no global config writes.
    return ['-c','model_provider="dispatcher_openai"','-c',
            'model_providers.dispatcher_openai={name="OpenAI",requires_openai_auth=true,supports_websockets=true,stream_idle_timeout_ms='+str(seconds*1000)+'}']
