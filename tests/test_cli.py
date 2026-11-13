import contextlib
import http.client
import importlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dispatcher.core import Store
from dispatcher.service import InstanceLock


class CLITests(unittest.TestCase):
    def module(self, name='dispatcher.__main__'):
        self.assertIsNotNone(importlib.util.find_spec(name), f'{name} is implemented')
        return importlib.import_module(name)

    def test_config_deep_merges_and_validates_limits(self):
        cli = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'config.example.json').write_text(json.dumps({'efforts': {'easy': 'low', 'medium': 'medium', 'hard': 'high'}}))
            (root/'config.local.json').write_text(json.dumps({'efforts': {'easy': 'medium'}}))
            self.assertEqual(cli.load_config(root)['efforts'], {'easy': 'medium', 'medium': 'medium', 'hard': 'high'})
            for key, invalid in [('max_concurrent', 5), ('max_concurrent', True), ('poll_seconds', 9), ('task_timeout_seconds', 29), ('daily_max_runs', 0), ('max_attempts', 0)]:
                with self.subTest(key=key, invalid=invalid):
                    (root/'config.local.json').write_text(json.dumps({key: invalid}))
                    with self.assertRaises(ValueError):
                        cli.load_config(root)

    def test_status_and_stop_work_without_installed_clis(self):
        cli = self.module()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(cli, 'make_service', side_effect=AssertionError('Must not initialize external CLIs')):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(['--root', directory, 'status']), 0)
                    self.assertEqual(cli.main(['stop', '--root', directory]), 0)
            self.assertTrue((Path(directory)/'data/stop.request').is_file())

    def test_executor_config_defaults_and_argv_overrides(self):
        cli = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = cli.load_config(root)
            self.assertEqual(config.get('enabled_executors'), ['codex'])
            self.assertEqual(config.get('executor_commands'), {})
            self.assertEqual(config.get('executor_routes'), {})
            overrides = {
                'enabled_executors': ['codex', 'qoder'],
                'executor_commands': {'qoder': ['C:/Program Files/Qoder/qodercli.exe', '--flag']},
                'executor_routes': {'code:easy': ['qoder', 'codex'], 'medium': ['codex']},
            }
            (root/'config.local.json').write_text(json.dumps(overrides))
            config = cli.load_config(root)
            for key, value in overrides.items():
                self.assertEqual(config[key], value)

    def test_executor_config_rejects_unknown_names_routes_and_shell_strings(self):
        cli = self.module()
        invalid_settings = [
            ('enabled_executors', 'codex'), ('enabled_executors', ['unknown']),
            ('enabled_executors', [1]), ('executor_commands', []),
            ('executor_commands', {'unknown': ['tool']}),
            ('executor_commands', {'dsh': 'node dsh.js'}),
            ('executor_commands', {'dsh': []}),
            ('executor_commands', {'dsh': ['node', '']}),
            ('executor_commands', {'dsh': ['node', 1]}),
            ('executor_routes', []), ('executor_routes', {'unknown': ['codex']}),
            ('executor_routes', {'code:unknown': ['codex']}),
            ('executor_routes', {'document:easy': []}),
            ('executor_routes', {'easy': 'codex'}),
            ('executor_routes', {'easy': ['unknown']}),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for key, value in invalid_settings:
                with self.subTest(key=key, value=value):
                    (root/'config.local.json').write_text(json.dumps({key: value}))
                    with self.assertRaisesRegex(ValueError, key):
                        cli.load_config(root)

    def test_agents_and_status_discover_providers_under_selected_root(self):
        cli = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = root/'.tools/providers/qoder/qodercli.exe'
            provider.parent.mkdir(parents=True)
            provider.write_bytes(b'not executed')
            (root/'config.local.json').write_text(json.dumps({'enabled_executors': ['qoder']}))
            for command in ('agents', 'status'):
                with self.subTest(command=command):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(cli.main(['--root', directory, command]), 0)
                    value = json.loads(output.getvalue())
                    agents = value if command == 'agents' else value['executors']
                    qoder = next((agent for agent in agents if agent['name'] == 'qoder'), None)
                    self.assertIsNotNone(qoder)
                    self.assertTrue(qoder['detected'])
                    self.assertTrue(qoder['enabled'])
                    self.assertEqual(qoder['command'], [str(provider)])

    def test_running_instance_blocks_retry_and_sync(self):
        cli = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root/'data/tasks.db')
            store.sync([{'id': 't', 'projectId': 'p', 'title': 'draft', 'status': 0, 'tags': ['AI']}])
            store.mark('p:t', 'failed')
            with InstanceLock(root), patch.object(cli, 'make_service', side_effect=AssertionError('No sync before lock')):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main(['--root', directory, 'retry', 'p:t']), 1)
                    self.assertEqual(cli.main(['--root', directory, 'sync']), 1)
            self.assertEqual(store.get('p:t')['state'], 'failed')

    def test_run_once_recovers_syncs_then_claims(self):
        cli = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root/'data/tasks.db')
            old = {'id': 'old', 'projectId': 'p', 'title': 'old', 'status': 0, 'tags': ['AI']}
            new = dict(old, id='new', title='new')
            store.sync([old])
            store.claim()
            calls = []
            class FakeService:
                def process_writebacks(self): pass
                def sync(self):
                    calls.append('sync')
                    self_state = store.get('p:old')['state']
                    if self_state != 'needs_review':
                        raise AssertionError('recover must precede sync')
                    store.sync([new])
                def execute(self, row):
                    calls.append(row['id'])
            with patch.object(cli, 'make_service', return_value=FakeService()):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(['--root', directory, 'run-once']), 0)
            self.assertEqual(calls, ['sync', 'p:new'])

    def test_daily_limit_prevents_run_once_from_claiming(self):
        cli = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'config.local.json').write_text('{"daily_max_runs": 1}')
            store = Store(root/'data/tasks.db')
            task = {'id': 'first', 'projectId': 'p', 'title': 'draft', 'status': 0, 'tags': ['AI']}
            store.sync([task, dict(task, id='second')])
            first = store.claim()
            store.mark(first['id'], 'done')
            class FakeService:
                def process_writebacks(self): pass
                def sync(self):
                    pass
                def execute(self, row):
                    raise AssertionError('Daily limit must block execution')
            with patch.object(cli, 'make_service', return_value=FakeService()):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(['--root', directory, 'run-once']), 0)
            self.assertEqual(store.get('p:second')['state'], 'queued')

    def test_resume_uses_execution_session_and_workspace_without_shell(self):
        cli = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root/'run/workspace'
            workspace.mkdir(parents=True)
            store = Store(root/'data/tasks.db')
            task = {'id': 't', 'projectId': 'p', 'title': 'draft', 'status': 0, 'tags': ['AI']}
            store.sync([task])
            store.mark('p:t', 'done', run_dir=str(workspace.parent), result={
                'thread_id': 'review-session', 'execution': {'thread_id': 'execution-session'},
            })
            with patch.object(cli, 'codex_command', return_value=['codex.exe']), patch.object(cli.subprocess, 'call', return_value=0) as call:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(['--root', directory, 'resume', 'p:t']), 0)
            call.assert_called_once_with(['codex.exe', 'resume', 'execution-session'], cwd=workspace)

    def test_vendor_resume_prints_vendor_instructions_without_calling_codex(self):
        cli = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root/'run'
            run_dir.mkdir()
            (run_dir/'execution-resume.txt').write_text('DSH vendor resume instructions\nSession: dsh-session', encoding='utf-8')
            store = Store(root/'data/tasks.db')
            store.sync([{'id': 't', 'projectId': 'p', 'title': 'draft', 'status': 0, 'tags': ['AI']}])
            store.mark('p:t', 'done', run_dir=str(run_dir), result={
                'executor': 'codex', 'thread_id': 'review-session',
                'execution': {'executor': 'dsh', 'session_id': 'dsh-session'},
            })
            output = io.StringIO()
            with patch.object(cli.subprocess, 'call', side_effect=AssertionError('Vendor sessions must not launch Codex')):
                with contextlib.redirect_stdout(output):
                    self.assertEqual(cli.main(['--root', directory, 'resume', 'p:t']), 0)
            self.assertIn('DSH vendor resume instructions', output.getvalue())
            self.assertIn('dsh-session', output.getvalue())
            self.assertIn('入口', output.getvalue())

    def test_vendor_resume_missing_instructions_reports_error(self):
        cli = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root/'data/tasks.db')
            store.sync([{'id': 't', 'projectId': 'p', 'title': 'draft', 'status': 0, 'tags': ['AI']}])
            store.mark('p:t', 'done', run_dir=str(root/'missing'), result={
                'executor': 'zcode', 'session_id': 'zcode-session',
            })
            error = io.StringIO()
            with patch.object(cli.subprocess, 'call', side_effect=AssertionError('No fallback to Codex')):
                with contextlib.redirect_stderr(error):
                    self.assertEqual(cli.main(['--root', directory, 'resume', 'p:t']), 1)
            self.assertIn('execution-resume.txt', error.getvalue())

    def test_run_once_retries_all_pending_writebacks_even_at_daily_limit(self):
        cli = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'config.local.json').write_text('{"daily_max_runs": 1}')
            store = Store(root/'data/tasks.db')
            task = {'id': 'first', 'projectId': 'p', 'title': 'draft', 'status': 0, 'tags': ['AI']}
            store.sync([task, dict(task, id='second'), dict(task, id='third')])
            pending = []
            for _ in range(2):
                row = store.claim()
                pending.append(row['id'])
                store.mark(row['id'], 'writeback_pending')
            calls = []
            class FakeService:
                def sync(self):
                    calls.append('sync')
                def writeback(self, row):
                    calls.append(row['id'])
                    store.mark(row['id'], 'done')
                def process_writebacks(self):
                    for row in store.list_tasks():
                        if row['state'] == 'writeback_pending':
                            self.writeback(row)
                def execute(self, row):
                    raise AssertionError('Writeback retry must not execute tasks')
            with patch.object(cli, 'make_service', return_value=FakeService()):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(['--root', directory, 'run-once']), 0)
            self.assertEqual(calls, ['sync', *pending])
            self.assertEqual(store.get('p:third')['state'], 'queued')
            self.assertEqual(store.count_started_today(), 2)

    def test_doctor_omits_token_lines_in_success_and_error(self):
        cli = self.module()
        with patch.object(cli, 'dida_command', return_value=['dida']), patch.object(cli, 'codex_command', return_value=['codex']):
            for response, expected in [(('已登录\n  Token: synthetic-hidden-fragment\n', ''), 0),
                                       (RuntimeError('CLI failed\nToken: synthetic-hidden-fragment'), 1)]:
                output = io.StringIO()
                with self.subTest(response=response), patch.object(cli, 'run_process', side_effect=[response, ('Logged in using ChatGPT', '')]):
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(cli.doctor(Path('.'), {}), expected)
                    self.assertNotIn('Token:', output.getvalue())
                    self.assertNotIn('synthetic-hidden-fragment', output.getvalue())

    def test_doctor_marks_exit_zero_dida_not_logged_in_as_failure(self):
        cli = self.module()
        with patch.object(cli, 'dida_command', return_value=['dida']), patch.object(cli, 'codex_command', return_value=['codex']):
            with patch.object(cli, 'run_process', side_effect=[('未登录\n', ''), ('Logged in using ChatGPT', '')]):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.doctor(Path('.'), {}), 1)


