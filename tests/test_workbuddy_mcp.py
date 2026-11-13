"""Owned temporary workspaces and local stdio only; never runs an AI model."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


class WorkBuddyMCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'dispatcher'
        self.workspace = self.root / 'workspaces' / 'task' / 'workspace'
        self.workspace.mkdir(parents=True)
        (self.workspace / 'input.txt').write_text('source', encoding='utf-8')

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('dispatcher.workbuddy_mcp'),
                             'The WorkBuddy MCP bridge is not implemented')
        from dispatcher import workbuddy_mcp
        return workbuddy_mcp

    def start(self, **kwargs):
        module = self.module()
        broker = module.WorkBuddyBroker(self.root, **kwargs).start()
        self.addCleanup(broker.close)
        client = module.BridgeClient(self.root)
        self.addCleanup(client.close)
        return broker, client

    def submit(self, broker, **kwargs):
        values = dict(run_id='run-1', workspace=self.workspace, prompt='Write result.txt.',
                      workspace_mode='default', timeout=60)
        values.update(kwargs)
        return broker.submit(**values)

    def claim(self, broker, client, **kwargs):
        self.submit(broker, **kwargs)
        value = client.request('poll')
        self.assertEqual(value['task']['run_id'], 'run-1')
        return value['task']['lease_nonce']

    def test_scoped_file_lifecycle_and_submission_revokes_writes(self):
        broker, client = self.start()
        nonce = self.claim(broker, client)
        self.assertNotIn(str(self.workspace), json.dumps(client.request('status')))
        self.assertEqual(client.request('read_file', lease_nonce=nonce, path='input.txt')['text'], 'source')
        client.request('write_file', lease_nonce=nonce, path='nested/result.txt', text='hello')
        self.assertEqual((self.workspace / 'nested/result.txt').read_text(), 'hello')
        result = client.request('publish_result', lease_nonce=nonce, summary='Ready for review',
                                artifacts=['nested/result.txt'], checks=['read-back'], status='complete', needs_input='')
        self.assertEqual(result, {'state': 'submitted'})
        receipt = broker.result('run-1')
        self.assertEqual(receipt['state'], 'submitted')
        self.assertEqual(receipt['submission']['status'], 'complete')
        self.assertNotIn('approved', receipt)
        with self.assertRaises(self.module().BridgeError):
            client.request('write_file', lease_nonce=nonce, path='later.txt', text='no')
        self.assertFalse((self.workspace / 'later.txt').exists())

    def test_unsafe_paths_and_credentials_never_reach_files(self):
        broker, client = self.start()
        nonce = self.claim(broker, client)
        for name in ('../escape', '/absolute', 'C:/secrets', 'foo\\bar', 'file:stream',
                     'a/../b', '.env', '.ssh/id_rsa', 'tokens.json', '.workbuddy/auth',
                     'NUL.txt', 'trailing.', 'trailing ', '.dispatcher-state', 'a//b'):
            with self.subTest(name=name), self.assertRaises(self.module().BridgeError):
                client.request('write_file', lease_nonce=nonce, path=name, text='no')
        self.assertFalse((self.workspace.parent / 'escape').exists())

    def test_framed_text_preserves_all_body_whitespace_after_vendor_outer_trim(self):
        module=self.module()
        self.assertEqual(getattr(module,'TEXT_FRAME_PREFIX',None),'<<<TODO_TEXT_V1_BEGIN>>>')
        self.assertEqual(getattr(module,'TEXT_FRAME_SUFFIX',None),'<<<TODO_TEXT_V1_END>>>')
        broker,client=self.start()
        nonce=self.claim(broker,client)
        bodies=['READY\n',' \n中文正文\n\n ','\n\n','',
                '内部标记 '+module.TEXT_FRAME_SUFFIX+'\n']
        for index,body in enumerate(bodies):
            with self.subTest(body=repr(body)):
                framed=' \n'+module.TEXT_FRAME_PREFIX+body+module.TEXT_FRAME_SUFFIX+'\n '
                vendor_argument=framed.strip()
                response=client.request('write_file',lease_nonce=nonce,path=f'framed-{index}.txt',
                    text=vendor_argument,text_format='framed')
                self.assertEqual(response['bytes'],len(body.encode('utf-8')))
                self.assertEqual((self.workspace/f'framed-{index}.txt').read_bytes(),body.encode('utf-8'))
                self.assertEqual(client.request('read_file',lease_nonce=nonce,path=f'framed-{index}.txt')['text'],body)

    def test_plain_format_remains_literal_and_never_auto_decodes_markers(self):
        module=self.module()
        broker,client=self.start()
        nonce=self.claim(broker,client)
        text=getattr(module,'TEXT_FRAME_PREFIX','<<<TODO_TEXT_V1_BEGIN>>>')+'中文\n'+getattr(module,'TEXT_FRAME_SUFFIX','<<<TODO_TEXT_V1_END>>>')
        for name,extra in [('default.txt',{}),('explicit.txt',{'text_format':'plain'})]:
            client.request('write_file',lease_nonce=nonce,path=name,text=text,**extra)
            self.assertEqual((self.workspace/name).read_text(encoding='utf-8'),text)

    def test_malformed_frames_or_unknown_format_cannot_modify_existing_file(self):
        module=self.module()
        prefix=getattr(module,'TEXT_FRAME_PREFIX','<<<TODO_TEXT_V1_BEGIN>>>')
        suffix=getattr(module,'TEXT_FRAME_SUFFIX','<<<TODO_TEXT_V1_END>>>')
        broker,client=self.start()
        nonce=self.claim(broker,client)
        client.request('write_file',lease_nonce=nonce,path='preserve.txt',text='keep')
        cases=[('', 'framed'),('READY\n','framed'),(prefix+'READY\n','framed'),
               ('READY\n'+suffix,'framed'),(' '+prefix+'READY\n'+suffix,'framed'),
               (prefix+'READY\n'+suffix+' ','framed'),(prefix+suffix,'unknown'),
               (prefix+suffix,None),(None,'framed'),(prefix+'\0'+suffix,'framed')]
        for text,format_value in cases:
            with self.subTest(text=repr(text),format=format_value),self.assertRaises(module.BridgeError):
                client.request('write_file',lease_nonce=nonce,path='preserve.txt',text=text,text_format=format_value)
            self.assertEqual((self.workspace/'preserve.txt').read_bytes(),b'keep')

    def test_framing_has_no_effect_on_decoded_utf8_file_size_limit(self):
        module=self.module()
        prefix=getattr(module,'TEXT_FRAME_PREFIX','<<<TODO_TEXT_V1_BEGIN>>>')
        suffix=getattr(module,'TEXT_FRAME_SUFFIX','<<<TODO_TEXT_V1_END>>>')
        broker,client=self.start()
        nonce=self.claim(broker,client)
        body='x'*module.MAX_FILE
        result=client.request('write_file',lease_nonce=nonce,path='boundary.txt',text=prefix+body+suffix,text_format='framed')
        self.assertEqual(result['bytes'],module.MAX_FILE)
        for body in ('x'*(module.MAX_FILE+1),'中'*(module.MAX_FILE//3+1)):
            with self.assertRaises(module.BridgeError):
                client.request('write_file',lease_nonce=nonce,path='oversized.txt',text=prefix+body+suffix,text_format='framed')
        self.assertFalse((self.workspace/'oversized.txt').exists())

    def test_tool_schema_advertises_optional_explicit_text_format(self):
        module=self.module()
        tool=next(item for item in module.TOOLS if item['name']=='write_file')
        self.assertNotIn('text_format',tool['inputSchema']['required'])
        self.assertEqual(tool['inputSchema']['properties'].get('text_format',{}).get('enum'),['plain','framed'])
        self.assertIn('<<<TODO_TEXT_V1_BEGIN>>>',tool['description'])
        self.assertIn('<<<TODO_TEXT_V1_END>>>',tool['description'])

    def test_hardlinks_are_rejected_for_reads_and_writes(self):
        broker, client = self.start()
        outside = self.root / 'outside.txt'
        outside.write_text('private')
        os.link(outside, self.workspace / 'linked.txt')
        nonce = self.claim(broker, client)
        for operation in ('read_file', 'write_file'):
            params = {'text': 'changed'} if operation == 'write_file' else {}
            with self.assertRaises(self.module().BridgeError):
                client.request(operation, lease_nonce=nonce, path='linked.txt', **params)
        self.assertEqual(outside.read_text(), 'private')

    def test_symlinks_are_rejected_without_exposing_targets(self):
        outside = self.root / 'outside'
        outside.mkdir()
        try:
            (self.workspace / 'linked').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Creating test symlinks requires Windows developer permissions')
        broker, client = self.start()
        nonce = self.claim(broker, client)
        with self.assertRaises(self.module().BridgeError):
            client.request('write_file', lease_nonce=nonce, path='linked/escape.txt', text='no')
        self.assertFalse((outside / 'escape.txt').exists())

    def test_wrong_nonce_second_connection_and_wrong_epoch_are_denied(self):
        broker, client = self.start()
        nonce = self.claim(broker, client)
        with self.assertRaises(self.module().BridgeError):
            client.request('read_file', lease_nonce='0' * 64, path='input.txt')
        with self.assertRaises(self.module().BridgeError):
            self.module().BridgeClient(self.root)
        original = client.epoch
        client.epoch = '0' * 64
        with self.assertRaises(self.module().BridgeError):
            client.request('write_file', lease_nonce=nonce, path='bad.txt', text='no')
        client.epoch = original
        self.assertFalse((self.workspace / 'bad.txt').exists())

    def test_cancel_timeout_and_disconnect_all_revoke_nonce(self):
        for reason in ('cancel', 'timeout', 'disconnect'):
            with self.subTest(reason=reason):
                current = [100.0]
                broker, client = self.start(clock=lambda: current[0])
                nonce = self.claim(broker, client, timeout=1)
                if reason == 'cancel':
                    self.assertTrue(broker.cancel('run-1'))
                elif reason == 'timeout':
                    current[0] += 2
                else:
                    client.close()
                with self.assertRaises(self.module().BridgeError):
                    client.request('write_file', lease_nonce=nonce, path='never.txt', text='no')
                self.assertIn(broker.result('run-1')['state'], ('cancelled', 'expired', 'disconnected'))
                client.close()
                broker.close()
        self.assertFalse((self.workspace / 'never.txt').exists())

    def test_lost_heartbeat_revokes_without_new_model_action(self):
        current = [100.0]
        broker, client = self.start(clock=lambda: current[0], heartbeat_timeout=2)
        nonce = self.claim(broker, client)
        current[0] += 3
        self.assertFalse(broker.status()['online'])
        self.assertEqual(broker.result('run-1')['state'], 'disconnected')
        with self.assertRaises(self.module().BridgeError):
            client.request('read_file', lease_nonce=nonce, path='input.txt')

    def test_disconnect_during_pending_wait_allows_a_fresh_connector(self):
        broker, client = self.start()
        client.request('wait_started', progress_token_seen=True)
        client.request('poll')
        self.assertTrue(broker.status()['wait_active'])
        client.close()
        self.assertFalse(broker.status()['wait_active'])
        replacement = self.module().BridgeClient(self.root)
        self.addCleanup(replacement.close)
        replacement.request('wait_started', progress_token_seen=False)
        replacement.request('poll')
        self.assertEqual(broker.status()['wait_calls'], 2)
        self.assertFalse(broker.status()['progress_token_seen'])
        self.assertTrue(broker.status()['ready'])

    def test_restarting_broker_never_reuses_old_connection(self):
        broker, client = self.start()
        first_epoch = client.epoch
        broker.close()
        other = self.module().WorkBuddyBroker(self.root).start()
        self.addCleanup(other.close)
        replacement = self.module().BridgeClient(self.root)
        self.addCleanup(replacement.close)
        self.assertNotEqual(replacement.epoch, first_epoch)
        with self.assertRaises(self.module().BridgeError):
            client.request('poll')
        self.assertIsNone(replacement.request('poll')['task'])

    def test_second_broker_cannot_replace_live_discovery(self):
        broker, client = self.start()
        path = self.root / 'data/workbuddy-bridge.json'
        before = path.read_bytes()
        contender = self.module().WorkBuddyBroker(self.root)
        self.addCleanup(contender.close)
        with self.assertRaises(self.module().BridgeError): contender.start()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(client.request('heartbeat'), {'state': 'alive'})
        self.assertTrue(broker.status()['online'])

    def test_start_failure_releases_instance_lock(self):
        module = self.module()
        failed = module.WorkBuddyBroker(self.root)
        with patch.object(module, '_private_acl', side_effect=module.BridgeError('test_failure')):
            with self.assertRaises(module.BridgeError): failed.start()
        self.assertIsNone(failed._instance_lock)
        self.assertIsNone(failed._server)
        broker, client = self.start()
        self.assertTrue(broker.status()['online'])

    def test_probe_bound_to_old_wait_cannot_be_claimed_by_new_wait(self):
        broker, client = self.start()
        client.request('wait_started', progress_token_seen=True)
        client.request('poll')
        run_id = 'probe-' + 'a' * 32
        self.submit(broker, run_id=run_id)
        status = broker.status()
        broker._bind_probe_wait(run_id, status['wait_calls'], status['connection_generation'])
        client.request('cancel_lease')
        client.request('wait_ended')
        client.request('wait_started', progress_token_seen=True)
        self.assertIsNone(client.request('poll')['task'])
        receipt = broker.result(run_id)
        self.assertEqual(receipt['state'], 'cancelled')
        self.assertFalse(receipt['claim_evidence']['matched'])
        self.assertEqual(receipt['claim_evidence']['actual']['wait_calls'], 2)

    def test_higher_priority_job_invalidates_probe_wait_reservation(self):
        broker, client = self.start()
        client.request('wait_started', progress_token_seen=True)
        client.request('poll')
        run_id = 'probe-' + 'b' * 32
        self.submit(broker, run_id=run_id)
        status = broker.status()
        broker._bind_probe_wait(run_id, status['wait_calls'], status['connection_generation'])
        other = self.root / 'workspaces/other/workspace'
        other.mkdir(parents=True)
        self.submit(broker, run_id='real-task', workspace=other, priority=9)
        self.assertEqual(client.request('poll')['task']['run_id'], 'real-task')
        receipt = broker.result(run_id)
        self.assertEqual(receipt['state'], 'cancelled')
        self.assertTrue(receipt['claim_evidence']['preempted'])
        self.assertFalse(receipt['claim_evidence']['matched'])

    def test_workspace_mode_settings_and_overlapping_runs_fail_explicitly(self):
        broker, client = self.start()
        for kwargs in ({'workspace_mode': 'existing'}, {'model': 'auto'}, {'effort': 'low'},
                       {'workspace': self.root / 'data'}, {'workspace': self.root}):
            with self.subTest(kwargs=kwargs), self.assertRaises(self.module().BridgeError):
                self.submit(broker, **kwargs)
        self.submit(broker)
        with self.assertRaises(self.module().BridgeError):
            self.submit(broker, run_id='run-2')
        self.assertEqual(broker.result('run-1')['state'], 'queued')

    def test_publish_requires_real_artifacts_and_does_not_approve(self):
        broker, client = self.start()
        nonce = self.claim(broker, client)
        with self.assertRaises(self.module().BridgeError):
            client.request('publish_result', lease_nonce=nonce, summary='done', status='complete',
                           artifacts=['missing.txt'], checks=[])
        self.assertEqual(broker.result('run-1')['state'], 'leased')
        with self.assertRaises(self.module().BridgeError):
            client.request('publish_result', lease_nonce=nonce, summary='done', status='approved',
                           artifacts=[], checks=[])

    def test_http_rejects_browser_origin_and_does_not_offer_admin_operations(self):
        broker, client = self.start()
        with self.assertRaises(self.module().BridgeError):
            client.request('submit', prompt='injected')
        module = self.module()
        connection = module.http.client.HTTPConnection('127.0.0.1', client.port, timeout=2)
        connection.request('POST', '/worker', body=b'{}', headers={'Origin': 'https://evil.example',
            'Authorization': 'Bearer ' + client.token, 'X-Bridge-Epoch': client.epoch,
            'Content-Type': 'application/json'})
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        connection.close()

    def test_mcp_utf8_stdio_task_even_when_python_environment_requests_gbk(self):
        broker, _client = self.start()
        _client.close()
        process = subprocess.Popen([sys.executable, '-u', '-m', 'dispatcher.workbuddy_mcp',
            '--root', str(self.root), '--idle-seconds', '3', '--progress-seconds', '0.05'],
            cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
            env=dict(os.environ, PYTHONUTF8='0', PYTHONIOENCODING='gbk'),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        def cleanup():
            if process.poll() is None:
                process.kill(); process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream and not stream.closed: stream.close()
        self.addCleanup(cleanup)
        def send(value):
            process.stdin.write(json.dumps(value) + '\n'); process.stdin.flush()
        def receive():
            line = process.stdout.readline()
            self.assertTrue(line, process.stderr.read() if process.poll() is not None else 'No MCP output')
            return json.loads(line)
        send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}})
        self.assertEqual(receive()['result']['protocolVersion'], '2025-03-26')
        send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        send({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        tools = receive()['result']['tools']
        self.assertEqual({item['name'] for item in tools},
                         {'wait_for_task', 'read_file', 'write_file', 'list_files', 'publish_result'})
        send({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {
            'name': 'wait_for_task', 'arguments': {}, '_meta': {'progressToken': 'progress-3'}}})
        notice = receive()
        self.assertEqual(notice['method'], 'notifications/progress')
        self.assertEqual(notice['params']['progressToken'], 'progress-3')
        self.submit(broker, prompt='请使用 MCP 写入并回读中文。')
        while True:
            result = receive()
            if result.get('id') == 3: break
        task = json.loads(result['result']['content'][0]['text'])['task']
        self.assertEqual(task['prompt'], '请使用 MCP 写入并回读中文。')
        self.assertNotIn(str(self.workspace), json.dumps(task))
        body=' \n通过 MCP 收到的中文\n\n '
        framed=self.module().TEXT_FRAME_PREFIX+body+self.module().TEXT_FRAME_SUFFIX
        send({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {
            'name': 'write_file', 'arguments': {'lease_nonce': task['lease_nonce'],
                'path': 'result.txt', 'text': framed.strip(), 'text_format': 'framed'}}})
        self.assertFalse(receive()['result'].get('isError', False))
        send({'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {
            'name': 'read_file', 'arguments': {'lease_nonce': task['lease_nonce'], 'path': 'result.txt'}}})
        returned = json.loads(receive()['result']['content'][0]['text'])
        self.assertEqual(returned['text'],body)
        process.stdin.close()
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0, process.stderr.read())
        self.assertEqual(broker.result('run-1')['state'], 'disconnected')
        self.assertEqual((self.workspace / 'result.txt').read_bytes(),body.encode('utf-8'))
        process.stdout.close(); process.stderr.close()

    def test_absolute_launcher_handshakes_from_unrelated_working_directory(self):
        broker, client=self.start()
        client.close()
        source_root=Path(__file__).resolve().parents[1]
        launcher=source_root/'scripts/workbuddy-mcp-worker.py'
        self.assertTrue(launcher.is_file(),'Absolute-path MCP launcher is not implemented')
        messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{
            'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'cwd-regression','version':'1'}}},
            {'jsonrpc':'2.0','method':'notifications/initialized'},
            {'jsonrpc':'2.0','id':2,'method':'tools/list'}]
        payload=''.join(json.dumps(value)+'\n' for value in messages)
        env=dict(os.environ,PYTHONUTF8='0',PYTHONIOENCODING='gbk')
        env.pop('PYTHONPATH',None)
        result=subprocess.run([sys.executable,'-B',str(launcher),'--root',str(self.root)],
            cwd=self.temp.name,input=payload,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            text=True,encoding='utf-8',env=env,timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        self.assertEqual(result.returncode,0,result.stderr)
        responses=[json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([value['id'] for value in responses],[1,2])
        self.assertEqual(len(responses[1]['result']['tools']),5)
        self.assertEqual(broker.status()['wait_calls'],0)
        self.assertFalse(broker.status()['online'])


if __name__ == '__main__':
    unittest.main()
