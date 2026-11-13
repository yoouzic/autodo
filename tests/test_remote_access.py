import http.client
import json
import unittest
import uuid

from dispatcher.dashboard import start_dashboard
from dispatcher.remote import RemoteAccess
from tests import test_control_api


class RemoteAccessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_control_api.ControlAPITests()
        self.fixture.setUp()
        self.access = RemoteAccess('https://machine.example.ts.net', ['owner@example.com'])
        self.context = start_dashboard(self.fixture.service.snapshot, port=0, control=self.fixture.control, remote=self.access)
        self.server = self.context.__enter__()
        self.headers = {'Host': 'machine.example.ts.net', 'X-Forwarded-Host': 'machine.example.ts.net',
                        'X-Forwarded-Proto': 'https', 'Tailscale-User-Login': 'owner@example.com',
                        'X-Forwarded-For': '100.10.20.30'}

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.fixture.tearDown()

    def request(self, method='GET', path='/api/state', headers=None, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1])
        connection.putrequest(method, path, skip_host=True)
        entries = headers.items() if isinstance(headers, dict) else (headers or [])
        for key, value in entries:
            connection.putheader(key, value)
        data = json.dumps(body).encode() if body is not None else b''
        if body is not None:
            connection.putheader('Content-Type', 'application/json')
            connection.putheader('Content-Length', str(len(data)))
        connection.endheaders(data)
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def state(self):
        status, data = self.request(headers=self.headers)
        self.assertEqual(status, 200)
        return json.loads(data)

    def post(self, body, headers=None):
        expected = dict(self.headers, Origin=self.access.origin,
                        **{'X-Dispatcher-Token': self.state()['csrf_token']})
        return self.request('POST', '/api/actions', expected if headers is None else headers, body)

    def test_all_reads_require_owner_identity(self):
        for path in ('/', '/static/app.js', '/api/state', '/api/status', '/api/task?id=p:t',
                     '/api/log?run_id=x', '/api/artifact?run_id=x&path=result.md&download=1', '/api/commands/x'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path=path, headers={'Host': 'machine.example.ts.net'})[0], 403)
        for name, value in [('Tailscale-User-Login', 'stranger@example.com'), ('Host', '127.0.0.1:8765'),
                            ('X-Forwarded-Host', 'localhost:8766'), ('X-Forwarded-Proto', 'http')]:
            with self.subTest(name=name):
                self.assertEqual(self.request(headers=dict(self.headers, **{name: value}))[0], 403)
        self.assertEqual(self.state()['client'], {'mode': 'remote', 'can_open_workspace': False})
        self.assertEqual(self.access.diagnostics()['source'], '100.10.20.30')

    def test_duplicate_headers_cross_origin_and_csrf_rejected(self):
        for name in ('Host', 'Tailscale-User-Login', 'X-Forwarded-Proto', 'X-Forwarded-Host'):
            duplicated = list(self.headers.items()) + [(name, self.headers[name])]
            self.assertEqual(self.request(headers=duplicated)[0], 403)
        body = {'action': 'sync', 'request_id': str(uuid.uuid4())}
        headers = dict(self.headers, Origin=self.access.origin, **{'X-Dispatcher-Token': self.state()['csrf_token']})
        self.assertEqual(self.post(body, dict(headers, Origin='https://evil.example'))[0], 403)
        self.assertEqual(self.post(body, dict(headers, **{'X-Dispatcher-Token': 'wrong'}))[0], 403)
        self.assertEqual(self.post(body, list(headers.items())+[('X-Dispatcher-Token', headers['X-Dispatcher-Token'])])[0], 403)

    def test_chat_link_navigation_allowed_but_cross_site_fetch_denied(self):
        navigation = dict(self.headers, **{'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'})
        self.assertEqual(self.request(path='/', headers=navigation)[0], 200)
        self.assertEqual(self.request(headers=dict(navigation, **{'Sec-Fetch-Mode': 'cors'}))[0], 403)

    def test_remote_open_directory_is_rejected_before_queueing(self):
        body = {'action': 'open_workspace', 'run_id': 'any', 'request_id': str(uuid.uuid4())}
        self.assertEqual(self.post(body)[0], 403)
        self.assertIsNone(self.fixture.store.next_command())

    def test_remote_dispatch_and_review_use_existing_queue(self):
        task = self.state()['tasks'][0]
        status, raw = self.post({'action': 'assign', 'request_id': str(uuid.uuid4()),
                                'task_id': task['id'], 'revision': task['revision'], 'executor': 'auto'})
        self.assertEqual(status, 202)
        self.fixture.control.process_pending()
        self.assertEqual(self.fixture.store.get_command(json.loads(raw)['id'])['status'], 'done')
        self.fixture.service.execute(self.fixture.store.claim())
        task = self.state()['tasks'][0]
        self.assertEqual(task['state'], 'awaiting_review')
        self.assertEqual(self.fixture.dida.completions, 0)
        fields = self.fixture.review_fields(task)
        status, _ = self.post({'action': 'approve', 'request_id': str(uuid.uuid4()), **fields})
        self.assertEqual(status, 202)
        self.fixture.control.process_pending()
        self.fixture.service.process_writebacks()
        self.assertEqual(self.fixture.dida.completions, 1)

    def test_local_endpoint_refuses_proxy_identity_headers(self):
        with start_dashboard(self.fixture.service.snapshot, port=0, control=self.fixture.control) as server:
            connection = http.client.HTTPConnection('127.0.0.1', server.server_address[1])
            connection.request('GET', '/api/state', headers={'Tailscale-User-Login': 'owner@example.com'})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            connection.close()


if __name__ == '__main__':
    unittest.main()
