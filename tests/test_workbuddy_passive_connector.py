"""Ordinary chats must not acquire the dispatch slot; owned subprocesses only."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from dispatcher.workbuddy_mcp import BridgeClient, WorkBuddyBroker


class ConnectorProcess:
    def __init__(self, root, provider='workbuddy'):
        self.process = subprocess.Popen([
            sys.executable, '-B', str(Path(__file__).resolve().parents[1] /
                                      f'scripts/{provider}-mcp-worker.py'),
            '--root', str(root), '--idle-seconds', '2', '--progress-seconds', '0.05'],
            cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.messages = queue.Queue()
        def read():
            try:
                for line in self.process.stdout:
                    self.messages.put(json.loads(line))
            finally:
                self.messages.put(None)
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def send(self, method, request_id=None, params=None):
        value = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
        if request_id is not None:
            value['id'] = request_id
        self.process.stdin.write(json.dumps(value) + '\n')
        self.process.stdin.flush()

    def receive(self, request_id):
        deadline = time.monotonic() + 8
        while True:
            try:
                value = self.messages.get(timeout=max(.01, deadline - time.monotonic()))
            except queue.Empty:
                raise AssertionError('Connector did not respond') from None
            if value is None:
                raise AssertionError('Connector exited before replying')
            if value.get('id') == request_id:
                return value

    def call(self, name, request_id, **arguments):
        self.send('tools/call', request_id, {'name': name, 'arguments': arguments})
        return self.receive(request_id)['result']

    def close(self):
        if not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)
        self.reader.join(timeout=2)
        self.process.stdout.close()
        self.process.stderr.close()


class PassiveWorkBuddyConnectorTests(unittest.TestCase):
    provider = 'workbuddy'

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='todo-passive-mcp-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / 'workspaces/task/workspace'
        self.workspace.mkdir(parents=True)

    def broker(self):
        broker = WorkBuddyBroker(self.root, provider=self.provider).start()
        self.addCleanup(broker.close)
        return broker

    def connector(self):
        connector = ConnectorProcess(self.root, provider=self.provider)
        self.addCleanup(connector.close)
        connector.send('initialize', 1, {'protocolVersion': '2025-03-26'})
        self.assertIn('result', connector.receive(1))
        connector.send('notifications/initialized')
        return connector

    def ping(self, connector, request_id=90):
        connector.send('ping', request_id)
        self.assertEqual(connector.receive(request_id)['result'], {})

    def wait_ready(self, broker):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if broker.status()['ready']:
                return
            time.sleep(.01)
        self.fail('Explicit wait did not become ready')

    def test_two_ordinary_chats_list_tools_without_reserving_a_connection(self):
        broker = self.broker()
        first, second = self.connector(), self.connector()
        for connector in (first, second):
            connector.send('tools/list', 2)
            self.assertEqual(len(connector.receive(2)['result']['tools']), 5)
        time.sleep(1.2)  # Cross a heartbeat tick: idle helpers must remain passive.
        self.ping(first)
        self.ping(second)
        self.assertEqual(broker.status()['connection_generation'], 0)
        self.assertFalse(broker.status()['online'])

    def test_offline_dispatcher_does_not_break_ordinary_chat_initialization(self):
        connector = self.connector()
        connector.send('tools/list', 2)
        self.assertEqual(len(connector.receive(2)['result']['tools']), 5)
        reply = connector.call('read_file', 3, path='secret.txt', lease_nonce='a' * 64)
        self.assertTrue(reply['isError'])
        self.ping(connector)
        self.assertFalse((self.root / 'data').exists())

    def test_passive_chat_does_not_block_dedicated_wait_and_file_submission(self):
        broker = self.broker()
        ordinary, dedicated = self.connector(), self.connector()
        dedicated.send('tools/call', 2, {'name': 'wait_for_task', 'arguments': {}})
        self.wait_ready(broker)
        broker.submit('owned-job', self.workspace, 'Write a fixed fixture file.')
        task = json.loads(dedicated.receive(2)['result']['content'][0]['text'])['task']
        nonce = task['lease_nonce']
        # Closing an unrelated connector must not revoke the real task.
        ordinary.close()
        self.assertEqual(broker.result('owned-job')['state'], 'leased')
        written = dedicated.call('write_file', 3, lease_nonce=nonce, path='result.txt', text='EXACT\n')
        self.assertFalse(written['isError'])
        submitted = dedicated.call('publish_result', 4, lease_nonce=nonce, status='complete',
                                   summary='fixture', artifacts=['result.txt'], checks=[], needs_input='')
        self.assertFalse(submitted['isError'])
        self.assertEqual(broker.result('owned-job')['state'], 'submitted')
        self.assertEqual((self.workspace / 'result.txt').read_bytes(), b'EXACT\n')

    def test_busy_owner_is_not_stolen_or_cancelled_by_another_chat(self):
        broker = self.broker()
        owner = BridgeClient(self.root, provider=self.provider)
        self.addCleanup(owner.close)
        owner.request('wait_started', progress_token_seen=False)
        owner.request('poll')
        broker.submit('owned-job', self.workspace, 'Keep the existing lease.')
        nonce = owner.request('poll')['task']['lease_nonce']
        ordinary = self.connector()
        reply = ordinary.call('wait_for_task', 2)
        self.assertTrue(reply['isError'])
        self.assertIn('worker_already_connected', reply['content'][0]['text'])
        # Even possession of a nonce does not authorize a different connection.
        reply = ordinary.call('write_file', 3, lease_nonce=nonce, path='bad.txt', text='no')
        self.assertTrue(reply['isError'])
        self.ping(ordinary)
        ordinary.close()
        self.assertEqual(broker.result('owned-job')['state'], 'leased')
        self.assertEqual(broker.status()['connection_generation'], 1)
        owner.request('write_file', lease_nonce=nonce, path='owner.txt', text='still owns lease')
        self.assertFalse((self.workspace / 'bad.txt').exists())

    def test_unactivated_chat_can_wait_after_dispatcher_starts(self):
        ordinary = self.connector()
        broker = self.broker()
        ordinary.send('tools/call', 2, {'name': 'wait_for_task', 'arguments': {}})
        self.wait_ready(broker)
        self.assertEqual(broker.status()['connection_generation'], 1)
        ordinary.send('notifications/cancelled', params={'requestId': 2})
        self.assertTrue(ordinary.receive(2)['result']['isError'])

    def test_unactivated_chat_survives_broker_restart_without_following_it(self):
        first = self.broker()
        ordinary = self.connector()
        first.close()
        second = self.broker()
        time.sleep(1.2)
        self.ping(ordinary)
        self.assertEqual(second.status()['connection_generation'], 0)
        ordinary.send('tools/call', 2, {'name': 'wait_for_task', 'arguments': {}})
        self.wait_ready(second)
        self.assertEqual(second.status()['connection_generation'], 1)
        ordinary.send('notifications/cancelled', params={'requestId': 2})
        self.assertTrue(ordinary.receive(2)['result']['isError'])


class PassiveDoubaoConnectorTests(PassiveWorkBuddyConnectorTests):
    provider = 'doubao'


if __name__ == '__main__':
    unittest.main()
