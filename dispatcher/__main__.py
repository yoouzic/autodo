"""Local command entry point; no credentials are read by this module."""
from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit

from .adapters import Dida, codex_command, dida_command, run_process
from .core import Store
from .execution_settings import load_zcode_catalog, load_doubao_catalog, resolve_execution_settings, validate_execution_settings
from .dashboard import public_snapshot, start_dashboard
from .executors import discover_executors
from .external import validate_antigravity_proxy_url
from .routing import EXECUTORS
from .service import InstanceLock, Service


DEFAULTS = {
    'eligibility_tag': 'AI', 'project_ids': [], 'repositories': {},
    'max_concurrent': 1, 'poll_seconds': 20, 'task_timeout_seconds': 1800,
    'daily_max_runs': 10, 'max_attempts': 2,
    'model': 'gpt-6.1-sol', 'models': {},
    'efforts': {'easy': 'low', 'medium': 'medium', 'hard': 'high'},
    'review_effort': 'medium', 'auto_complete': True, 'write_comments': True, 'require_human_review': True,
    'windows_sandbox': 'elevated',
    'enabled_executors': ['codex'], 'executor_commands': {}, 'executor_routes': {}, 'agent_settings': {},
    'codebuddy_model_timeouts': {},
    'agent_timeouts': {},
    'antigravity_proxy_url': '',
    'workbuddy_transport': 'cli', 'doubao_transport': 'cli',
    'repository_checks': {},
    'workspace_discovery': {'native_agents': True, 'collections': []},
    'remote_access': {'enabled': False, 'mode': 'tailscale', 'port': 8766, 'origin': '', 'allowed_users': [],
                      'proxy_secret_file': 'data/relay-proxy.key', 'tunnel_enabled': False,
                      'ssh_alias': 'autodo-relay', 'relay_port': 18766},
}


