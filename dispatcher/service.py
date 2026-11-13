"""Durable scheduler: source reconciliation, execution, verification, writeback."""
import copy
import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .core import StateConflict, classify, is_completed, manifest_digest, task_fingerprint, task_key, normalize_task
from .runner import artifact_paths
from .index import write_index
from .routing import RoutingError, EXECUTORS
from .workspace_policy import WorkspacePolicy
from .workspace_lock import WorkspaceBusy
from .workbuddy import WorkBuddyLoginRequired
from .codebuddy import CodeBuddyLoginRequired
from .doubao import DoubaoRunIncomplete
from .control_store import validate_workspace_choice, _artifact_directory, _reject_path_links
from .result_protocol import validate_answer, resolve_delivery
from .result_receipt import freeze_answer, check_answer_receipt
from .task_inputs import local_reply


def _business_snapshot(workspace):
    from .workspace_copy import _scan, _manifest
    scan = _scan(workspace)
    return {'files': _manifest(scan), 'directories': sorted(scan['directories'])}


class InstanceLock:
    def __init__(self, root): self.path = Path(root)/'data/daemon.lock'
    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        try:
            self.file.seek(0)
            if not self.file.read(1):
                self.file.write(b'0')
                self.file.flush()
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            self.file.close()
            raise RuntimeError('A dispatcher is already running')
        return self
    def __exit__(self, *_): self.file.close()


class _RunCancellation:
    """Read-only cancellation probe for a shared-service provider's native client."""
    def __init__(self,service,row):
        self.service,self.row=service,row

    def is_set(self):
        if self.service.stop.is_set():
            return True
        try:
            latest=self.service._same_run(self.row)
            return (latest is None or latest['state']!='running' or bool(latest.get('paused')) or
                    is_completed(latest['payload']))
        except Exception:
            # Lost source/run visibility cannot authorize continued execution.
            return True


