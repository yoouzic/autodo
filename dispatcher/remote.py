"""Identity boundary for a dedicated loopback backend behind Tailscale Serve.

Only Serve may expose this listener. Local processes are already trusted by the
local control endpoint; arbitrary public proxies must not target this listener.
"""
import re
import threading
import time
from urllib.parse import urlsplit


class RemoteAccess:
    def __init__(self, origin, allowed_users):
        if not isinstance(origin, str):
            raise ValueError('remote_access.origin must be an exact HTTPS origin')
        parsed = urlsplit(origin)
        hostname = parsed.hostname or ''
        if (parsed.scheme != 'https' or parsed.username or parsed.password or
                parsed.path not in ('', '/') or parsed.query or parsed.fragment or
                not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?', hostname) or
                not hostname.endswith('.ts.net') or '..' in hostname):
            raise ValueError('remote_access.origin must name one HTTPS Tailscale Serve host')
        if parsed.port not in (None, 443):
            raise ValueError('The private Serve entry must use HTTPS port 443')
        if not isinstance(allowed_users, list) or not allowed_users or any(
                not isinstance(user, str) or not user or user != user.strip() or
                any(character.isspace() for character in user) or '*' in user for user in allowed_users):
            raise ValueError('remote_access.allowed_users must list exact Tailscale login names')
        self.origin = 'https://' + hostname.lower()
        self.authority = hostname.lower()
        self.allowed_users = frozenset(allowed_users)
        self._lock = threading.Lock()
        self._last = {'last_seen': None, 'user': None, 'source': None}

    @staticmethod
    def _single(headers, name):
        values = headers.get_all(name, [])
        return values[0] if len(values) == 1 else None

    def allowed(self, headers, mutation=False):
        # No localhost bypass exists on this listener, regardless of Host/SNI.
        if self._single(headers, 'Host') != self.authority:
            return False
        if self._single(headers, 'X-Forwarded-Host') != self.authority:
            return False
        if self._single(headers, 'X-Forwarded-Proto') != 'https':
            return False
        user = self._single(headers, 'Tailscale-User-Login')
        if user not in self.allowed_users:
            return False
        for name in set(headers.keys()):
            if name.lower().startswith(('tailscale-', 'x-forwarded-')) and len(headers.get_all(name, [])) != 1:
                return False
        origins = headers.get_all('Origin', [])
        if len(origins) > 1 or (mutation and len(origins) != 1):
            return False
        if origins and origins[0] != self.origin:
            return False
        if headers.get('Sec-Fetch-Site') == 'cross-site':
            # Opening the private link from a chat is a safe authenticated GET.
            # Cross-site scripts and all cross-site writes remain rejected.
            if mutation or origins or headers.get('Sec-Fetch-Mode') != 'navigate' or headers.get('Sec-Fetch-Dest') != 'document':
                return False
        with self._lock:
            self._last = {'last_seen': time.time(), 'user': user,
                          'source': (headers.get('X-Forwarded-For') or '')[:256]}
        return True

    def diagnostics(self):
        with self._lock:
            return {'enabled': True, 'origin': self.origin, **self._last}
