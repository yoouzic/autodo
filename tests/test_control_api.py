import hashlib
import http.client
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from dispatcher.control import Controller
from dispatcher.core import Store
from dispatcher.dashboard import start_dashboard
from dispatcher.service import Service
from dispatcher.workspace import WorkspaceManager
from tests.test_service import FakeDida, FakeRunner


class ManagedRunner(FakeRunner):
    def prepare(self, row):
        directory = self.root/'workspaces/p/task/runs'/row['current_run_id']
        (directory/'workspace').mkdir(parents=True)
        return directory


class ControlAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'data/tasks.db')
        self.source = {'id': 't', 'projectId': 'p', 'title': '整理文档验收清单', 'tags': [], 'status': 0, 'priority': 5}
        self.dida = FakeDida(self.source.copy())
        self.service = Service(self.root, {'enabled_executors': ['codex'], 'require_human_review': True},
                               self.store, self.dida, ManagedRunner(self.root))
        self.control = Controller(self.service, WorkspaceManager(self.root, self.store),
                                  [{'name': 'codex', 'enabled': True, 'status': 'available'}])
        self.service.sync()

    def tearDown(self):
        self.temp.cleanup()

    def command(self, action, **kwargs):
        submitted = self.control.submit({'action': action, 'request_id': str(uuid.uuid4()), **kwargs})
        self.control.process_pending()
        return self.store.get_command(submitted['id'])

    def ready(self):
        row = self.store.get('p:t')
        command = self.command('assign', task_id=row['id'], revision=row['revision'], executor='auto')
        self.assertEqual(command['status'], 'done', command['error'])
        self.service.execute(self.store.claim())
        return self.control.detail('p:t')['task']

    def review_fields(self, task):
        return dict(task_id=task['id'], revision=task['revision'], run_id=task['current_run_id'],
                    fingerprint=task['fingerprint'], manifest_digest=task['manifest_digest'])

    def test_manual_untagged_assignment_review_then_completion(self):
        task = self.ready()
        self.assertEqual(task['stage'], 'review')
        self.assertEqual((self.dida.comments, self.dida.completions), (0, 0))
        self.assertTrue(task['can_approve'])
        result = self.command('approve', **self.review_fields(task))
        self.assertEqual(result['status'], 'done', result['error'])
        self.assertEqual(self.dida.completions, 0)
        self.service.process_writebacks()
        self.assertEqual(self.store.get('p:t')['state'], 'done')
        self.assertEqual(self.dida.completions, 1)
        self.assertEqual(self.store.get_run(task['current_run_id'])['review_status'], 'approved')

    def test_reject_preserves_feedback_and_no_external_write(self):
        task = self.ready()
        result = self.command('reject', **self.review_fields(task), note='请补充依据')
        self.assertEqual(result['status'], 'done')
        row = self.store.get('p:t')
        self.assertEqual(row['feedback'], '请补充依据')
        self.service.process_writebacks()
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.dida.completions, 0)

    def test_stale_and_tampered_reviews_rejected(self):
        task = self.ready()
        fields = self.review_fields(task)
        fields['revision'] -= 1
        submitted = self.control.submit({'action': 'approve', 'request_id': 'old', **fields})
        self.control.process_pending()
        self.assertEqual(self.store.get_command(submitted['id'])['http_status'], 409)
        path = self.control.workspaces.resolve_artifact(task['current_run_id'], 'result.md')
        path.write_text('changed', encoding='utf-8')
        result = self.command('approve', **self.review_fields(task))
        self.assertEqual(result['http_status'], 409)
        self.assertEqual(self.dida.completions, 0)

    def test_http_csrf_host_origin_methods_and_command_polling(self):
        with start_dashboard(self.service.snapshot, port=0, control=self.control) as server:
            port = server.server_address[1]
            def request(method, path, payload=None, headers=None):
                connection = http.client.HTTPConnection('127.0.0.1', port)
                body = json.dumps(payload) if payload is not None else None
                connection.request(method, path, body=body, headers=headers or {})
                response = connection.getresponse()
                status, content = response.status, response.read()
                connection.close()
                return status, content
            code, raw = request('GET', '/api/state')
            self.assertEqual(code, 200)
            state = json.loads(raw)
            row = state['tasks'][0]
            payload = {'action': 'assign', 'request_id': 'http-1', 'task_id': row['id'], 'revision': row['revision'], 'executor': 'codex'}
            headers = {'Content-Type': 'application/json', 'Origin': f'http://127.0.0.1:{port}', 'X-Dispatcher-Token': state['csrf_token']}
            self.assertEqual(request('POST', '/api/actions', payload)[0], 403)
            self.assertEqual(request('POST', '/api/actions', payload, dict(headers, Origin='https://evil.example'))[0], 403)
            self.assertEqual(request('GET', '/api/state', headers={'Host': 'evil.example'})[0], 403)
            code, data = request('POST', '/api/actions', payload, headers)
            self.assertEqual(code, 202)
            self.control.process_pending()
            command = json.loads(request('GET', '/api/commands/'+json.loads(data)['id'])[1])
            self.assertEqual(command['status'], 'done')
            self.assertEqual(request('GET', '/api/actions')[0], 404)
            self.assertEqual(request('POST', '/stop', payload, headers)[0], 405)
            self.assertEqual(request('GET', '/static/../../config.local.json')[0], 404)
            same = json.loads(request('POST', '/api/actions', payload, headers)[1])
            self.assertEqual(same['id'], command['id'])

    def test_artifact_preview_download_and_unknown_path(self):
        task = self.ready()
        self.assertEqual(self.control.workspaces.preview(task['current_run_id'], 'result.md')['text'], 'done')
        with self.assertRaises(ValueError):
            self.control.workspaces.preview(task['current_run_id'], '../task.json')


if __name__ == '__main__':
    unittest.main()
