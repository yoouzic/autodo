"""Local command boundary and the control-center projection."""
from __future__ import annotations

from pathlib import Path

from .core import StateConflict, classify, manifest_digest
from .routing import EXECUTORS, RoutingError, choose_executor, task_available_executors
from .execution_settings import agent_capabilities, resolve_execution_settings, validate_execution_settings
from .control_store import validate_workspace_choice
from .workspace_layout import task_summary
from .result_protocol import answer_projection, validate_mode, resolve_delivery, ANSWER_EXECUTORS
from .task_inputs import INPUT_ACTIONS, local_reply, validate_input, validate_continuation, questions_for
from . import permissions
from .execution_timeouts import resolve as resolve_timeouts


def public_run(run, *, full=False):
    result = run.get('result') or {}
    public = {key: value for key, value in run.items() if key not in {'result', 'payload', 'input_context'}}
    public['delivery'] = answer_projection(result, full=full)
    public['summary'] = result.get('summary')
    public['execution_limits']=result.get('execution_limits') or (result.get('execution') or {}).get('execution_limits')
    public['checks'] = result.get('checks', []) if full else []
    context = run.get('input_context') or {}
    public['round_input'] = context.get('messages', [])[-1] if full and context.get('messages') else None
    return public


ASSIGNABLE = {'ignored', 'queued', 'failed', 'needs_input', 'needs_review', 'revision_requested', 'cancelled'}
STAGES = {'queued': 'queued', 'running': 'running', 'verifying': 'running',
          'awaiting_review': 'review', 'needs_review': 'review',
          'approved': 'approved', 'writeback_pending': 'approved', 'done': 'done'}
ACTIONS = {'assign', 'withdraw', 'approve', 'reject', 'archive', 'restore', 'open_workspace', 'sync', 'configure_agent'} | INPUT_ACTIONS