def _merge(base, overlay):
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def load_config(root):
    config = copy.deepcopy(DEFAULTS)
    for name in ('config.example.json', 'config.local.json'):
        path = Path(root)/name
        if path.is_file():
            value = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(value, dict):
                raise ValueError(f'{name} must contain a JSON object')
            _merge(config, value)
    for key, minimum, maximum in (
        ('max_concurrent', 1, 4), ('poll_seconds', 10, None),
        ('task_timeout_seconds', 30, None), ('daily_max_runs', 1, None),
        ('max_attempts', 1, None),
    ):
        value = config.get(key)
        if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
            raise ValueError(f'{key} must be an integer >= {minimum}' + (f' and <= {maximum}' if maximum else ''))
    for key in ('auto_complete', 'write_comments', 'require_human_review'):
        if type(config.get(key)) is not bool:
            raise ValueError(f'{key} must be true or false')
    validate_antigravity_proxy_url(config.get('antigravity_proxy_url'))
    from .codebuddy import runtime_limits
    runtime_limits(config,{})
    from .execution_timeouts import resolve as resolve_timeouts
    for name in config.get('agent_timeouts',{}):resolve_timeouts(config,name)
    for key in ('workbuddy_transport','doubao_transport'):
        if config.get(key) not in ('cli','mcp','disabled'):
            raise ValueError(key+' must be cli, mcp or disabled')
    remote = config.get('remote_access')
    if not isinstance(remote, dict) or type(remote.get('enabled')) is not bool:
        raise ValueError('remote_access must be an object with boolean enabled')
    if type(remote.get('port')) is not int or not 1024 <= remote['port'] <= 65535 or remote['port'] == 8765:
        raise ValueError('remote_access.port must be a separate port between 1024 and 65535')
    if remote['enabled']:
        if remote.get('mode') == 'tailscale':
            from .remote import RemoteAccess
            RemoteAccess(remote.get('origin'), remote.get('allowed_users'))
        elif remote.get('mode') == 'password_proxy':
            origin = urlsplit(remote.get('origin', ''))
            if origin.scheme != 'https' or not origin.hostname or origin.username or origin.password or origin.path or origin.query or origin.fragment or '*' in origin.netloc:
                raise ValueError('Password proxy requires an exact HTTPS origin')
            if not isinstance(remote.get('proxy_secret_file'), str) or not remote['proxy_secret_file']:
                raise ValueError('proxy_secret_file must be a file path')
            secret = Path(root) / remote['proxy_secret_file']
            if not secret.is_file() or len(secret.read_text(encoding='utf-8').strip()) < 32:
                raise ValueError('Password proxy requires a private proxy secret file')
        else:
            raise ValueError('Unsupported remote_access.mode')
    if type(remote.get('tunnel_enabled')) is not bool:
        raise ValueError('remote_access.tunnel_enabled must be boolean')
    if remote['tunnel_enabled']:
        if not remote['enabled'] or remote['mode'] != 'password_proxy':
            raise ValueError('SSH relay requires enabled password_proxy access')
        from .tunnel import TunnelManager
        TunnelManager(root, ssh_alias=remote['ssh_alias'], remote_port=remote['relay_port'], local_port=remote['port'])
    if config.get('windows_sandbox') not in ('elevated', 'unelevated'):
        raise ValueError('windows_sandbox must be elevated or unelevated')
    if not isinstance(config.get('eligibility_tag'), str) or not isinstance(config.get('project_ids'), list):
        raise ValueError('eligibility_tag must be a string and project_ids must be an array')
    if not all(isinstance(value, str) and value for value in config['project_ids']):
        raise ValueError('project_ids must contain nonempty strings')
    for key in ('repositories', 'models', 'efforts'):
        if not isinstance(config.get(key), dict):
            raise ValueError(f'{key} must be an object')
    for alias, value in config['repositories'].items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'repositories.{alias} must be a path string')
        path = Path(value).expanduser()
        config['repositories'][alias] = str((path if path.is_absolute() else Path(root)/path).resolve())
    for effort in list(config['efforts'].values()) + [config.get('review_effort')]:
        if effort not in ('minimal', 'low', 'medium', 'high', 'xhigh'):
            raise ValueError(f'Invalid reasoning effort: {effort}')
    if 'codex_command' in config:
        command = config['codex_command']
        if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
            raise ValueError('codex_command must be a nonempty argv array')
    enabled = config.get('enabled_executors')
    if not isinstance(enabled, list) or any(name not in EXECUTORS for name in enabled):
        raise ValueError('enabled_executors must be an array of supported executor names')
    commands = config.get('executor_commands')
    if not isinstance(commands, dict):
        raise ValueError('executor_commands must be an object')
    for name, command in commands.items():
        if name not in EXECUTORS:
            raise ValueError(f'executor_commands has unsupported executor: {name}')
        if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg.strip() for arg in command):
            raise ValueError(f'executor_commands.{name} must be a nonempty argv array')
    routes = config.get('executor_routes')
    if not isinstance(routes, dict):
        raise ValueError('executor_routes must be an object')
    route_keys = {f'{prefix}{difficulty}' for prefix in ('', 'code:', 'document:')
                  for difficulty in ('easy', 'medium', 'hard')}
    for key, route in routes.items():
        if key not in route_keys:
            raise ValueError(f'executor_routes has unsupported route: {key}')
        if not isinstance(route, list) or not route or any(name not in EXECUTORS for name in route):
            raise ValueError(f'executor_routes.{key} must be a nonempty array of supported executor names')
    checks=config.get('repository_checks')
    if not isinstance(checks,dict): raise ValueError('repository_checks must be an object')
    for alias,spec in checks.items():
        if alias not in config['repositories'] or not isinstance(spec,dict):
            raise ValueError('repository_checks must reference a configured repository')
        commands=spec.get('commands')
        if not isinstance(commands,list) or not commands:
            raise ValueError('repository_checks commands must be a nonempty list of argv arrays')
        for argv in commands:
            if not isinstance(argv,list) or not argv or not all(isinstance(arg,str) and arg for arg in argv):
                raise ValueError('Repository check command must be a nonempty argv array')
        if not isinstance(spec.get('report'),str) or not spec['report']:
            raise ValueError('Repository check report must be a relative path')
    discovery=config.get('workspace_discovery')
    if (not isinstance(discovery,dict) or set(discovery)-{'native_agents','collections'} or
            type(discovery.get('native_agents')) is not bool):
        raise ValueError('workspace_discovery requires a boolean native_agents setting')
    from .workspace_collections import validate_collections
    discovery['collections']=validate_collections(discovery.get('collections',[]))
    config['_zcode_model_catalog'] = load_zcode_catalog(root)
    config['_doubao_model_catalog'] = load_doubao_catalog(root)
    from .codebuddy import load_catalog as load_codebuddy_catalog
    config['_codebuddy_model_catalog'] = load_codebuddy_catalog(root)
    if not isinstance(config.get('agent_settings'), dict):
        raise ValueError('agent_settings must be an object')
    for executor in config['agent_settings']:
        if executor in ('workbuddy','doubao') and config.get(executor+'_transport')=='disabled':
            # Preserve dormant settings without making an inactive provider usable.
            validate_execution_settings(executor, config['agent_settings'][executor])
        else:
            resolve_execution_settings(executor, config, {}, {}, 'medium')
    return config


