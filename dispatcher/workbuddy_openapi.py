"""Official WorkBuddy local-assistant messaging, not a task executor.

Contract checked 2026-11-08: https://open.workbuddy.cn/docs/openapi
Only the documented localassistant endpoints are used. A message ID means
accepted, never completed. The public contract has no device selector, cwd,
model/effort setting, run correlation, terminal status or cancellation endpoint.
The cloud /tasks API and its ACP terminal events are NOT local-assistant APIs.

The host supplies an OAuth access token; this module does not read desktop
credentials, persist secrets, log requests, log responses or automatically retry.
OAuth authorization-code exchange/refresh belongs to the host's secret handling:
POST TOKEN_ENDPOINT, application/x-www-form-urlencoded, client_id/client_secret
and either grant_type=authorization_code + code + exact redirect_uri, or
grant_type=refresh_token + refresh_token. The host must verify callback state.
"""
from __future__ import annotations

import http.client
import json
import math
import re
import ssl
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from urllib.parse import urlencode, urlsplit


ORIGIN = 'https://www.workbuddy.cn'
API_PREFIX = '/openapi/v2'
TOKEN_ENDPOINT = ORIGIN + API_PREFIX + '/token'
LOCAL_ASSISTANT_SCOPES = ('user.localassistant.readable', 'user.localassistant.invokable')
LOCAL_ASSISTANT_CAPABILITIES = MappingProxyType({
    'online_status': True, 'send_text': True, 'message_history': True,
    'terminal_status': False, 'cancellation': False, 'device_selection': False,
    'cwd_selection': False, 'model_selection': False, 'effort_selection': False,
    'run_correlation': False,
})
# Defensive client bounds, not claims about the provider's maximum input size.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_MESSAGE_BYTES = 64 * 1024
_BEARER = re.compile(r'[A-Za-z0-9._~+/\-]+=*\Z')


class WorkBuddyAPIError(RuntimeError):
    """Fixed diagnostics only; unknown write outcomes must not be retried blindly."""

    def __init__(self, reason, *, http_status=None, may_have_been_accepted=False):
        self.reason = reason
        self.http_status = http_status
        self.may_have_been_accepted = may_have_been_accepted
        super().__init__('WorkBuddy Open API request failed: ' + reason)


@dataclass(frozen=True)
class AcceptedMessage:
    """Transport acceptance only. It does not establish local execution."""

    message_id: str
    delivery_state: str = field(default='accepted', init=False)


def _text(value, *, maximum=1024, nonblank=True):
    return (isinstance(value, str) and len(value) <= maximum and
            (not nonblank or bool(value.strip())) and
            not any(ord(char) < 32 or ord(char) == 127 for char in value))


def build_authorization_url(*, client_id, redirect_uri, state):
    """Prepare an explicit minimal-scope authorization; never open a browser.

    The host must store an unpredictable state bound to its authorization
    session, compare it on callback, and use this exact redirect_uri at exchange.
    Only HTTPS callbacks are supported here; no undocumented loopback exception.
    """
    if not _text(client_id, maximum=256) or not _text(state, maximum=512) or len(state) < 16:
        raise ValueError('OAuth client ID and a session-bound state are required')
    if not _text(redirect_uri, maximum=2048):
        raise ValueError('OAuth requires an exact registered HTTPS callback')
    try:
        parsed = urlsplit(redirect_uri)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None or
                parsed.password is not None or parsed.fragment or parsed.port == 0 or
                any(char.isspace() for char in redirect_uri)):
            raise ValueError()
    except ValueError:
        raise ValueError('OAuth requires an exact registered HTTPS callback') from None
    return ORIGIN + API_PREFIX + '/authorize?' + urlencode({
        'response_type': 'code', 'client_id': client_id, 'redirect_uri': redirect_uri,
        'scope': ' '.join(LOCAL_ASSISTANT_SCOPES), 'state': state,
    })


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError('Non-JSON numeric value')