class Controller:
    def __init__(self, service, workspaces, agents):
        self.service, self.store, self.workspaces = service, service.store, workspaces
        self.agents = {item['name']: item for item in agents}

    def task(self, row, *, full=False):
        result, payload = row.get('result') or {}, row['payload']
        source_done = row.get('source_status') == 2
        explanation = local_reply(row)
        source_completed = source_done and row['state'] != 'done' and not explanation
        stage = 'done' if source_completed else STAGES.get(row['state'], 'backlog')
        run = self.store.get_run(row['current_run_id']) if row.get('current_run_id') else None
        execution = result.get('execution') or result
        executor = (run or {}).get('executor') or execution.get('executor')
        if not executor and execution.get('thread_id'):
            executor = 'codex'
        route_error, recommendation, planned_route = None, None, None
        if row['state'] != 'done' and not source_completed:
            try:
                planned_route = choose_executor(payload, row['difficulty'], row['kind'], self.service.config,
                    task_available_executors(row, self.service.config, self.agents),
                    manual_executor=row.get('manual_executor') if row.get('delegated') else None)
                recommendation = planned_route['executor']
                if not executor and row['state'] in {'queued', 'running', 'verifying'}:
                    executor = recommendation
            except RoutingError as exc:
                route_error = str(exc)
        if stage == 'backlog' and not row.get('delegated'):
            executor = None
        review = (run or {}).get('review_status')
        continuation = self.continuation(row, run, full=full)
        runtime_progress=None
        if executor=='codebuddy' and row['state']=='running' and row.get('run_dir'):
            from .codebuddy import progress
            runtime_progress=progress(row['run_dir'])
        return {
            **{key: row.get(key) for key in ('id', 'priority', 'difficulty', 'kind', 'state', 'revision',
                'fingerprint', 'current_run_id', 'manual_executor', 'repo_alias', 'attempts', 'error', 'feedback',
                'source_status', 'source_checked_at')},
            'source_completed': source_completed,
            'source_writeback_allowed': not explanation,
            'input_version': row.get('input_version', 1),
            'round_number': (run or {}).get('round_number'),
            'continuation': continuation,
            'runtime_progress':runtime_progress,
            'title': payload.get('title', ''),
            'description': payload.get('content') or payload.get('desc') or '',
            'start_date': payload.get('startDate'), 'due_date': payload.get('dueDate'),
            'is_all_day': payload.get('isAllDay') is True, 'time_zone': payload.get('timeZone'),
            'stage': stage, 'executor': executor,
            'recommended_executor': recommendation,
            'execution_overrides': row.get('execution_overrides') or {},
            'delivery_mode': row.get('delivery_mode', 'auto'),
            'delivery': answer_projection(result, full=full),
            'execution_settings': (run or {}).get('execution_settings'),
            'permission_profile': row.get('permission_profile','default'),
            'permissions': (run or {}).get('permissions'),
            'execution_limits': execution.get('execution_limits'),
            'workspace_choice': row.get('workspace_choice') or {'mode':'default'},
            'workspace_suggested_name': task_summary(payload),
            'workspace_binding': (run or {}).get('workspace_binding'),
            'manifest_digest': manifest_digest(result), 'review_status': review,
            'review_note': (run or {}).get('review_note'),
            'auto_review_status': None if source_completed else 'passed' if row['state'] in {'awaiting_review', 'approved', 'writeback_pending', 'done', 'revision_requested'}
                                  else ('failed' if row['state'] == 'needs_review' else None),
            'workspace': (run or {}).get('artifact_root') or (str(Path(row['run_dir']) / 'workspace') if row.get('run_dir') else None),
            'can_assign': not source_done and row['state'] in ASSIGNABLE,
            'can_approve': (not source_done or explanation) and row['state'] == 'awaiting_review' and bool(manifest_digest(result)),
            'can_reject': (not source_done or explanation) and row['state'] in {'awaiting_review', 'needs_review'} and bool(run),
            'can_withdraw': not source_done and row['state'] in ASSIGNABLE - {'needs_review', 'cancelled'},
            'summary': result.get('summary'), 'checks': result.get('checks', []),
            'runtime_warning': ('Antigravity 原始状态为 ERROR（EOF）。已恢复完成产物；'
                                '只有独立验收通过后才可人工批准。'
                                if (execution.get('runtime_warning') or {}).get('kind') == 'completed_eof' else
                                'CodeBuddy 曾用 Read 探测目录；后续文件写入和回读成功。原错误保留，仍需独立验收与人工复审。'
                                if (execution.get('runtime_warning') or {}).get('kind') == 'completed_directory_probe' else None),
            'route_reason': (execution.get('route') or {}).get('reason') or route_error or
                            (planned_route or {}).get('reason') or row.get('reason'),
        }

    def continuation(self, row, run, *, full=False):
        if not run: return {'available': False}
        try:
            validate_continuation(row, run, 'save_input_draft', '', {})
        except (ValueError, KeyError, TypeError):
            return {'available': False}
        info = {'available': True, 'executor': run['executor'], 'parent_run_id': run['id'],
                'action': 'respond_input' if row['state'] == 'needs_input' else 'follow_up',
                'explanation_only': row.get('source_status') == 2 or local_reply(row),
                'settings': run['execution_settings'],
                'ready': bool(self.agents.get(run['executor'], {}).get('enabled'))}
        if full: info['questions'] = questions_for(run.get('result'))
        return info

    def workspace_catalog(self, *, force=False):
        policy=getattr(self.service,'workspace_policy',None)
        return policy.list(force=force) if policy else {'items':[],'warnings':[]}

    def state(self):
        from .workbuddy_runner import refresh_worker_registry
        self.agents = refresh_worker_registry(self.agents, self.service.config, getattr(self.service, 'runner', None))
        tasks = [self.task(row) for row in self.store.list_tasks()]
        agents = []
        for name in EXECUTORS:
            agent = self.agents.get(name, {})
            saved = self.store.get_agent_settings(name)
            configuration_error = None
            try:
                effective = resolve_execution_settings(name, self.service.config, saved['settings'], {}, 'medium')
            except ValueError as exc:
                effective = None
                configuration_error = str(exc)
            agents.append({'id': name, 'name': name, 'enabled': bool(agent.get('enabled')) and not configuration_error,
                'status': configuration_error or agent.get('status', '未发现独立 CLI'),
                'settings': saved['settings'], 'settings_revision': saved['revision'],
                'effective_defaults': effective,
                'capabilities': agent_capabilities(name, self.service.config),
                'permission_capabilities': permissions.capabilities(name),
                'timeout_policy': resolve_timeouts(self.service.config,name,effective),
                'delivery_modes': ['files', 'answer'] if name in ANSWER_EXECUTORS else ['files'],
                'busy': sum(t['stage'] == 'running' and t['executor'] == name for t in tasks),
                'queued': sum(t['stage'] == 'queued' and t['executor'] == name for t in tasks)})
        return {'system': {'running': not self.service.stop.is_set(), 'last_sync': self.service.last_sync,
                'last_error': self.service.last_error, 'review_policy': 'human_required',
                'poll_seconds': self.service.config.get('poll_seconds', 20),
                'syncing': getattr(self.service, 'syncing', False),
                'last_sync_duration_seconds': getattr(self.service, 'last_sync_duration_seconds', None),
                'sync_warnings': getattr(self.service, 'warnings', []),
                'max_concurrent': self.service.config.get('max_concurrent', 1),
                'remote_access': self.remote_access.diagnostics() if getattr(self, 'remote_access', None) else None,
                'tunnel': self.service.tunnel.snapshot() if self.service.tunnel is not None else None},
            'tasks': tasks, 'agents': agents, 'runs': [public_run(run) for run in self.workspaces.list_runs()],
            'delivery_modes': ['auto', 'answer', 'files'],
            'workspace_catalog': self.workspace_catalog(),
            'workspace_policy': {'reuse_strategy':'in_place','managed_root':str(Path(self.service.root)/'workspaces')
                                 if getattr(self.service,'root',None) else ''},
            'repositories': [{'alias': alias, 'path': path} for alias, path in self.service.config.get('repositories', {}).items()],
            'events': self.store.events(50)}

    def detail(self, task_id, run_id=None):
        row = self.store.get(task_id)
        if not row:
            raise KeyError(task_id)
        selected = run_id or row.get('current_run_id')
        runs = self.workspaces.list_runs(task_id)
        if run_id and not any(run['id'] == run_id for run in runs):
            raise KeyError('Run does not belong to this task')
        task = self.task(row, full=True)
        return {'task': task,
                'runs': [public_run(run, full=run['id'] == selected) for run in runs],
                'input_draft': self.store.input_draft(task_id, row.get('current_run_id')) if task['continuation']['available'] else {'revision': 0, 'message': '', 'answers': {}},
                'events': [e for e in self.store.events(500) if e.get('task_id') == task_id]}

    def submit(self, body):
        from .workbuddy_runner import refresh_worker_registry
        self.agents=refresh_worker_registry(self.agents,self.service.config,getattr(self.service,'runner',None))
        if not isinstance(body, dict) or body.get('action') not in ACTIONS:
            raise ValueError('Unsupported action')
        action = body['action']
        allowed = {'action', 'request_id'} | {
            'assign': {'task_id', 'revision', 'executor', 'repo_alias', 'execution_overrides', 'workspace_choice', 'delivery_mode', 'permission_profile', 'full_access_ack'},
            'configure_agent': {'executor', 'revision', 'settings'},
            'follow_up': {'task_id', 'revision', 'run_id', 'fingerprint', 'message', 'answers', 'draft_revision'},
            'respond_input': {'task_id', 'revision', 'run_id', 'fingerprint', 'message', 'answers', 'draft_revision'},
            'save_input_draft': {'task_id', 'revision', 'run_id', 'fingerprint', 'message', 'answers', 'draft_revision'},
            'withdraw': {'task_id', 'revision'},
            'approve': {'task_id', 'revision', 'run_id', 'fingerprint', 'manifest_digest', 'note'},
            'reject': {'task_id', 'revision', 'run_id', 'fingerprint', 'manifest_digest', 'note'},
            'archive': {'run_id'}, 'restore': {'run_id'}, 'open_workspace': {'run_id'}, 'sync': set(),
        }[action]
        if set(body) - allowed:
            raise ValueError('Unexpected action fields')
        for key in ('request_id',):
            if not isinstance(body.get(key), str) or not 1 <= len(body[key]) <= 128:
                raise ValueError(f'{key} is required')
        if action in {'assign', 'withdraw', 'approve', 'reject'} | INPUT_ACTIONS:
            if not isinstance(body.get('task_id'), str) or not body['task_id']:
                raise ValueError('task_id is required')
            if type(body.get('revision')) is not int:
                raise ValueError('revision is required')
        if action == 'configure_agent':
            if type(body.get('revision')) is not int or body['revision'] < 0:
                raise ValueError('A nonnegative agent settings revision is required')
            self._validate_agent_settings(body.get('executor'), body.get('settings'))
        if action in {'archive', 'restore', 'open_workspace', 'approve', 'reject'} | INPUT_ACTIONS:
            if not isinstance(body.get('run_id'), str) or not body['run_id']:
                raise ValueError('run_id is required')
        if action in INPUT_ACTIONS:
            validate_input(body.get('message'), body.get('answers'))
            if not isinstance(body.get('fingerprint'), str) or not body['fingerprint']:
                raise ValueError('fingerprint is required')
            if action == 'save_input_draft' and (type(body.get('draft_revision')) is not int or body['draft_revision'] < 0):
                raise ValueError('draft_revision is required')
            if 'draft_revision' in body and (type(body['draft_revision']) is not int or body['draft_revision'] < 0):
                raise ValueError('Invalid draft_revision')
            payload = {k: v for k, v in body.items() if k not in ('action', 'request_id')}
            existing = self.store.command_for_request(body['request_id'])
            if existing: return self.store.submit_command(action, payload, body['request_id'])
            self._validate_continuation(body, action)
        if action in {'approve', 'reject'}:
            if not isinstance(body.get('fingerprint'), str) or not body['fingerprint']:
                raise ValueError('fingerprint is required')
            if 'manifest_digest' not in body:
                raise ValueError('manifest_digest is required')
            if 'note' in body and not isinstance(body['note'], str):
                raise ValueError('note must be text')
            if action == 'reject' and not body.get('note', '').strip():
                raise ValueError('Rejection requires feedback')
        if action == 'assign':
            name = body.get('executor')
            if name not in ('auto', *EXECUTORS):
                raise ValueError('Unsupported executor')
            if name != 'auto' and not self.agents.get(name, {}).get('enabled'):
                raise ValueError('Executor is unavailable or disabled')
            alias = body.get('repo_alias')
            if alias is not None and alias not in self.service.config.get('repositories', {}):
                raise ValueError('Repository must be a configured alias')
            row = self.store.get(body['task_id'])
            if row is None:
                raise KeyError(body['task_id'])
            self._validate_assignment_settings(name, row['payload'],
                                              body.get('execution_overrides', {}), alias, body.get('delivery_mode', 'auto'))
            self._validate_workspace_choice(body.get('workspace_choice',{'mode':'default'}), row['payload'], alias, executor=name)
            self._validate_permissions(body,row['payload'])
        payload = {k: v for k, v in body.items() if k not in ('action', 'request_id')}
        return self.store.submit_command(action, payload, body['request_id'])

    def _validate_continuation(self, body, action):
        row = self.store.get(body['task_id'])
        if not row: raise KeyError(body['task_id'])
        if row['revision'] != body['revision'] or row['fingerprint'] != body['fingerprint']:
            raise StateConflict('任务已更新，请刷新后再提交。')
        run = self.store.get_run(body['run_id'])
        validate_continuation(row, run, action, body['message'], body['answers'])
        if action != 'save_input_draft':
            if not self.agents.get(run['executor'], {}).get('enabled'):
                raise ValueError('原执行器暂不可用，请恢复登录或连接后再继续。')
            settings = {k: run['execution_settings'][k] for k in ('model', 'effort') if run['execution_settings'].get(k)}
            resolve_execution_settings(run['executor'], self.service.config, {}, settings, row['difficulty'])
            if (run.get('result') or {}).get('result_receipt'):
                from .result_receipt import check_answer_receipt
                check_answer_receipt(self.service.root, dict(run, id=run['task_id'], current_run_id=run['id']))
        return row

    def _validate_permissions(self,body,payload):
        profile=permissions.validate(body['executor'],body.get('permission_profile','default'),body.get('full_access_ack',False))
        if profile=='default': return profile
        policy,route=self._validate_assignment_route(body['executor'],payload,body.get('repo_alias'),body.get('delivery_mode','auto'))
        delivery=resolve_delivery(policy,route['executor'])
        permissions.resolve(profile,route['executor'],answer=delivery['mode']=='answer')
        return profile

    def _validate_assignment_route(self, executor, payload, repo_alias=None, delivery_mode='auto'):
        from .workbuddy_runner import refresh_worker_registry
        self.agents=refresh_worker_registry(self.agents,self.service.config,getattr(self.service,'runner',None))
        policy = classify(payload, self.service.config)
        policy.update(payload=payload, delivery_mode=validate_mode(delivery_mode))
        if repo_alias is not None:
            policy['kind'] = 'code'
            policy['repo'] = self.service.config.get('repositories', {}).get(repo_alias)
        available = task_available_executors(policy, self.service.config, self.agents)
        if executor == 'qoder' and policy['kind'] == 'code' and not available.get('qoder'):
            raise ValueError('Qoder code tasks require configured repository checks; choose a checked repository or another executor')
        route = choose_executor(payload, policy['difficulty'], policy['kind'], self.service.config,
                                available, manual_executor=None if executor == 'auto' else executor)
        return policy, route

    def _validate_agent_settings(self, executor, settings):
        settings = validate_execution_settings(executor, settings)
        resolve_execution_settings(executor, self.service.config, settings, {}, 'medium')
        return settings

    def _validate_workspace_choice(self, choice, payload, repo_alias=None, *, executor=None):
        choice=validate_workspace_choice(choice)
        if (choice['mode']=='existing' and executor in ('workbuddy','doubao')
                and self.service.config.get(executor+'_transport')=='mcp'):
            label='WorkBuddy' if executor=='workbuddy' else '豆包'
            raise ValueError(label+' 桌面连接当前仅支持默认或新建工作区。')
        if choice['mode']=='existing':
            policy=classify(payload,self.service.config)
            repo=self.service.config.get('repositories',{}).get(repo_alias) if repo_alias else policy.get('repo')
            workspaces=getattr(self.service,'workspace_policy',None)
            if workspaces is None:raise ValueError('Workspace discovery is unavailable')
            workspaces.resolve(choice,repo=repo)
        return choice

    def _validate_assignment_settings(self, executor, payload, overrides, repo_alias=None, delivery_mode='auto'):
        if not isinstance(overrides, dict):
            raise ValueError('Session settings must be an object')
        if executor == 'auto' and overrides:
            raise ValueError('Session settings require an explicitly selected executor')
        policy, route = self._validate_assignment_route(executor, payload, repo_alias, delivery_mode)
        name = route['executor']
        resolve_delivery(policy, name)
        overrides = validate_execution_settings(name, overrides)
        defaults = self.store.get_agent_settings(name)['settings']
        resolve_execution_settings(name, self.service.config, defaults, overrides, policy['difficulty'])
        return overrides

    def process_pending(self, limit=8):
        for _ in range(limit):
            command = self.store.next_command()
            if command is None:
                break
            try:
                result = self._execute(command['action'], command['payload'], command_id=command['id'])
                self.store.finish_command(command['id'], 'done', result=result)
            except Exception as exc:
                status = 409 if isinstance(exc, StateConflict) else 404 if isinstance(exc, (KeyError, FileNotFoundError)) else 400 if isinstance(exc, ValueError) else 500
                self.store.finish_command(command['id'], 'failed', error=str(exc), http_status=status)

    def _execute(self, action, body, *, command_id=None):
        if action in INPUT_ACTIONS:
            row = self._validate_continuation(body, action)
            if action == 'save_input_draft': return self.store.save_input_draft(body)
            fresh = self.service.dida.get(row['payload'])
            return self.task(self.store.continue_task(body, fresh, command_id, action))
        if action == 'configure_agent':
            settings = self._validate_agent_settings(body['executor'], body['settings'])
            return self.store.set_agent_settings(body['executor'], settings, body['revision'])
        if action == 'sync':
            return self.service.sync(force=True)
        if action in {'archive', 'restore'}:
            return self.store.archive_run(body['run_id'], archived=action == 'archive')
        if action == 'open_workspace':
            self.workspaces.open(body['run_id'])
            return {'opened': True}
        task_id = body['task_id']
        row = self.store.get(task_id)
        if not row:
            raise KeyError(task_id)
        if row['revision'] != body['revision']:
            raise StateConflict('Task changed; refresh before retrying')
        if action == 'assign':
            fresh = self.service.dida.get(row['payload'])
            self._validate_permissions(body,fresh)
            overrides = self._validate_assignment_settings(body['executor'], fresh,
                body.get('execution_overrides', {}), body.get('repo_alias'), body.get('delivery_mode', 'auto'))
            choice=self._validate_workspace_choice(body.get('workspace_choice',{'mode':'default'}),fresh,body.get('repo_alias'),executor=body['executor'])
            return self.task(self.store.assign(task_id, body['executor'], body['revision'], fresh,
                                              self.service.config, repo_alias=body.get('repo_alias'),
                                              execution_overrides=overrides,workspace_choice=choice,
                                              delivery_mode=body.get('delivery_mode', 'auto'),
                                              permission_profile=body.get('permission_profile','default'),
                                              full_access_ack=body.get('full_access_ack',False)))
        if action == 'withdraw':
            return self.task(self.store.withdraw(task_id, body['revision']))
        if action == 'approve':
            if row['state'] != 'awaiting_review' or row.get('current_run_id') != body['run_id'] or row['fingerprint'] != body['fingerprint'] or manifest_digest(row.get('result')) != body['manifest_digest']:
                raise StateConflict('Review no longer matches the current run')
            self.workspaces.resolve_run(body['run_id'])
            if not self.service.current(row) or not self.service._check_artifacts(row):
                raise StateConflict('Source or artifacts changed; review the current result again')
        return self.task(self.store.decide_review(task_id, body['revision'], body['run_id'],
            body['fingerprint'], body['manifest_digest'], approve=action == 'approve', note=body.get('note', '')))
