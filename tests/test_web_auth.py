import http.client
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from dispatcher.web_auth import COOKIE, PasswordAccess
from dispatcher.dashboard import start_dashboard
from tests import test_control_api


class WebAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'auth.db'
        self.access = PasswordAccess('https://todo.example.com', 's'*48, self.path)
        access = self.access

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def _headers(self, status, size, mime, extra=None):
                self.send_response(status)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(size))
                for name, value in (extra or {}).items(): self.send_header(name, value)
                self.end_headers()
            def _respond(self, status, body, mime='text/plain'):
                self._headers(status, len(body), mime)
                self.wfile.write(body)
            def do_GET(self):
                if access.handle_auth(self): return
                if not access.ingress_allowed(self.headers):
                    self._respond(403, b'bad ingress')
                elif not access.allowed(self.headers):
                    self._respond(401, b'login required')
                else:
                    self._respond(200, b'private task data')
            def do_POST(self):
                if not access.handle_auth(self): self._respond(404, b'not found')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.headers = {'Host': 'todo.example.com', 'X-Forwarded-Host': 'todo.example.com',
                        'X-Forwarded-Proto': 'https', 'X-Dispatcher-Proxy-Secret': 's'*48,
                        'X-Forwarded-For': '203.0.113.2'}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, path, method='GET', body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        connection.putrequest(method, path, skip_host=True)
        values = self.headers if headers is None else headers
        for name, value in values.items() if isinstance(values, dict) else values:
            connection.putheader(name, value)
        data = json.dumps(body).encode('utf-8') if body is not None else b''
        if body is not None:
            connection.putheader('Content-Type', 'application/json')
            connection.putheader('Content-Length', str(len(data)))
        connection.endheaders(data)
        response = connection.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return result

    def post(self, path, body, cookie=None, extra=None):
        headers = dict(self.headers, Origin=self.access.origin)
        headers.update(extra or {})
        if cookie: headers['Cookie'] = cookie
        return self.request(path, 'POST', body, headers)

    def ticket(self):
        link = self.access.create_setup_link()
        self.assertEqual(urlsplit(link).query, '')
        return parse_qs(urlsplit(link).fragment)['setup'][0]

    def setup_owner(self):
        status, body, headers = self.post('/auth/setup', {'token': self.ticket(), 'password': 'unique test password 123'})
        self.assertEqual(status, 200, body)
        return headers['Set-Cookie'].split(';', 1)[0]

    def message(self, extra=None):
        headers = Message()
        for name, value in dict(self.headers, **(extra or {})).items(): headers[name] = value
        return headers

    def test_ingress_and_sessions_are_both_required(self):
        self.assertEqual(self.request('/api/state')[0], 401)
        self.assertEqual(self.request('/auth/login', headers={'Host': 'todo.example.com'})[0], 403)
        for name, value in [('Host', '127.0.0.1:8766'), ('X-Forwarded-Proto', 'http'),
                            ('X-Forwarded-Host', 'evil.example'), ('X-Dispatcher-Proxy-Secret', 'wrong')]:
            self.assertEqual(self.request('/auth/login', headers=dict(self.headers, **{name: value}))[0], 403)
        for name in ('Host', 'X-Dispatcher-Proxy-Secret', 'X-Forwarded-Host'):
            values = list(self.headers.items())+[(name, self.headers[name])]
            self.assertEqual(self.request('/auth/login', headers=values)[0], 403)
        cookie = self.setup_owner()
        self.assertEqual(self.request('/api/state', headers=dict(self.headers, Cookie=cookie))[0], 200)

    def test_setup_requires_unexpired_single_use_ticket(self):
        first, second = self.ticket(), self.ticket()
        password = 'unique test password 123'
        self.assertEqual(self.post('/auth/setup', {'token': first, 'password': password})[0], 403)
        self.assertFalse(self.access.has_user())
        status, body, headers = self.post('/auth/setup', {'token': second, 'password': password})
        self.assertEqual(status, 200)
        self.assertNotIn(password.encode(), body)
        for attribute in ('HttpOnly', 'Secure', 'SameSite=Lax', 'Path=/'): self.assertIn(attribute, headers['Set-Cookie'])
        self.assertEqual(self.post('/auth/setup', {'token': second, 'password': password})[0], 403)
        with self.assertRaises(RuntimeError): self.access.create_setup_link()

    def test_expired_setup_and_short_password_fail(self):
        ticket = self.ticket()
        self.assertEqual(self.post('/auth/setup', {'token': ticket, 'password': 'short'})[0], 400)
        with self.access._connect() as connection:
            connection.execute('UPDATE auth_setup SET expires_at=?', (time.time()-1,))
        self.assertEqual(self.post('/auth/setup', {'token': ticket, 'password': 'unique test password 123'})[0], 403)
        self.assertFalse(self.access.has_user())

    def test_login_persists_hashed_sessions_and_logout_revokes(self):
        self.setup_owner()
        status, _, _ = self.post('/auth/login', {'username': 'autodo', 'password': 'wrong password'})
        self.assertEqual(status, 401)
        status, _, headers = self.post('/auth/login', {'username': 'autodo', 'password': 'unique test password 123'})
        self.assertEqual(status, 200)
        cookie = headers['Set-Cookie'].split(';', 1)[0]
        token = cookie.split('=', 1)[1]
        with self.access._connect() as connection:
            raw = str(connection.execute('SELECT * FROM auth_user').fetchall())+str(connection.execute('SELECT * FROM auth_sessions').fetchall())
        self.assertNotIn(token, raw)
        self.assertNotIn('unique test password 123', raw)
        reloaded = PasswordAccess(self.access.origin, 's'*48, self.path)
        self.assertTrue(reloaded.allowed(self.message({'Cookie': cookie})))
        status, _, headers = self.post('/auth/logout', {}, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn('Max-Age=0', headers['Set-Cookie'])
        self.assertFalse(reloaded.allowed(self.message({'Cookie': cookie})))

    def test_session_expiry_and_duplicate_cookie_rejected(self):
        cookie = self.setup_owner()
        self.assertFalse(self.access.allowed(self.message({'Cookie': cookie+'; '+cookie})))
        with self.access._connect() as connection:
            connection.execute('UPDATE auth_sessions SET expires_at=?', (time.time()-1,))
        self.assertFalse(self.access.allowed(self.message({'Cookie': cookie})))

    def test_auth_posts_require_exact_origin_and_block_cross_site(self):
        body = {'username': 'autodo', 'password': 'not secret for tests'}
        self.assertEqual(self.request('/auth/login', 'POST', body)[0], 403)
        self.assertEqual(self.post('/auth/login', body, extra={'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.post('/auth/login', body, extra={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        cookie = self.setup_owner()
        navigation = self.message({'Cookie': cookie, 'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'})
        self.assertTrue(self.access.allowed(navigation))
        self.assertFalse(self.access.allowed(navigation, mutation=True))

    def test_rate_limit_and_request_size(self):
        body = {'username': 'autodo', 'password': 'wrong'}
        for _ in range(5): self.assertEqual(self.post('/auth/login', body)[0], 401)
        self.assertEqual(self.post('/auth/login', body)[0], 429)
        self.assertEqual(self.post('/auth/login', {'password': 'x'*9000}, extra={'X-Forwarded-For': '203.0.113.3'})[0], 400)

    def test_auth_assets_are_self_contained_and_no_setup_ticket_is_reflected(self):
        token = self.ticket()
        status, body, _ = self.request('/auth/setup')
        self.assertEqual(status, 200)
        self.assertNotIn(token.encode(), body)
        self.assertIn(b'/auth/app.js', body)
        self.assertNotIn(b'<script>', body)
        self.assertEqual(self.request('/auth/app.js')[0], 200)
        self.assertEqual(self.request('/auth/style.css')[0], 200)
        self.assertEqual(self.request('/auth/logout')[0], 404)

    def test_concurrent_setup_cannot_replace_owner(self):
        ticket = self.ticket()
        def attempt(password):
            try:
                return self.access._setup(ticket, password)
            except PermissionError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ['first secure password', 'second secure password']))
        self.assertEqual(sum(result is not None for result in results), 1)
        with self.access._connect() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM auth_user').fetchone()[0], 1)


class PasswordDashboardIntegrationTests(unittest.TestCase):
    def test_real_dashboard_setup_login_csrf_commands_and_logout(self):
        fixture = test_control_api.ControlAPITests()
        fixture.setUp()
        try:
            access = PasswordAccess('https://todo.example.com', 'x'*48, fixture.root/'auth.db')
            base_headers = {'Host': access.authority, 'X-Forwarded-Host': access.authority,
                            'X-Forwarded-Proto': 'https', 'X-Dispatcher-Proxy-Secret': 'x'*48,
                            'X-Forwarded-For': '203.0.113.5'}
            with start_dashboard(fixture.service.snapshot, port=0, control=fixture.control, remote=access) as server:
                def request(path, body=None, extra=None):
                    headers = dict(base_headers, **(extra or {}))
                    data = None
                    if body is not None:
                        data = json.dumps(body)
                        headers.setdefault('Origin', access.origin)
                        headers['Content-Type'] = 'application/json'
                    connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
                    connection.request('POST' if body is not None else 'GET', path, body=data, headers=headers)
                    response = connection.getresponse()
                    answer = response.status, response.read(), dict(response.getheaders())
                    connection.close()
                    return answer
                status, _, headers = request('/')
                self.assertEqual((status, headers['Location']), (302, '/auth/login'))
                self.assertEqual(request('/api/state')[0], 401)
                ticket = parse_qs(urlsplit(access.create_setup_link()).fragment)['setup'][0]
                self.assertEqual(request('/auth/setup')[0], 200)
                status, _, headers = request('/auth/setup', {'token': ticket, 'password': 'integration test password'})
                self.assertEqual(status, 200)
                cookie = headers['Set-Cookie'].split(';', 1)[0]
                authenticated = {'Cookie': cookie}
                status, raw, _ = request('/api/state', extra=authenticated)
                self.assertEqual(status, 200)
                state = json.loads(raw)
                self.assertEqual(state['client']['mode'], 'remote')
                command = {'action': 'sync', 'request_id': 'password-dashboard-test'}
                self.assertEqual(request('/api/actions', command, authenticated)[0], 403)
                with_csrf = dict(authenticated, **{'X-Dispatcher-Token': state['csrf_token']})
                self.assertEqual(request('/api/actions', command, dict(with_csrf, Origin='https://evil.example'))[0], 403)
                status, raw, _ = request('/api/actions', command, with_csrf)
                self.assertEqual(status, 202)
                fixture.control.process_pending()
                self.assertEqual(fixture.store.get_command(json.loads(raw)['id'])['status'], 'done')
                self.assertEqual(request('/api/state', extra=dict(authenticated, **{'X-Dispatcher-Proxy-Secret': 'wrong'}))[0], 403)
                self.assertEqual(request('/auth/logout', {}, authenticated)[0], 200)
                self.assertEqual(request('/api/state', extra=authenticated)[0], 401)
        finally:
            fixture.tearDown()


if __name__ == '__main__':
    unittest.main()
