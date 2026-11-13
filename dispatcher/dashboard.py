"""Loopback-only control API with a durable command queue."""
import json
import threading
import secrets
import shutil
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, quote

from .core import StateConflict


def public_snapshot(snapshot):
    tasks = []
    for row in snapshot.get('tasks', []):
        result = row.get('result') or {}
        execution = result.get('execution') or {}
        worker = execution or result
        executor = execution.get('executor') or result.get('executor') or row.get('executor')
        if not executor and (worker.get('thread_id') or row.get('thread_id')):
            executor='codex'
        thread_id = (worker.get('thread_id') or row.get('thread_id')) if executor == 'codex' else None
        route = execution.get('route') or result.get('route') or {}
        tasks.append({
            'id': row.get('id'), 'title': (row.get('payload') or {}).get('title', row.get('title', '')),
            'state': row.get('state'), 'difficulty': row.get('difficulty'), 'attempts': row.get('attempts', 0),
            'priority': row.get('priority', 0), 'reason': row.get('reason'), 'error': row.get('error'),
            'executor': executor, 'session_id': worker.get('session_id') or thread_id or row.get('session_id'),
            'route_reason': route.get('reason') or worker.get('route_reason') or row.get('route_reason'),
            'thread_id': thread_id,
            'review_thread_id': result.get('thread_id') if execution else row.get('review_thread_id'),
            'workspace': str(Path(row['run_dir'])/'workspace') if row.get('run_dir') else row.get('workspace'),
        })
    return {key: snapshot.get(key) for key in ('running', 'stop_requested', 'last_sync', 'last_error', 'counts', 'executors')} | {'tasks': tasks}


def _local_authority(authority, port):
    return authority.lower() in {f'127.0.0.1:{port}', f'localhost:{port}'}