def make_service(root, config, store, *, execution=True):
    runner = None
    if execution:
        from .runner import MultiAgentRunner
        runner = MultiAgentRunner(root, config)
    return Service(root, config, store, Dida(dida_command(root)), runner)


def detect_agents(config=None, root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    return list(discover_executors(root, config or {}).values())


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def local_status(root, store, config):
    saved = {}
    path = root/'data/status.json'
    if path.is_file():
        try:
            saved = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
    tasks = store.list_tasks()
    counts = {}
    for row in tasks:
        counts[row['state']] = counts.get(row['state'], 0) + 1
    # File presence alone does not establish daemon liveness. Probe its OS lock.
    try:
        with InstanceLock(root):
            running = False
    except RuntimeError:
        running = True
    return public_snapshot(dict(saved, tasks=tasks, counts=counts, running=running,
                                stop_requested=(root/'data/stop.request').exists(),
                                executors=detect_agents(config, root)))


def resolve_task(store, identifier):
    row = store.get(identifier)
    if row:
        return row
    matches = [row for row in store.list_tasks() if row['payload'].get('id') == identifier]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ValueError('Task ID is ambiguous; use projectId:taskId')
    raise ValueError(f'Task not found: {identifier}')


def doctor(root, config):
    def safe_status(value):
        return '\n'.join(line for line in value.splitlines() if 'token' not in line.casefold()).strip()

    success = True
    for label, command in (
        ('DIDA OAuth', lambda: dida_command(root) + ['auth', 'status']),
        ('Codex login', lambda: (config.get('executor_commands',{}).get('codex') or config.get('codex_command') or codex_command()) + ['login', 'status']),
    ):
        try:
            stdout, stderr = run_process(command(), timeout=30)
            message = safe_status(stdout + '\n' + stderr)
            if label == 'DIDA OAuth' and ('已登录' not in message or '未登录' in message):
                success = False
            print(label + ': ' + (message or '登录状态无法确认'))
        except (OSError, RuntimeError, TimeoutError) as exc:
            print(label + ': ' + (safe_status(str(exc)) or '登录状态检查失败'))
            success = False
    return 0 if success else 1


def parser():
    value = argparse.ArgumentParser(description='滴答清单 → 本机多智能体派工器')
    value.add_argument('--root', default=str(Path(__file__).resolve().parent.parent), help='项目根目录')
    commands = value.add_subparsers(dest='command', required=True)
    for name in ('doctor', 'auth', 'sync', 'status', 'run-once', 'daemon', 'stop', 'retry', 'agents', 'resume', 'mobile-setup'):
        command = commands.add_parser(name)
        command.add_argument('--root', default=argparse.SUPPRESS)
        if name in ('retry', 'resume'):
            command.add_argument('id', help='projectId:taskId，或唯一的 taskId')
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    try:
        if args.command == 'stop':
            (root/'data').mkdir(parents=True, exist_ok=True)
            (root/'data/stop.request').touch()
            print('已请求优雅停止；正在执行的任务结束后退出。')
            return 0
        config = load_config(root)
        if args.command == 'doctor':
            return doctor(root, config)
        if args.command == 'auth':
            return subprocess.call(dida_command(root) + ['auth', 'login'], cwd=root)
        if args.command == 'agents':
            print_json(detect_agents(config, root))
            return 0
        if args.command == 'mobile-setup':
            from .web_auth import PasswordAccess
            remote = config['remote_access']
            if not remote['enabled'] or remote['mode'] != 'password_proxy':
                raise ValueError('Enable password_proxy remote access first')
            access = PasswordAccess(remote['origin'], (root/remote['proxy_secret_file']).read_text(encoding='utf-8').strip(), root/'data/web-auth.db')
            print(access.create_setup_link())
            return 0
        store = Store(root/'data/tasks.db')
        if args.command == 'status':
            print_json(local_status(root, store, config))
            return 0
        if args.command == 'resume':
            from .index import workspace_paths
            from .workspace_copy import _check_components, _directory
            row = resolve_task(store, args.id)
            result = row.get('result') or {}
            execution = result.get('execution') or {}
            executor = execution.get('executor') or result.get('executor') or 'codex'
            if executor != 'codex':
                if (execution or result).get('resume_available') is False:
                    print(f'{executor} 此次运行未启用会话持久化，session ID 仅供诊断，不能恢复；产物目录：{row.get("run_dir")}')
                    return 0
                if not row.get('run_dir'):
                    raise ValueError(f'此 {executor} 任务没有运行目录，无法读取 execution-resume.txt')
                run = store.get_run(row['current_run_id']) if row.get('current_run_id') else None
                if isinstance(run, dict) and run.get('workspace_binding') is not None:
                    workspace, _ = workspace_paths(root, row, store=store)
                    _directory(workspace)
                    print(f'原执行目录：{workspace}')
                instructions = Path(row['run_dir'])/'execution-resume.txt'
                _check_components(instructions)
                if not instructions.is_file():
                    raise ValueError(f'此 {executor} 任务没有恢复说明：{instructions}')
                print(instructions.read_text(encoding='utf-8-sig'))
                print(f'请按上述说明在 {executor} 的厂商入口恢复会话。')
                return 0
            thread_id = (execution or result).get('thread_id')
            if not isinstance(thread_id, str) or not thread_id or thread_id.startswith('-'):
                raise ValueError('此任务尚未记录可恢复的 Codex 会话 ID')
            workspace, _ = workspace_paths(root, row, store=store)
            _directory(workspace)
            print('正在可见终端恢复会话。进入 Codex TUI 后输入 /app，可交接到 Codex App。', flush=True)
            return subprocess.call((config.get('executor_commands',{}).get('codex') or config.get('codex_command') or codex_command()) + ['resume', thread_id], cwd=workspace)
        if args.command == 'daemon':
            from .control import Controller
            from .workspace import WorkspaceManager
            service = make_service(root, config, store)
            executors = detect_agents(config, root)
            workspaces = WorkspaceManager(root, store)
            workspaces.import_history()
            service.control = Controller(service, workspaces, executors)
            with ExitStack() as servers:
                for provider in ('workbuddy','doubao'):
                    if config.get(provider+'_transport')=='mcp':
                        from .workbuddy_mcp import WorkBuddyBroker
                        broker=WorkBuddyBroker(root,provider=provider).start()
                        servers.callback(broker.close)
                        setattr(service.runner,provider+'_broker',broker)
                snapshot = lambda: dict(service.snapshot(), running=True, executors=executors)
                servers.enter_context(start_dashboard(snapshot, control=service.control))
                remote = config['remote_access']
                if remote['enabled']:
                    if remote['mode'] == 'password_proxy':
                        from .web_auth import PasswordAccess
                        access = PasswordAccess(remote['origin'], (root/remote['proxy_secret_file']).read_text(encoding='utf-8').strip(), root/'data/web-auth.db')
                    else:
                        from .remote import RemoteAccess
                        access = RemoteAccess(remote['origin'], remote['allowed_users'])
                    service.control.remote_access = access
                    servers.enter_context(start_dashboard(snapshot, port=remote['port'], control=service.control, remote=access))
                    if remote['tunnel_enabled']:
                        from .tunnel import TunnelManager
                        service.tunnel = TunnelManager(root, ssh_alias=remote['ssh_alias'], remote_port=remote['relay_port'], local_port=remote['port'])
                    print('Private mobile entry: ' + remote['origin'], flush=True)
                print('派工台：http://127.0.0.1:8765', flush=True)
                service.daemon()
            return 0
        with InstanceLock(root):
            if args.command == 'retry':
                print_json(store.retry(resolve_task(store, args.id)['id']))
                return 0
            service = make_service(root, config, store, execution=args.command == 'run-once')
            if args.command == 'sync':
                print_json(service.sync())
                return 0
            store.recover()
            service.sync()
            service.process_writebacks()
            if store.count_started_today() >= config['daily_max_runs']:
                print('今日运行次数已达上限；没有领取任务。')
                return 0
            row = store.claim()
            if row:
                service.execute(row)
                print_json(store.get(row['id']))
            else:
                print('没有可执行的排队任务。')
            return 0
    except (OSError, ValueError, RuntimeError, KeyError, TimeoutError) as exc:
        print(f'错误：{exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('已中断。', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
