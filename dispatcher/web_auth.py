"""Single-owner browser sessions for the authenticated HTTPS relay.

The relay secret authenticates the ingress, never the browser user. Passwords,
setup tickets and browser sessions remain on the execution computer.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit


COOKIE = '__Host-dispatcher_session'
OWNER_USERNAME = 'autodo'
ITERATIONS = 600_000
SESSION_SECONDS = 7 * 24 * 60 * 60
SETUP_SECONDS = 30 * 60

STYLE = '''*{box-sizing:border-box}body{margin:0;background:#f4f2ec;color:#29333e;font:15px/1.7 system-ui,sans-serif;min-height:100vh;display:grid;place-items:center;padding:24px}main{width:min(100%,430px);background:#fffefa;border:1px solid #d4d3c9;border-radius:18px;padding:32px;box-shadow:0 12px 40px #29333e0a}h1{font-size:26px;margin:8px 0}p{color:#646c75}label{display:block;margin-top:18px}input,button{font:inherit;width:100%;padding:12px;border-radius:8px;border:1px solid #9299a0}input{margin-top:6px;background:#fffefa}button{margin-top:24px;background:#465a70;color:white;cursor:pointer}button:disabled{opacity:.6;cursor:wait}.eyebrow{font-size:12px;letter-spacing:.12em;color:#746247}.note{font-size:12px}#message{white-space:pre-wrap;color:#9c3932;min-height:1.7em;margin-top:15px}[hidden]{display:none!important}a{color:#465a70}'''

SCRIPT = r'''(() => {
  'use strict';
  const form=document.getElementById('auth-form'), message=document.getElementById('message');
  const setup=form.dataset.mode==='setup';
  const ticket=setup?new URLSearchParams(location.hash.slice(1)).get('setup'):null;
  if(location.hash)history.replaceState(null,'',location.pathname);
  if(setup&&!ticket){message.textContent='请使用电脑生成的一次性设置链接。';form.hidden=true;return;}
  form.addEventListener('submit',async event=>{
    event.preventDefault();message.textContent='';
    const password=document.getElementById('password').value;
    if(setup&&password!==document.getElementById('confirm').value){message.textContent='两次密码不一致。';return;}
    const button=form.querySelector('button');button.disabled=true;
    try{
      const response=await fetch(setup?'/auth/setup':'/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(setup?{token:ticket,password}:{username:document.getElementById('username').value,password})});
      const data=await response.json();
      if(!response.ok)throw Error(data.error||'请求失败，请稍后重试。');
      location.replace('/');
    }catch(error){message.textContent=error.message;}finally{button.disabled=false;}
  });
})();'''


def _page(setup):
    title = '设置你的登录密码' if setup else '登录派工台'
    description = f'账号为 {OWNER_USERNAME}。请设置至少 12 个字符的独立密码。' if setup else '使用你的专属账号，管理电脑上的智能体任务。'
    confirmation = '<label>再次输入密码<input id="confirm" type="password" autocomplete="new-password" required minlength="12" maxlength="1024"></label>' if setup else ''
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><title>{title}</title><link rel="stylesheet" href="/auth/style.css"><script defer src="/auth/app.js"></script></head><body><main><div class="eyebrow">本机智能体 · 远程派工台</div><h1>{title}</h1><p>{description}</p><form id="auth-form" data-mode="{'setup' if setup else 'login'}"><label>账号<input id="username" value="{OWNER_USERNAME}" autocomplete="username" readonly></label><label>密码<input id="password" type="password" autocomplete="{'new-password' if setup else 'current-password'}" required minlength="{'12' if setup else '1'}" maxlength="1024"></label>{confirmation}<button type="submit">{'设置密码并进入' if setup else '登录'}</button></form><div id="message" role="status"></div><p class="note">任务与文件仍由你的电脑处理。请保持电脑在线。</p></main></body></html>'''


class PasswordAccess:
    def __init__(self, origin, proxy_secret, db_path):
        if not isinstance(origin, str):
            raise ValueError('A precise HTTPS relay origin is required')
        parsed = urlsplit(origin)
        hostname = parsed.hostname or ''
        if (parsed.scheme != 'https' or parsed.username or parsed.password or
                parsed.path not in ('', '/') or parsed.query or parsed.fragment or
                parsed.port not in (None, 443) or
                not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', hostname) or
                '.' not in hostname or '..' in hostname):
            raise ValueError('A precise HTTPS relay origin on port 443 is required')
        if not isinstance(proxy_secret, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', proxy_secret):
            raise ValueError('The relay proxy secret must contain 32–128 URL-safe random characters')
        self.origin, self.authority = 'https://' + hostname.lower(), hostname.lower()
        self.proxy_secret = proxy_secret
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._attempts = defaultdict(deque)
        self._global_attempts = deque()
        self._last = {'last_seen': None, 'user': None, 'source': None}
        with self._connect() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS auth_user (
                    id INTEGER PRIMARY KEY CHECK(id=1), salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL, iterations INTEGER NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS auth_setup (
                    id INTEGER PRIMARY KEY CHECK(id=1), token_hash TEXT NOT NULL, expires_at REAL NOT NULL);
            ''')
        if os.name != 'nt':
            self.db_path.chmod(0o600)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _single(headers, name):
        values = headers.get_all(name, [])
        return values[0] if len(values) == 1 else None

    @staticmethod
    def _digest(value):
        return hashlib.sha256(value.encode('utf-8')).hexdigest()

    @staticmethod
    def _source(headers):
        value = headers.get('X-Forwarded-For', '').split(',')[-1].strip()
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            return 'unknown'

    def ingress_allowed(self, headers):
        if (self._single(headers, 'Host') != self.authority or
                self._single(headers, 'X-Forwarded-Host') != self.authority or
                self._single(headers, 'X-Forwarded-Proto') != 'https'):
            return False
        supplied = self._single(headers, 'X-Dispatcher-Proxy-Secret')
        if not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode('utf-8'), self.proxy_secret.encode('ascii')):
            return False
        return all(len(headers.get_all(name, [])) == 1 for name in set(headers.keys())
                   if name.lower().startswith(('x-forwarded-', 'x-dispatcher-proxy-')))

    def _origin_allowed(self, headers, mutation=False):
        origins = headers.get_all('Origin', [])
        if len(origins) > 1 or (mutation and len(origins) != 1) or (origins and origins[0] != self.origin):
            return False
        if headers.get('Sec-Fetch-Site') == 'cross-site':
            return not mutation and not origins and headers.get('Sec-Fetch-Mode') == 'navigate' and headers.get('Sec-Fetch-Dest') == 'document'
        return True

    def _session_token(self, headers):
        value = self._single(headers, 'Cookie')
        if not value or sum(part.strip().split('=', 1)[0] == COOKIE for part in value.split(';')) != 1:
            return None
        try:
            cookie = SimpleCookie(value)
            token = cookie[COOKIE].value
        except (CookieError, KeyError):
            return None
        return token if re.fullmatch(r'[A-Za-z0-9_-]{43}', token) else None

    def authenticated(self, headers):
        token = self._session_token(headers)
        if token is None:
            return False
        with self._connect() as connection:
            row = connection.execute('SELECT expires_at FROM auth_sessions WHERE token_hash=?', (self._digest(token),)).fetchone()
        return bool(row and row['expires_at'] > time.time())

    def allowed(self, headers, mutation=False):
        if not self.ingress_allowed(headers) or not self._origin_allowed(headers, mutation) or not self.authenticated(headers):
            return False
        with self._lock:
            self._last = {'last_seen': time.time(), 'user': OWNER_USERNAME, 'source': self._source(headers)}
        return True

    def has_user(self):
        with self._connect() as connection:
            return connection.execute('SELECT 1 FROM auth_user WHERE id=1').fetchone() is not None

    def create_setup_link(self):
        token = secrets.token_urlsafe(32)
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute('SELECT 1 FROM auth_user WHERE id=1').fetchone():
                raise RuntimeError('The owner is already configured')
            connection.execute('INSERT OR REPLACE INTO auth_setup VALUES (1, ?, ?)',
                               (self._digest(token), time.time() + SETUP_SECONDS))
        return self.origin + '/auth/setup#setup=' + token

    def _rate_allowed(self, headers):
        now, source = time.monotonic(), self._source(headers)
        with self._lock:
            for key in list(self._attempts):
                queue = self._attempts[key]
                while queue and queue[0] <= now - 60:
                    queue.popleft()
                if not queue:
                    del self._attempts[key]
            while self._global_attempts and self._global_attempts[0] <= now - 60:
                self._global_attempts.popleft()
            queue = self._attempts[source]
            if len(queue) >= 5 or len(self._global_attempts) >= 20:
                return False
            queue.append(now)
            self._global_attempts.append(now)
            return True

    @staticmethod
    def _password(value, setup=False):
        if not isinstance(value, str) or not (12 if setup else 1) <= len(value) <= 1024:
            raise ValueError('密码至少需要 12 个字符。' if setup else '账号或密码不正确。')
        return value.encode('utf-8')

    def _new_session(self, connection):
        token = secrets.token_urlsafe(32)
        connection.execute('DELETE FROM auth_sessions WHERE expires_at<=?', (time.time(),))
        connection.execute('INSERT INTO auth_sessions VALUES (?, ?)', (self._digest(token), time.time() + SESSION_SECONDS))
        return token

    def _setup(self, ticket, password):
        encoded = self._password(password, setup=True)
        if not isinstance(ticket, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', ticket):
            raise PermissionError('设置链接无效或已过期，请从电脑重新生成。')
        digest = self._digest(ticket)
        def valid(connection):
            row = connection.execute('SELECT * FROM auth_setup WHERE id=1').fetchone()
            return (row and row['expires_at'] > time.time() and hmac.compare_digest(row['token_hash'], digest)
                    and not connection.execute('SELECT 1 FROM auth_user WHERE id=1').fetchone())
        with self._connect() as connection:
            if not valid(connection):
                raise PermissionError('设置链接无效或已过期，请从电脑重新生成。')
        salt = secrets.token_bytes(32)
        hashed = hashlib.pbkdf2_hmac('sha256', encoded, salt, ITERATIONS)
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if not valid(connection):
                raise PermissionError('设置链接无效或已过期，请从电脑重新生成。')
            connection.execute('INSERT INTO auth_user VALUES (1, ?, ?, ?, ?)', (salt, hashed, ITERATIONS, time.time()))
            connection.execute('DELETE FROM auth_setup')
            return self._new_session(connection)

    def _login(self, username, password):
        encoded = self._password(password)
        with self._connect() as connection:
            user = connection.execute('SELECT * FROM auth_user WHERE id=1').fetchone()
        salt = user['salt'] if user else b'no-owner-configured'.ljust(32, b'\x00')
        candidate = hashlib.pbkdf2_hmac('sha256', encoded, salt, user['iterations'] if user else ITERATIONS)
        if not user or username != OWNER_USERNAME or not hmac.compare_digest(candidate, user['password_hash']):
            raise PermissionError('账号或密码不正确。')
        with self._connect() as connection:
            return self._new_session(connection)

    @staticmethod
    def _json(handler, status, payload, cookie=None):
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        handler._headers(status, len(data), 'application/json; charset=utf-8', {'Set-Cookie': cookie} if cookie else None)
        handler.wfile.write(data)

    @staticmethod
    def _cookie(token, delete=False):
        return f'{COOKIE}={token}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age={0 if delete else SESSION_SECONDS}'

    def handle_auth(self, handler):
        parsed = urlsplit(handler.path)
        if not parsed.path.startswith('/auth/'):
            return False
        if not self.ingress_allowed(handler.headers) or not self._origin_allowed(handler.headers, handler.command == 'POST'):
            self._json(handler, 403, {'error': 'Private access denied'})
            return True
        path = parsed.path
        if handler.command == 'GET':
            if path in ('/auth/login', '/auth/setup'):
                handler._respond(200, _page(path == '/auth/setup').encode('utf-8'), 'text/html; charset=utf-8')
            elif path == '/auth/app.js':
                handler._respond(200, SCRIPT.encode('utf-8'), 'text/javascript; charset=utf-8')
            elif path == '/auth/style.css':
                handler._respond(200, STYLE.encode('utf-8'), 'text/css; charset=utf-8')
            else:
                handler._respond(404, b'Not found')
            return True
        if handler.command != 'POST' or path not in ('/auth/login', '/auth/setup', '/auth/logout'):
            handler._respond(405, b'Method not allowed')
            return True
        try:
            if path == '/auth/logout':
                if not self.authenticated(handler.headers):
                    self._json(handler, 401, {'error': '请先登录。'})
                    return True
                token = self._session_token(handler.headers)
                with self._connect() as connection:
                    connection.execute('DELETE FROM auth_sessions WHERE token_hash=?', (self._digest(token),))
                self._json(handler, 200, {'ok': True}, self._cookie('', delete=True))
                return True
            if not self._rate_allowed(handler.headers):
                self._json(handler, 429, {'error': '尝试次数较多，请一分钟后重试。'})
                return True
            lengths = handler.headers.get_all('Content-Length', [])
            if len(lengths) != 1 or handler.headers.get('Transfer-Encoding') or handler.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                raise ValueError('需要有效的 JSON 请求。')
            length = int(lengths[0])
            if not 1 <= length <= 8192:
                raise ValueError('请求过大或为空。')
            handler.connection.settimeout(10)
            raw = handler.rfile.read(length)
            if len(raw) != length:
                raise ValueError('请求不完整。')
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError('需要有效的 JSON 请求。')
            if path == '/auth/setup':
                token = self._setup(body.get('token'), body.get('password'))
            else:
                token = self._login(body.get('username'), body.get('password'))
            self._json(handler, 200, {'ok': True}, self._cookie(token))
        except PermissionError as exc:
            self._json(handler, 403 if path == '/auth/setup' else 401, {'error': str(exc)})
        except (ValueError, TypeError, UnicodeError):
            self._json(handler, 400, {'error': '请求无效；首次设置密码至少需要 12 个字符。'})
        return True

    def diagnostics(self):
        with self._lock:
            return {'enabled': True, 'mode': 'password_proxy', 'origin': self.origin, **self._last}
