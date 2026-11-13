"""Process-local provider configuration; shared workspace sandbox remains outside."""
import json
import re
import sys
from pathlib import Path
from .execution_settings import strip_model_options
from .execution_timeouts import resolve as resolve_timeouts


def validate_antigravity_proxy_url(value):
    """Only an explicit local HTTP proxy; never accept credentials or URL extras."""
    if value == '':
        return value
    match = re.fullmatch(r'http://(?:127\.0\.0\.1|localhost|\[::1\]):([0-9]{1,5})', value) if isinstance(value, str) else None
    if not match or not 1 <= int(match.group(1)) <= 65535:
        raise ValueError('antigravity_proxy_url must be empty or http://loopback:port '
                         '(127.0.0.1, localhost or [::1]); credentials, paths and URL parameters are not allowed')
    return value


def invocation(name, command, workspace, run_dir, prompt, config, kind='document', *, settings=None, runtime_dir=None, run_id=None, answer_mode=False, permissions=None, limits=None):
    workspace,run_dir=Path(workspace).resolve(),Path(run_dir).resolve()
    limits=limits or resolve_timeouts(config,name,settings)
    if answer_mode and name != 'codebuddy':
        raise ValueError('This external executor does not support text answers')
    runtime=Path(runtime_dir).resolve() if runtime_dir is not None else workspace/'.runtime'
    if runtime_dir is not None:
        from .workspace_copy import _check_components
        _check_components(runtime)
        if runtime != run_dir/'runtime':
            raise ValueError('External runtime must use this run runtime directory')
    runtime.mkdir(exist_ok=True)
    inputs=runtime if runtime_dir is not None else run_dir
    if name=='doubao':
        from .doubao import build_invocation
        return build_invocation(command,workspace,run_dir,prompt,settings or {},runtime,
                                run_id=run_id,
                                max_age_seconds=min(86400,max(60,int(limits['total_seconds'])+120)))
    if name=='workbuddy':
        from .workbuddy import build_invocation
        from .runner import RESULT_SCHEMA
        return build_invocation(command,workspace,run_dir,prompt,settings or {},runtime,schema=RESULT_SCHEMA)
    if name=='codebuddy':
        from .codebuddy import build_invocation
        from .runner import RESULT_SCHEMA
        from .result_protocol import ANSWER_SCHEMA
        return build_invocation(command,workspace,run_dir,prompt,settings or {},runtime,
                                schema=ANSWER_SCHEMA if answer_mode else RESULT_SCHEMA,readonly=answer_mode,permissions=permissions,limits=limits)
    state_roots=[]
    env={'CI':'1'}
    stdin=None
    if settings is not None and name in ('qoder', 'antigravity'):
        command = strip_model_options(command, name)
        if settings.get('cli_model'):
            command += ['--model', settings['cli_model']]
        if settings.get('effort'):
            command += ['--effort' if name == 'antigravity' else '--reasoning-effort', settings['effort']]
    if name=='dsh':
        patch=[{'id':'sandbox-policy','config':{'mode':'workspace-write','workspaceRoot':str(workspace)}},
               {'id':'approval','config':{'policy':'never'}},
               {'id':'permission','config':{'defaultPreset':'dispatcher','presets':{'dispatcher':{'sandbox':'workspace-write','approval':'never'}}}},
               {'id':'session-persistence-jsonl','config':{'root':str(runtime/'sessions')}},
               {'id':'storage-json','config':{'root':str(runtime/'storages')}},
               {'id':'credentials','config':{'watch':False}}]
        for plugin in ('rea-mcp','tool-pwsh','tool-web','tool-subagent','tool-subagent-fork','tool-workflow','tool-skill','session-title-llm'):
            patch.append({'id':plugin,'disabled':True})
        selected = {}
        if settings and settings.get('model'): selected['model'] = settings['model']
        if settings and settings.get('effort'): selected['reasoningEffort'] = settings['effort']
        if selected:
            if not settings.get('provider'):
                raise ValueError('DSH model override requires a verified provider')
            selected['provider'] = settings['provider']
            patch.append({'id':'agent-default-model','config':selected})
        if limits.get('stream_idle_seconds'):
            milliseconds=limits['stream_idle_seconds']*1000
            patch += [{'id':identifier,'config':{'streamIdleTimeoutMs':milliseconds}}
                      for identifier in ('llm-deepseek','llm-deepseek-account','llm-pi-ai')]
            command=list(command)
            command[1:1]=['--require',str(Path(__file__).with_name('node_model_timeouts.cjs').resolve())]
            env['DISPATCHER_STREAM_IDLE_MS']=str(milliseconds)
        patch_file=inputs/'dsh.patch.json'
        # JSON is a strict YAML subset, accepted by the vendor patch loader.
        patch_file.write_text(json.dumps(patch,ensure_ascii=False,indent=2),encoding='utf-8')
        args=list(command)+['--profile','headless','--patch',str(patch_file),'--json','-']
        stdin=prompt
        env['DSH_TELEMETRY_MODE']='DISABLED'
        state_roots=[Path.home()/'.dsh/profiles/headless']
    elif name=='zcode':
        if len(prompt)>20000: raise ValueError('ZCode prompt exceeds Windows command-line limit; shorten task context')
        package=Path(command[-1]).parent.parent
        env.update(ZCODE_STORAGE_DIR=str(runtime),ZCODE_LOG_DIR=str(runtime/'logs'),
                   ZCODE_MODEL_TELEMETRY_ENABLED='false',
                   ZCODE_BUILTIN_PROVIDER_CONFIG_FILE=str(package/'config/provider/zcode-builtin.json'),
                   ZCODE_PERSONAL_PROVIDER_CONFIG_FILE=str(Path.home()/'.zcode/v2/provider_config.json'))
        if limits.get('http_request_seconds'):env['ZCODE_HTTP_TIMEOUT']=str(limits['http_request_seconds']*1000)
        args=list(command)+['--cwd',str(workspace),'--mode','edit','--workflow-mode','disabled',
                            '--toolsearch-mode','off','--disallowed-tools',
                            'Bash,Shell,PowerShell,NodeREPL,WebSearch,WebFetch,Task,Agent',
                            '--json','--no-color','--prompt',prompt]
        if settings and (settings.get('model') or settings.get('effort')):
            command_file = inputs/'zcode-command.json'
            request_file = inputs/'zcode-request.json'
            command_file.write_text(json.dumps(list(command), ensure_ascii=False), encoding='utf-8')
            request_file.write_text(json.dumps({'workspace':str(workspace), 'prompt':prompt,
                'model':settings.get('model'), 'effort':settings.get('effort'),
                'timeout_seconds':limits['total_seconds']}, ensure_ascii=False), encoding='utf-8')
            args = [sys.executable, str(Path(__file__).with_name('zcode_session.py').resolve()),
                    '--command-json', str(command_file), '--request', str(request_file)]
        state_roots=[Path.home()/'.zcode/cli']
    elif name=='qoder':
        if len(prompt)>20000: raise ValueError('Qoder prompt exceeds Windows command-line limit')
        tools='Read,Write,Edit,Glob,Grep' if kind=='code' else 'Read,Write'
        args=list(command)+['--tools',tools,'--strict-mcp-config','--mcp-config','{"mcpServers":{}}',
                            '--setting-sources','','--settings','{"general":{"enableAutoUpdate":false}}',
                            '--no-session-persistence','--permission-mode','accept_edits',
                            '--max-model-request-retries','0','--output-format','json']
        args+=['-p',prompt]
        state_roots=[Path.home()/'.qoder'/part for part in ('cache','.cache','logs','tmp','security','shell-snapshots','.bin')]
    elif name=='antigravity':
        if len(prompt)>20000: raise ValueError('Antigravity prompt exceeds Windows command-line limit')
        proxy=validate_antigravity_proxy_url(config.get('antigravity_proxy_url',''))
        if proxy:
            env.update(HTTP_PROXY=proxy,HTTPS_PROXY=proxy,NO_PROXY='localhost,127.0.0.1,::1')
        settings={'toolPermission':'proceed-in-sandbox','allowNonWorkspaceAccess':False,'enableTerminalSandbox':True}
        schema=run_dir/'result.schema.json'
        if runtime_dir is not None:
            schema=runtime/'result.schema.json'
            schema.write_bytes((run_dir/'result.schema.json').read_bytes())
        args=list(command)+['--sandbox','--mode','accept-edits','--settings',json.dumps(settings),
                            '--print-timeout',str(limits['total_seconds'])+'s',
                            '--log-file',str(runtime/'antigravity.log'),
                            '--json-schema',str(schema),
                            '--output-format','stream-json','-p',prompt]
        state_roots=[Path.home()/'.gemini/antigravity-cli']
    else:
        raise ValueError(f'Unsupported external executor: {name}')
    if runtime_dir is not None:
        state_roots.append(runtime)
    return args,env,stdin,state_roots