def handler(snapshot, control=None, remote=None):
    token = secrets.token_urlsafe(32)
    static = Path(__file__).parent / 'static'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _allowed(self, mutation=False):
            if remote is not None:
                return remote.allowed(self.headers, mutation=mutation)
            # A mistakenly targeted proxy must never inherit local access.
            if any(name.lower().startswith(('tailscale-', 'x-forwarded-')) for name in self.headers):
                return False
            port = self.server.server_address[1]
            hosts = self.headers.get_all('Host', [])
            if len(hosts) != 1 or not _local_authority(hosts[0], port):
                return False
            origins = self.headers.get_all('Origin', [])
            if len(origins) > 1 or (mutation and len(origins) != 1):
                return False
            if origins:
                origin = urlsplit(origins[0])
                if origin.scheme != 'http' or origin.netloc.lower() != hosts[0].lower() or origin.path or origin.query or origin.fragment:
                    return False
            return self.headers.get('Sec-Fetch-Site', 'same-origin') != 'cross-site'

        def _headers(self, code, size, mime, extra=None):
            self.send_response(code)
            for key, value in {'Content-Type': mime, 'Content-Length': str(size), 'Cache-Control': 'no-store',
                    'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
                    'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
                    **(extra or {})}.items():
                self.send_header(key, value)
            self.end_headers()

        def _respond(self, code, data, mime='text/plain; charset=utf-8'):
            self._headers(code, len(data), mime)
            self.wfile.write(data)

        def _json(self, code, value):
            self._respond(code, json.dumps(value, ensure_ascii=False).encode('utf-8'), 'application/json; charset=utf-8')

        def _error(self, exc):
            code = 409 if isinstance(exc, StateConflict) else 404 if isinstance(exc, (KeyError, FileNotFoundError)) else 400 if isinstance(exc, (TypeError, ValueError)) else 500
            self._json(code, {'error': str(exc) if code != 500 else 'Internal request error'})

        def _deny_access(self):
            if remote is not None and hasattr(remote, 'ingress_allowed') and remote.ingress_allowed(self.headers):
                if remote.authenticated(self.headers):
                    self._json(403, {'error': 'Request origin is not permitted'})
                elif urlsplit(self.path).path.startswith('/api/') or self.command != 'GET':
                    self._json(401, {'error': '请登录派工台', 'login_url': '/auth/login'})
                else:
                    self._headers(302, 0, 'text/plain', {'Location': '/auth/login'})
            else:
                self._respond(403, b'Private access denied' if remote else b'Local requests only')

        def do_GET(self):
            try:
                if remote is not None and hasattr(remote, 'handle_auth') and remote.handle_auth(self):
                    return
                if not self._allowed():
                    self._deny_access()
                    return
                parsed = urlsplit(self.path)
                path, params = parsed.path, parse_qs(parsed.query)
                def param(name, default=None):
                    values = params.get(name, [default])
                    if len(values) != 1 or values[0] is None:
                        raise ValueError(f'One {name} is required')
                    return values[0]
                assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                          '/result-card.js': ('result-card.js', 'text/javascript; charset=utf-8'),
                          '/task-conversation.js': ('task-conversation.js', 'text/javascript; charset=utf-8'),
                          '/session-permissions.js': ('session-permissions.js', 'text/javascript; charset=utf-8'),
                          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                          '/app.css': ('app.css', 'text/css; charset=utf-8'),
                          '/assets/paper.webp': ('assets/paper.webp', 'image/webp')}
                if path.startswith('/static/'):
                    path = path.removeprefix('/static')
                if path in assets:
                    name, mime = assets[path]
                    self._respond(200, (static/name).read_bytes(), mime)
                elif path == '/api/status':
                    self._json(200, public_snapshot(snapshot()))
                elif control is None:
                    self._respond(404, b'Not found')
                elif path == '/api/state':
                    self._json(200, dict(control.state(), csrf_token=token,
                        client={'mode': 'remote' if remote else 'local', 'can_open_workspace': remote is None}))
                elif path == '/api/task':
                    self._json(200, control.detail(param('id'), run_id=param('run_id', '') or None))
                elif path == '/api/workspaces':
                    self._json(200, control.workspace_catalog(force=True))
                elif path.startswith('/api/commands/'):
                    command = control.store.get_command(path.removeprefix('/api/commands/'))
                    if command is None:
                        raise KeyError('Command not found')
                    self._json(200, {k: command[k] for k in ('id', 'status', 'result', 'error', 'http_status')})
                elif path == '/api/log':
                    self._json(200, control.workspaces.log(param('run_id'), param('phase', 'execution'), param('stream', 'events')))
                elif path == '/api/artifact':
                    run_id, name = param('run_id'), param('path')
                    if param('download', '0') == '1':
                        artifact = control.workspaces.resolve_artifact(run_id, name)
                        with artifact.open('rb') as stream:
                            self._headers(200, artifact.stat().st_size, 'application/octet-stream',
                                {'Content-Disposition': "attachment; filename*=UTF-8''" + quote(artifact.name, safe='')})
                            shutil.copyfileobj(stream, self.wfile)
                    else:
                        self._json(200, control.workspaces.preview(run_id, name))
                else:
                    self._respond(404, b'Not found')
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                self._error(exc)

        def do_POST(self):
            if remote is not None and hasattr(remote, 'handle_auth'):
                try:
                    if remote.handle_auth(self):
                        return
                except Exception as exc:
                    self._error(exc)
                    return
            if remote is not None and not self._allowed(mutation=True):
                self._deny_access()
                return
            if control is None or self.path != '/api/actions':
                self._respond(405, b'Unsupported write endpoint')
                return
            try:
                tokens = self.headers.get_all('X-Dispatcher-Token', [])
                if not self._allowed(mutation=True) or len(tokens) != 1 or not secrets.compare_digest(tokens[0], token):
                    self._respond(403, b'Origin and dispatcher token required')
                    return
                if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                    self._respond(415, b'JSON required')
                    return
                lengths = self.headers.get_all('Content-Length', [])
                if len(lengths) != 1 or self.headers.get('Transfer-Encoding'):
                    raise ValueError('One Content-Length required')
                length = int(lengths[0])
                if not 1 <= length <= 32768:
                    self._respond(413, b'Body too large or empty')
                    return
                self.connection.settimeout(10)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError('Incomplete request body')
                body = json.loads(raw)
                if remote is not None and isinstance(body, dict) and body.get('action') == 'open_workspace':
                    self._json(403, {'error': 'Remote clients can preview or download artifacts; opening computer folders is local only'})
                    return
                command = control.submit(body)
                self._json(202, {'id': command['id'], 'status': command['status']})
            except Exception as exc:
                self._error(exc)

        def reject_write(self):
            self._respond(405, b'Unsupported method')

        do_PUT = do_PATCH = do_DELETE = do_OPTIONS = reject_write

    return Handler


@contextmanager
def start_dashboard(snapshot, port=8765, control=None, remote=None):
    server = ThreadingHTTPServer(('127.0.0.1', port), handler(snapshot, control, remote))
    thread = threading.Thread(target=server.serve_forever, name='control-center', daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
