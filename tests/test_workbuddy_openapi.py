"""Documented WorkBuddy local-assistant API; no live accounts or requests."""
import importlib.util
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit


TOKEN = 'unit-test-only-access-token'


class WorkBuddyOpenAPITests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('dispatcher.workbuddy_openapi'),
                             'The official WorkBuddy API client is not implemented')
        from dispatcher import workbuddy_openapi
        return workbuddy_openapi

    def wire(self, payload=None, *, status=200, raw=None, content_type='application/json'):
        module = self.module()
        factory = patch.object(module.http.client, 'HTTPSConnection').start()
        self.addCleanup(patch.stopall)
        connection = factory.return_value
        response = connection.getresponse.return_value
        response.status = status
        response.getheader.return_value = content_type
        response.read.return_value = raw if raw is not None else json.dumps(
            payload if payload is not None else {'code': 0, 'data': {'online': True}}
        ).encode('utf-8')
        return module, factory, connection, response

    def message(self, **changes):
        return dict(message_id='message-2', role='assistant', content=['Done'],
                    msg_type='text', created_at='2026-07-30T10:00:05Z',
                    attachments=[], metadata={}, **changes)

    def test_read_status_uses_only_official_verified_https_and_header_token(self):
        module, factory, connection, response = self.wire()
        client = module.WorkBuddyOpenAPI(TOKEN, timeout=8)
        self.assertTrue(client.get_local_assistant_status())
        self.assertEqual(factory.call_args.args, ('www.workbuddy.cn',))
        self.assertEqual(factory.call_args.kwargs['timeout'], 8)
        context = factory.call_args.kwargs['context']
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, module.ssl.CERT_REQUIRED)
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ('GET', '/openapi/v2/localassistant'))
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer ' + TOKEN)
        self.assertIsNone(kwargs['body'])
        self.assertNotIn(TOKEN, repr(client))
        response.read.assert_called_once_with(module.MAX_RESPONSE_BYTES + 1)
        connection.close.assert_called_once()

    def test_offline_is_a_valid_status_not_an_execution_failure(self):
        module, _, _, _ = self.wire({'code': 0, 'data': {'online': False}})
        self.assertFalse(module.WorkBuddyOpenAPI(TOKEN).get_local_assistant_status())

    def test_send_returns_acceptance_only_and_preserves_unicode(self):
        module, _, connection, _ = self.wire({'code': 0, 'data': {'message_id': 'msg-1'}})
        receipt = module.WorkBuddyOpenAPI(TOKEN).send_text('只处理本次测试任务。')
        self.assertEqual(receipt.message_id, 'msg-1')
        self.assertEqual(receipt.delivery_state, 'accepted')
        self.assertFalse(hasattr(receipt, 'complete'))
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ('POST', '/openapi/v2/localassistant/message'))
        self.assertEqual(json.loads(kwargs['body']),
                         {'content': '只处理本次测试任务。', 'msg_type': 'text'})
        self.assertNotIn(TOKEN.encode(), kwargs['body'])
        self.assertNotIn(TOKEN, args[1])
        self.assertEqual(connection.request.call_count, 1)

    def test_history_keeps_content_and_metadata_without_inferring_completion(self):
        item = self.message()
        item['metadata'] = {'status': 'completed', 'unknown': {'x': 1}}
        module, _, connection, _ = self.wire({'code': 0, 'data': {'messages': [item]}})
        result = module.WorkBuddyOpenAPI(TOKEN).get_messages(limit=100, offset=20)
        self.assertEqual(result, [item])
        self.assertEqual(connection.request.call_args.args,
                         ('GET', '/openapi/v2/localassistant/message?limit=100&offset=20'))

    def test_incremental_cursor_is_encoded_and_not_mixed_with_pagination(self):
        module, _, connection, _ = self.wire({'code': 0, 'data': {'messages': []}})
        client = module.WorkBuddyOpenAPI(TOKEN)
        self.assertEqual(client.get_messages(after_message_id='msg/a?b&c'), [])
        query = urlsplit(connection.request.call_args.args[1]).query
        self.assertEqual(parse_qs(query), {'message_id': ['msg/a?b&c']})
        with self.assertRaises(ValueError):
            client.get_messages(after_message_id='msg-1', limit=10)

    def test_redirect_and_http_errors_are_not_followed_retried_or_echoed(self):
        for status in (301, 302, 307, 308, 400, 401, 403, 429, 500):
            with self.subTest(status=status):
                module, _, connection, response = self.wire(status=status,
                    raw=('error ' + TOKEN).encode())
                with self.assertRaises(module.WorkBuddyAPIError) as caught:
                    module.WorkBuddyOpenAPI(TOKEN).send_text('test')
                error = caught.exception
                self.assertEqual(error.http_status, status)
                self.assertTrue(error.may_have_been_accepted)
                self.assertNotIn(TOKEN, str(error))
                self.assertNotIn(TOKEN, repr(error))
                self.assertEqual(connection.request.call_count, 1)
                response.read.assert_not_called()
                connection.close.assert_called_once()
                patch.stopall()

    def test_timeout_on_write_keeps_delivery_uncertain_without_retry(self):
        module, _, connection, _ = self.wire()
        connection.getresponse.side_effect = TimeoutError('secret=' + TOKEN)
        with self.assertRaises(module.WorkBuddyAPIError) as caught:
            module.WorkBuddyOpenAPI(TOKEN).send_text('test')
        self.assertEqual(caught.exception.reason, 'transport_error')
        self.assertTrue(caught.exception.may_have_been_accepted)
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(connection.request.call_count, 1)
        connection.close.assert_called_once()

    def test_read_network_failure_is_safe_and_not_a_write(self):
        module, _, connection, _ = self.wire()
        connection.request.side_effect = OSError('private ' + TOKEN)
        with self.assertRaises(module.WorkBuddyAPIError) as caught:
            module.WorkBuddyOpenAPI(TOKEN).get_local_assistant_status()
        self.assertFalse(caught.exception.may_have_been_accepted)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_invalid_envelopes_and_json_are_fail_closed(self):
        invalid = [b'not JSON', b'{"code":0,"code":0,"data":{"online":true}}',
                   b'{"code":0,"data":{"online":NaN}}', b'[]',
                   b'{"code":false,"data":{"online":true}}',
                   b'{"code":"0","data":{"online":true}}',
                   b'{"data":{"online":true}}',
                   b'{"code":0,"data":{"online":1}}',
                   b'{"code":0,"data":{"online":true},"error":"private"}']
        for raw in invalid:
            with self.subTest(raw=raw):
                module, _, _, _ = self.wire(raw=raw)
                with self.assertRaises(module.WorkBuddyAPIError):
                    module.WorkBuddyOpenAPI(TOKEN).get_local_assistant_status()
                patch.stopall()

    def test_business_rejection_does_not_expose_vendor_message(self):
        module, _, _, _ = self.wire({'code': 123, 'msg': TOKEN, 'data': {}})
        with self.assertRaises(module.WorkBuddyAPIError) as caught:
            module.WorkBuddyOpenAPI(TOKEN).get_local_assistant_status()
        self.assertEqual(caught.exception.reason, 'api_rejected')
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_non_json_and_oversized_response_rejected(self):
        module = self.module()
        for kwargs in ({'content_type': 'text/html'},
                       {'raw': b' ' * (module.MAX_RESPONSE_BYTES + 1)}):
            with self.subTest(kwargs=list(kwargs)):
                _, _, _, _ = self.wire(**kwargs)
                with self.assertRaises(module.WorkBuddyAPIError):
                    module.WorkBuddyOpenAPI(TOKEN).get_local_assistant_status()
                patch.stopall()

    def test_missing_or_invalid_acceptance_id_is_uncertain(self):
        for data in ({}, {'message_id': ''}, {'message_id': None}):
            with self.subTest(data=data):
                module, _, _, _ = self.wire({'code': 0, 'data': data})
                with self.assertRaises(module.WorkBuddyAPIError) as caught:
                    module.WorkBuddyOpenAPI(TOKEN).send_text('test')
                self.assertTrue(caught.exception.may_have_been_accepted)
                patch.stopall()

    def test_malformed_or_duplicate_history_items_are_rejected(self):
        item = self.message()
        invalid = [None, {}, [dict(item, content='wrong')], [dict(item, role='tool')],
                   [dict(item, attachments=None)], [dict(item, metadata=None)],
                   [dict(item, created_at='yesterday')], [item, item]]
        for items in invalid:
            with self.subTest(items=items):
                module, _, _, _ = self.wire({'code': 0, 'data': {'messages': items}})
                with self.assertRaises(module.WorkBuddyAPIError):
                    module.WorkBuddyOpenAPI(TOKEN).get_messages()
                patch.stopall()

    def test_invalid_inputs_fail_before_any_network(self):
        module, factory, _, _ = self.wire()
        for token in ('', 'a\r\nInjected: yes', 'a b', None):
            with self.subTest(token=token), self.assertRaises(ValueError):
                module.WorkBuddyOpenAPI(token)
        for timeout in (0, 61, float('inf'), float('nan'), True, '10'):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                module.WorkBuddyOpenAPI(TOKEN, timeout=timeout)
        client = module.WorkBuddyOpenAPI(TOKEN)
        for text in ('', '  ', '\0', None, 'x' * (module.MAX_MESSAGE_BYTES + 1)):
            with self.subTest(text_type=type(text)), self.assertRaises(ValueError):
                client.send_text(text)
        for kwargs in ({'limit': 0}, {'limit': 101}, {'limit': True}, {'offset': -1},
                       {'offset': False}, {'after_message_id': ''},
                       {'after_message_id': 'x\r\nAuthorization: ' + TOKEN}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                client.get_messages(**kwargs)
        factory.assert_not_called()

    def test_unpublished_execution_controls_are_explicitly_unavailable(self):
        module = self.module()
        client = module.WorkBuddyOpenAPI(TOKEN)
        for method in ('cancel', 'run', 'wait_for_completion', 'list_devices',
                       'set_model', 'set_cwd', 'set_effort'):
            self.assertFalse(hasattr(client, method))
        self.assertFalse(module.LOCAL_ASSISTANT_CAPABILITIES['terminal_status'])
        self.assertFalse(module.LOCAL_ASSISTANT_CAPABILITIES['cancellation'])
        self.assertFalse(module.LOCAL_ASSISTANT_CAPABILITIES['device_selection'])

    def test_oauth_url_has_minimal_scopes_state_and_no_secret(self):
        module = self.module()
        callback = 'https://dispatcher.example/return?source=workbuddy'
        url = module.build_authorization_url(client_id='app_test', redirect_uri=callback,
                                             state='x' * 32)
        parsed = urlsplit(url)
        self.assertEqual(parsed.scheme, 'https')
        self.assertEqual(parsed.netloc, 'www.workbuddy.cn')
        self.assertEqual(parsed.path, '/openapi/v2/authorize')
        self.assertEqual(parse_qs(parsed.query), {
            'response_type': ['code'], 'client_id': ['app_test'],
            'redirect_uri': [callback], 'state': ['x' * 32],
            'scope': ['user.localassistant.readable user.localassistant.invokable']})
        self.assertEqual(module.TOKEN_ENDPOINT, 'https://www.workbuddy.cn/openapi/v2/token')
        self.assertNotIn(TOKEN, url)
        for changes in ({'state': ''}, {'redirect_uri': 'http://example.com/callback'},
                        {'redirect_uri': 'https://user:pass@example.com/callback'},
                        {'redirect_uri': 'https://example.com/callback#fragment'}):
            values = dict(client_id='app_test', redirect_uri=callback, state='x' * 32)
            values.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                module.build_authorization_url(**values)


if __name__ == '__main__':
    unittest.main()