class WorkBuddyOpenAPI:
    """One bounded HTTPS request per method; no alternate origin or redirects."""

    def __init__(self, access_token, *, timeout=15):
        if (not isinstance(access_token, str) or not 1 <= len(access_token) <= 16384 or
                not _BEARER.fullmatch(access_token)):
            raise ValueError('A valid host-supplied OAuth access token is required')
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or
                not 0 < timeout <= 60):
            raise ValueError('HTTP timeout must be greater than 0 and at most 60 seconds')
        self._access_token = access_token
        self._timeout = timeout

    def __repr__(self):
        return '<WorkBuddyOpenAPI official local-assistant messaging client>'

    def _request(self, method, path, body=None):
        # This private method is still bounded so future callers cannot turn it
        # into an arbitrary authenticated HTTP client or accidentally use /tasks.
        parsed = urlsplit(path)
        if (method not in ('GET', 'POST') or parsed.scheme or parsed.netloc or parsed.fragment or
                parsed.path not in (API_PREFIX + '/localassistant', API_PREFIX + '/localassistant/message') or
                (method == 'POST' and (parsed.path != API_PREFIX + '/localassistant/message' or parsed.query))):
            raise ValueError('Unsupported local-assistant API operation')
        is_write = method == 'POST'
        headers = {'Authorization': 'Bearer ' + self._access_token,
                   'Accept': 'application/json', 'Accept-Encoding': 'identity'}
        encoded = None
        if body is not None:
            encoded = json.dumps(body, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
            headers['Content-Type'] = 'application/json; charset=utf-8'
        connection = None
        status = None
        try:
            connection = http.client.HTTPSConnection('www.workbuddy.cn', timeout=self._timeout,
                                                      context=ssl.create_default_context())
            connection.request(method, path, body=encoded, headers=headers)
            response = connection.getresponse()
            status = response.status
            if type(status) is not int or not 200 <= status < 300:
                # http.client never follows a Location header, including redirects
                # back to this origin. Do not even read a possibly sensitive error.
                raise WorkBuddyAPIError('http_error', http_status=status if type(status) is int else None,
                                        may_have_been_accepted=is_write)
            content_type = response.getheader('Content-Type', '').split(';', 1)[0].strip().lower()
            if content_type != 'application/json':
                raise WorkBuddyAPIError('invalid_response', http_status=status,
                                        may_have_been_accepted=is_write)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise WorkBuddyAPIError('response_too_large', http_status=status,
                                        may_have_been_accepted=is_write)
            try:
                envelope = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                                      parse_constant=_invalid_constant)
            except (ValueError, UnicodeError, RecursionError):
                raise WorkBuddyAPIError('invalid_response', http_status=status,
                                        may_have_been_accepted=is_write) from None
            if (not isinstance(envelope, dict) or type(envelope.get('code')) is not int or
                    'error' in envelope or 'errors' in envelope):
                raise WorkBuddyAPIError('invalid_response', http_status=status,
                                        may_have_been_accepted=is_write)
            if envelope['code'] != 0:
                raise WorkBuddyAPIError('api_rejected', http_status=status,
                                        may_have_been_accepted=is_write)
            if not isinstance(envelope.get('data'), dict):
                raise WorkBuddyAPIError('invalid_response', http_status=status,
                                        may_have_been_accepted=is_write)
            return envelope['data']
        except WorkBuddyAPIError:
            raise
        except (OSError, http.client.HTTPException, ValueError, UnicodeError):
            # No original exception text, headers, URL or vendor body escapes.
            raise WorkBuddyAPIError('transport_error', http_status=status,
                                    may_have_been_accepted=is_write) from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except (OSError, http.client.HTTPException):
                    pass

    def get_local_assistant_status(self):
        """Return the account's online flag; the API does not identify its PC."""
        data = self._request('GET', API_PREFIX + '/localassistant')
        if type(data.get('online')) is not bool:
            raise WorkBuddyAPIError('invalid_response')
        return data['online']

    def send_text(self, content):
        """Send one text message. On uncertain outcome do not blindly retry.

        No cwd/model/effort or device targeting is promised. Permission responses
        are intentionally absent: a messaging client must not approve tools.
        """
        try:
            valid = (isinstance(content, str) and bool(content.strip()) and '\x00' not in content and
                     len(content.encode('utf-8')) <= MAX_MESSAGE_BYTES)
        except UnicodeError:
            valid = False
        if not valid:
            raise ValueError('Message must be bounded nonempty UTF-8 text')
        data = self._request('POST', API_PREFIX + '/localassistant/message',
                             {'content': content, 'msg_type': 'text'})
        if not _text(data.get('message_id')):
            raise WorkBuddyAPIError('invalid_response', may_have_been_accepted=True)
        return AcceptedMessage(data['message_id'])

    def get_messages(self, *, after_message_id=None, limit=None, offset=None):
        """Get opaque messages. No polling, terminal inference or auto-approval.

        Incremental mode and pagination are mutually exclusive. Message ordering,
        task correlation and a complete execution lifecycle are not documented.
        """
        if after_message_id is not None:
            if not _text(after_message_id) or limit is not None or offset is not None:
                raise ValueError('Incremental history requires only a valid message cursor')
            query = {'message_id': after_message_id}
        else:
            limit = 20 if limit is None else limit
            offset = 0 if offset is None else offset
            if (type(limit) is not int or not 1 <= limit <= 100 or
                    type(offset) is not int or not 0 <= offset <= 2**31 - 1):
                raise ValueError('History requires limit 1..100 and a nonnegative integer offset')
            query = {'limit': limit, 'offset': offset}
        data = self._request('GET', API_PREFIX + '/localassistant/message?' + urlencode(query))
        messages = data.get('messages')
        if not isinstance(messages, list) or len(messages) > 10000:
            raise WorkBuddyAPIError('invalid_response')
        seen = set()
        for item in messages:
            if (not isinstance(item, dict) or not _text(item.get('message_id')) or
                    item['message_id'] in seen or item.get('role') not in ('user', 'assistant') or
                    not isinstance(item.get('content'), list) or not _text(item.get('msg_type'), maximum=128) or
                    not isinstance(item.get('attachments'), list) or not isinstance(item.get('metadata'), dict) or
                    not _text(item.get('created_at'), maximum=64)):
                raise WorkBuddyAPIError('invalid_response')
            try:
                if datetime.fromisoformat(item['created_at'].replace('Z', '+00:00')).tzinfo is None:
                    raise ValueError()
            except ValueError:
                raise WorkBuddyAPIError('invalid_response') from None
            seen.add(item['message_id'])
        return messages