class Service:
    def __init__(self, root, config, store, dida, runner):
        self.root, self.config, self.store, self.dida, self.runner = Path(root), config, store, dida, runner
        self.last_sync, self.last_error = None, None
        self.warnings = []
        self.syncing = False
        self.last_sync_duration_seconds = None
        self._source_probe_backoff = {}
        self.stop = threading.Event()
        self._writeback_locks = {}
        self._writeback_locks_guard = threading.Lock()
        self.control = None
        self.tunnel = None
        self._status_next = 0.0
        self._status_content = None
        self.workspace_policy = WorkspacePolicy(self.root,store,config)

    @property
    def require_human_review(self):
        return self.config.get('require_human_review', True) is not False

    def _mark(self, row, state, **fields):
        return self.store.mark(row['id'], state, expected_run_id=row.get('current_run_id'), **fields)

    def _mark_error(self, row, state, error):
        try:
            self._mark(row, state, error=str(error))
        except StateConflict:
            self.store.event(row['id'], 'Ignored stale worker error after the current run changed')

    def _same_run(self, row):
        latest = self.store.get(row['id'])
        if (latest is None or latest.get('current_run_id') != row.get('current_run_id') or
                latest['fingerprint'] != row['fingerprint']):
            return None
        return latest

    def sync(self, *, force=False):
        started = time.monotonic()
        self.syncing = True
        try:
            tasks, warnings = self.dida.fetch_tasks()
            tasks = list(tasks)
            warnings = list(warnings)
            present = set()
            for task in tasks:
                try: present.add(task_key(task))
                except (KeyError, ValueError, TypeError): pass
            candidates = [row for row in self.store.list_tasks()
                          if row['id'] not in present and (row['state'] != 'done' or not row.get('source_checked_at'))
                          and not (row.get('source_status') == 2 and row.get('source_checked_at'))]
            # Avoid repeated 404/timeouts monopolising the scheduler. Each cycle
            # gets a small serial read budget; unprobed tasks stay unchanged.
            deadline = time.monotonic() + 6
            attempted, confirmed = 0, 0
            candidates.sort(key=lambda row: (row['state'] == 'done', self._source_probe_backoff.get(row['id'], (0, 0))[1]))
            for row in candidates:
                failures, next_probe = self._source_probe_backoff.get(row['id'], (0, 0))
                if not force and time.monotonic() < next_probe:
                    continue
                remaining = deadline - time.monotonic()
                if attempted >= 20 or remaining <= 0:
                    break
                attempted += 1
                try:
                    probe = getattr(self.dida, 'probe', None)
                    fresh = probe(row['payload'], timeout=min(8, remaining)) if probe else self.dida.get(row['payload'])
                    normalized = normalize_task(fresh)
                    if task_key(normalized) != row['id'] or normalized['status'] not in (0, 2):
                        raise ValueError('Source status or identity could not be confirmed')
                    tasks.append(fresh)
                    confirmed += 1
                    if normalized['status'] == 0:
                        # Remember successful reads too, so a truncated active
                        # prefix cannot starve later completed source tasks.
                        self._source_probe_backoff[row['id']] = (0, time.monotonic())
                    else:
                        self._source_probe_backoff.pop(row['id'], None)
                except (OSError, RuntimeError, ValueError, TypeError, KeyError, TimeoutError):
                    failures = min(failures + 1, 5)
                    self._source_probe_backoff[row['id']] = (failures, time.monotonic() + min(300, 20 * 2**(failures-1)))
            if len(candidates) > confirmed:
                warnings.append(f'仍有 {len(candidates)-confirmed} 项离开未完成列表的任务待核对；未将缺失或读取失败视为完成。')
            live_ids = {row['id'] for row in candidates}
            self._source_probe_backoff = {key:value for key,value in self._source_probe_backoff.items() if key in live_ids}
            self.warnings = warnings
            result = self.store.sync(tasks, self.config)
            write_index(self.root, self.store.list_tasks(), store=self.store)
            self.last_sync, self.last_error = time.time(), None
            return result
        finally:
            self.syncing = False
            self.last_sync_duration_seconds = round(time.monotonic() - started, 3)

    def current(self, row):
        latest = self._same_run(row)
        if latest is None:
            return False
        fresh = self.dida.get(row['payload'])
        status = fresh.get('status')
        expected_status = 2 if local_reply(row) else 0
        if not ((type(status) is int and status == expected_status) or
                (isinstance(status, str) and status.strip() == str(expected_status))):
            if is_completed(fresh):
                self.store.sync([fresh], self.config)
            else:
                self._mark(row, 'cancelled', error='Source task is no longer incomplete')
            return False
        if latest.get('paused') or not (latest.get('delegated') or classify(fresh, self.config)['eligible']):
            self._mark(row, 'cancelled', error='Delegation was removed or paused')
            return False
        if task_fingerprint(fresh) != row['fingerprint']:
            self._mark(row, 'needs_review', error='Source changed since dispatch')
            return False
        return True

    def execute(self, row):
        try:
            if row['attempts'] > self.config.get('max_attempts', 2):
                self._mark(row, 'needs_input', error='Maximum attempts reached')
                return
            if not self.current(row): return
            if row.get('input_context'):
                run = self.store.get_run(row['current_run_id'])
                if (not run or run.get('input_context') != row['input_context'] or run.get('input_version') != row['input_version'] or
                        row['input_context'].get('source_fingerprint') != row['fingerprint']):
                    raise ValueError('Frozen task input does not match this run')
            if row['payload'].get('repeatFlag'):
                self._mark(row, 'needs_input', error='Recurring tasks require occurrence scheduling; delegate a separate one-off task in v1')
                return
            choice=validate_workspace_choice(row.get('workspace_choice') or {'mode':'default'})
            if (row.get('kind') == 'code' and not row.get('repo') and choice['mode']=='default'
                    and resolve_delivery(row, 'codex')['mode'] != 'answer'):
                self._mark(row, 'needs_input', error='Code task needs a configured repo:alias tag')
                return
            row = dict(row, _agent_defaults={name: self.store.get_agent_settings(name)['settings']
                       for name in EXECUTORS})
            row['_workspace_store']=self.store
            if choice['mode']=='existing':
                row['_workspace_source']=self.workspace_policy.resolve(choice,repo=row.get('repo'))
            run_dir = self.runner.prepare(row)
            actual_workspace=Path((row.get('_workspace_binding') or {}).get('workspace_path') or Path(run_dir)/'workspace')
            route_file=Path(run_dir)/'routing.json'
            route=json.loads(route_file.read_text(encoding='utf-8')) if route_file.is_file() else {'executor':'codex'}
            running_result = {'executor':route['executor'],'route':route}
            if row.get('_execution_settings') is not None:
                running_result['execution_settings'] = copy.deepcopy(row['_execution_settings'])
            if row.get('_permissions') is not None:
                running_result['permissions'] = copy.deepcopy(row['_permissions'])
            if row.get('_execution_limits') is not None:
                running_result['execution_limits']=copy.deepcopy(row['_execution_limits'])
            if row.get('_workspace_binding') is not None:
                running_result['workspace_binding']=copy.deepcopy(row['_workspace_binding'])
            self._mark(row, 'running', run_dir=str(run_dir), result=running_result, error=None)
            self.workspace_policy.invalidate()
            if route['executor'] in ('doubao','workbuddy'):
                row['_cancel_event']=_RunCancellation(self,row)
            answer_mode = row.get('_delivery_policy', {}).get('mode') == 'answer'
            before = _business_snapshot(actual_workspace) if answer_mode else None
            result = self.runner.execute(row, run_dir)
            if answer_mode:
                validate_answer(result)
                if _business_snapshot(actual_workspace) != before:
                    raise ValueError('纯文本任务修改了业务文件，不能送审。')
                result['file_changes'] = {'status': 'verified', 'added': 0, 'modified': 0, 'deleted': 0}
            elif result.get('protocol_version') == 2 or result.get('delivery_type') == 'answer':
                raise ValueError('Executor cannot change the frozen delivery type')
            if route['executor'] in ('doubao','workbuddy') and not self.current(row):
                return
            recovered = result['status'] == 'recovered'
            recovery_report = None
            directory_probe = result.get('directory_probe_recovered') is True
            if directory_probe:
                if result.get('executor')!='codebuddy' or result['status']!='complete' or not self.require_human_review:
                    raise ValueError('Directory-probe recovery requires mandatory human review')
                expected=result.get('recovered_artifact_sha256')
                paths=artifact_paths(actual_workspace,result.get('artifacts'))
                actual={p.relative_to(actual_workspace).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
                if actual!=expected:raise ValueError('Directory-probe artifacts changed before verification')
                self.store.event(row['id'],'CodeBuddy directory Read probe failed; later artifact writes and reads succeeded; original error retained for independent and human review')
            if recovered:
                if (not self.require_human_review or row.get('kind') != 'document' or
                        result.get('executor') != 'antigravity' or result.get('vendor_status') != 'ERROR' or
                        (result.get('runtime_warning') or {}).get('kind') != 'completed_eof'):
                    raise ValueError('Artifact recovery requires document scope and mandatory human review')
                workspace = actual_workspace.resolve()
                paths = artifact_paths(workspace, result.get('artifacts'))
                hashes = result.get('recovered_artifact_sha256')
                names = {path.relative_to(workspace).as_posix() for path in paths}
                if not isinstance(hashes, dict) or set(hashes) != names:
                    raise ValueError('Recovered artifacts require an exact hash manifest')
                for path in paths:
                    if hashlib.sha256(path.read_bytes()).hexdigest() != hashes[path.relative_to(workspace).as_posix()]:
                        raise ValueError('Recovered artifact changed before independent verification')
                recovery_report = copy.deepcopy(result)
                self.store.event(row['id'], 'Recovered completed artifacts after EOF; vendor ERROR retained; independent and human review required')
            elif result['status'] != 'complete':
                self._mark(row, 'needs_input' if result['status']=='needs_input' else 'failed',
                                result=result, error=result.get('needs_input') or result.get('summary'))
                return
            if answer_mode:
                row['_answer_receipt'], _ = freeze_answer(self.root, row, result)
            else:
                artifact_paths(actual_workspace, result.get('artifacts'))
            self._mark(row, 'verifying', result=result)
            verified = self.runner.verify(row, run_dir, result)
            if route['executor'] in ('doubao','workbuddy') and not self.current(row):
                return
            if row.get('_workspace_binding') is not None:
                verified['workspace_binding']=copy.deepcopy(row['_workspace_binding'])
            if recovery_report is not None:
                verified = dict(verified, execution=recovery_report,
                                runtime_warning=recovery_report['runtime_warning'])
            if verified['status'] != 'complete':
                self._mark(row, 'needs_review', result=verified, error=verified.get('needs_input') or verified.get('summary'))
                return
            if answer_mode:
                if _business_snapshot(actual_workspace) != before:
                    raise ValueError('业务文件在只读验收期间发生变化，不能送审。')
                check_answer_receipt(self.root, dict(row, result=verified))
                reviewed_paths = []
            else:
                reviewed_paths = artifact_paths(Path(run_dir)/'workspace', verified.get('artifacts'))
            if recovery_report is not None:
                snapshot_workspace=(Path(run_dir)/'workspace').resolve()
                actual = {path.relative_to(snapshot_workspace).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in reviewed_paths}
                if (actual != recovery_report['recovered_artifact_sha256'] or
                        manifest_digest({'sha256': verified.get('sha256')}) != manifest_digest({'sha256': actual})):
                    raise ValueError('Independent review must cover the exact unchanged recovered artifacts')
            if not verified.get('checks'):
                raise ValueError('Verifier supplied no evidence')
            if directory_probe and manifest_digest({'sha256':verified.get('sha256')})!=manifest_digest({'sha256':result['recovered_artifact_sha256']}):
                raise ValueError('Directory-probe review must bind every unchanged artifact')
            requires_review = answer_mode or recovered or self.require_human_review or bool(row.get('_permissions')) or result.get('directory_probe_recovered') is True
            next_state = 'awaiting_review' if requires_review else 'writeback_pending'
            updated = self._mark(row, next_state, result=verified, error=None)
            if not requires_review:
                self.writeback(updated)
        except StateConflict:
            self.store.event(row['id'], 'Ignored stale worker result after the current run changed')
        except (RoutingError, WorkspaceBusy, WorkBuddyLoginRequired, CodeBuddyLoginRequired, DoubaoRunIncomplete) as exc:
            self._mark_error(row, 'needs_input', exc)
        except Exception as exc:
            self._mark_error(row, 'failed', exc)
        finally:
            try:
                release=getattr(self.runner,'release_workspace',None)
                if release:release(row)
            finally:
                self.workspace_policy.invalidate()
                write_index(self.root, self.store.list_tasks(), store=self.store)

    def _check_artifacts(self, row):
        """Only the exact bytes recorded during verification may be completed."""
        try:
            result = row.get('result')
            if not isinstance(result, dict) or not row.get('run_dir'):
                raise ValueError('Missing verified result or run directory')
            if 'result_receipt' in result or (result.get('execution') or {}).get('protocol_version') == 2:
                return check_answer_receipt(self.root, row)
            hashes = result.get('sha256')
            if not isinstance(hashes, dict):
                raise ValueError('Missing verified SHA-256 hashes')
            run=self.store.get_run(row.get('current_run_id')) if row.get('current_run_id') else None
            run_path=Path(row['run_dir'])
            if not run_path.is_absolute():run_path=self.root/run_path
            _reject_path_links(run_path)
            run_path=run_path.resolve()
            artifact_root=(run or {}).get('artifact_root')
            workspace=_artifact_directory(run_path/'workspace' if artifact_root is None else artifact_root)
            if workspace==run_path or not workspace.is_relative_to(run_path):
                raise ValueError('Artifact directory escapes its run')
            for path in artifact_paths(workspace, result.get('artifacts')):
                name = str(path.relative_to(workspace))
                expected = hashes.get(name)
                if not isinstance(expected, str) or len(expected) != 64:
                    raise ValueError(f'Missing verified SHA-256 hash for {name}')
                with path.open('rb') as artifact:
                    actual = hashlib.file_digest(artifact, 'sha256').hexdigest()
                if actual != expected.lower():
                    raise ValueError(f'Artifact changed since verification: {name}')
            return True
        except (OSError, ValueError, TypeError) as exc:
            self._mark(row, 'needs_review', error=f'Artifact integrity check failed: {exc}')
            return False

    def writeback(self, row):
        with self._writeback_locks_guard:
            lock = self._writeback_locks.setdefault(row['id'], threading.Lock())
        with lock:
            latest = self.store.get(row['id'])
            if (latest is None or latest['state'] != 'writeback_pending' or
                    latest.get('current_run_id') != row.get('current_run_id') or
                    latest['fingerprint'] != row['fingerprint'] or
                    latest['run_dir'] != row['run_dir']):
                return
            self._writeback(latest)

    def _review_allows_writeback(self, row):
        latest = self._same_run(row)
        if latest is None or latest['state'] != 'writeback_pending':
            return False
        result = latest.get('result') or {}
        execution = result.get('execution') or result
        recovery_requires_review = execution.get('status') == 'recovered' or execution.get('protocol_version') == 2
        if not self.require_human_review and not recovery_requires_review:
            return True
        run = self.store.get_run(row.get('current_run_id'))
        if (run is not None and run['review_status'] == 'approved' and
                run['fingerprint'] == row['fingerprint'] and
                manifest_digest(run['result']) is not None and
                manifest_digest(run['result']) == manifest_digest(latest['result'])):
            return True
        if (latest.get('result') or {}).get('status') == 'complete':
            self._mark(row, 'awaiting_review', error='Human approval is required before source writeback')
        else:
            self._mark(row, 'needs_review', error='Legacy pending run lacks a complete verified result')
        return False

    def process_writebacks(self):
        """Approved work progresses each tick, independently of source polling."""
        for row in self.store.list_tasks(states=('approved', 'writeback_pending')):
            try:
                if row['state'] == 'approved':
                    row = self._mark(row, 'writeback_pending', error=None)
                if row['state'] == 'writeback_pending':
                    self.writeback(row)
            except StateConflict:
                continue

    def _writeback(self, row):
        try:
            if not self._review_allows_writeback(row): return
            if not self._check_artifacts(row): return
            if local_reply(row):
                if not self.current(row): return
                self._mark(row, 'done', error=None)
                return  # Completed-source explanation: no comment, no completion.
            fresh = self.dida.get(row['payload'])
            if is_completed(fresh):
                # A successful prior writeback may need acknowledgement after
                # a network error. Changed source content cannot borrow its review.
                completed_snapshot = dict(row['payload'], status=2)
                if task_fingerprint(fresh) == task_fingerprint(completed_snapshot):
                    self._mark(row, 'done', error=None)
                    self.store.sync([fresh], self.config)
                else:
                    self.store.sync([fresh], self.config)
                return
            if not self.current(row): return
            result = row['result']
            marker = '[AI-RUN:' + hashlib.sha256(row['run_dir'].encode()).hexdigest()[:16] + ']'
            if self.config.get('write_comments', True) and not result.get('comment_written'):
                text = result['summary'] + '\n本地交付目录：' + row['run_dir'] + '\n验收：' + '; '.join(result['checks'])
                if not self._review_allows_writeback(row): return
                self.dida.ensure_comment(row['payload'], marker, text)
                result['comment_written'] = True
                self._mark(row, 'writeback_pending', result=result)
            complete_source = self.config.get('auto_complete', True)
            if complete_source:
                # Re-read after comment and immediately before completion.
                if not self.current(row): return
            if not self._check_artifacts(row): return
            if complete_source:
                if not self._review_allows_writeback(row): return
                self.dida.complete(row['payload'])
            self._mark(row, 'done', result=result, error=None)
            if complete_source:
                self.store.sync([dict(row['payload'], status=2)], self.config)
        except StateConflict:
            self.store.event(row['id'], 'Ignored stale writeback after the current run changed')
        except Exception as exc:
            self._mark_error(row, 'writeback_pending', exc)

    def snapshot(self):
        tasks = self.store.list_tasks()
        counts = {}
        for task in tasks: counts[task['state']] = counts.get(task['state'], 0) + 1
        return {'last_sync': self.last_sync, 'last_error': self.last_error, 'warnings': self.warnings,
                'counts': counts, 'tasks': tasks, 'events': self.store.events(30)}

    def publish_status(self, *, now=None, force=False):
        """Disk diagnostics may lag five seconds; live API/queue polling stays live."""
        now = time.monotonic() if now is None else now
        if not force and now < self._status_next:
            return False
        content = json.dumps(self.snapshot(), ensure_ascii=False, indent=2)
        target = self.root/'data/status.json'
        if content != self._status_content or not target.exists():
            temporary = self.root/'data/status.tmp'
            temporary.write_text(content, encoding='utf-8')
            temporary.replace(target)
            self._status_content = content
            written = True
        else:
            written = False
        self._status_next = now + 5
        return written

    def daemon(self):
        with InstanceLock(self.root):
            self.store.recover()
            self.store.recover_commands()
            data = self.root/'data'
            (data/'stop.request').unlink(missing_ok=True)
            (data/'daemon.pid').write_text(str(os.getpid()))
            limit = self.config.get('max_concurrent', 1)
            futures = set()
            next_sync = 0
            try:
                if self.tunnel is not None:
                    self.tunnel.start()
                with ThreadPoolExecutor(max_workers=limit) as pool:
                    while not self.stop.is_set() and not (data/'stop.request').exists():
                        if self.control is not None:
                            self.control.process_pending()
                        self.process_writebacks()
                        now = time.time()
                        if now >= next_sync:
                            try:
                                self.sync()
                            except Exception as exc:
                                self.last_error = str(exc)
                                self.store.event(None, self.last_error)
                            next_sync = now + self.config.get('poll_seconds', 20)
                        futures = {f for f in futures if not f.done()}
                        if self.last_sync and not self.last_error:
                            while len(futures) < limit and self.store.count_started_today() < self.config.get('daily_max_runs', 10):
                                row = self.store.claim()
                                if row is None: break
                                futures.add(pool.submit(self.execute, row))
                        self.publish_status()
                        self.stop.wait(1)
            finally:
                try:
                    if self.tunnel is not None:
                        self.tunnel.stop()
                finally:
                    (data/'daemon.pid').unlink(missing_ok=True)
                    self.publish_status(force=True)