class DashboardTests(unittest.TestCase):
    module = CLITests.module

    def test_vendor_snapshot_preserves_executor_route_and_separates_review_thread(self):
        dashboard = self.module('dispatcher.dashboard')
        snapshot = {'tasks': [{
            'id': 'p:t', 'payload': {'title': 'draft'}, 'state': 'done',
            'result': {'executor': 'codex', 'thread_id': 'review-thread', 'execution': {
                'executor': 'dsh', 'session_id': 'dsh-session',
                'route': {'reason': 'document/easy route: dsh → codex'},
            }},
        }, {
            'id': 'p:u', 'result': {'executor': 'zcode', 'session_id': 'zcode-session',
                                  'route': {'reason': 'Explicit agent:zcode tag'}},
        }]}
        first, second = dashboard.public_snapshot(snapshot)['tasks']
        self.assertEqual(first.get('executor'), 'dsh')
        self.assertEqual(first.get('session_id'), 'dsh-session')
        self.assertEqual(first.get('route_reason'), 'document/easy route: dsh → codex')
        self.assertIsNone(first['thread_id'])
        self.assertEqual(first['review_thread_id'], 'review-thread')
        self.assertEqual(second.get('executor'), 'zcode')
        self.assertEqual(second.get('session_id'), 'zcode-session')
        self.assertEqual(second.get('route_reason'), 'Explicit agent:zcode tag')

    def test_index_labels_vendor_session_and_route_without_codex_handoff(self):
        index = self.module('dispatcher.index')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'run').mkdir()
            (root/'run/execution-resume.txt').write_text('DSH session instructions',encoding='utf-8')
            index.write_index(root, [{
                'id': 'p:t', 'payload': {'title': 'draft'}, 'state': 'done', 'run_dir': str(root/'run'),
                'result': {'executor': 'codex', 'thread_id': 'review-thread', 'execution': {
                    'executor': 'dsh', 'session_id': 'dsh-session',
                    'route': {'reason': 'document/easy route: dsh → codex'},
                }},
            }])
            contents = (root/'workspaces/INDEX.md').read_text(encoding='utf-8')
            self.assertIn('执行器：`dsh`', contents)
            self.assertIn('dsh-session', contents)
            self.assertIn('document/easy route: dsh → codex', contents)
            self.assertIn('execution-resume.txt', contents)
            self.assertNotIn('Codex 会话', contents)
            self.assertNotIn('/app', contents)

    def test_dashboard_filters_data_and_rejects_nonlocal_origins_and_writes(self):
        dashboard = self.module('dispatcher.dashboard')
        snapshot = {'tasks': [{'id': 'p:t', 'payload': {'title': '<img src=x onerror=alert(1)>', 'content': 'private body'}, 'state': 'running', 'result': {'thread_id': 'session-1'}, 'run_dir': '/test/run'}], 'counts': {'running': 1}, 'events': [{'message': 'private body'}]}
        with dashboard.start_dashboard(lambda: snapshot, port=0) as server:
            port = server.server_address[1]
            def request(method, path, headers=None):
                connection = http.client.HTTPConnection('127.0.0.1', port)
                connection.request(method, path, headers=headers or {})
                response = connection.getresponse()
                value = (response.status, response.read())
                connection.close()
                return value
            status, body = request('GET', '/api/status')
            self.assertEqual(status, 200)
            self.assertNotIn(b'private body', body)
            self.assertIn(b'session-1', body)
            self.assertEqual(request('GET', '/api/status', {'Host': 'example.com'})[0], 403)
            self.assertEqual(request('GET', '/api/status', {'Origin': 'https://example.com'})[0], 403)
            self.assertEqual(request('GET', '/api/status', {'Host': f'127.0.0.1:{port}', 'Origin': f'http://127.0.0.1:{port}'})[0], 200)
            self.assertEqual(request('POST', '/api/status')[0], 405)
            self.assertEqual(request('GET', '/stop')[0], 404)
            page = request('GET', '/')[1]
            self.assertNotIn(b'innerHTML', page)
            self.assertIn(b'app.js', page)
            self.assertIn(b'textContent', request('GET', '/static/app.js')[1])


if __name__ == '__main__':
    unittest.main()
