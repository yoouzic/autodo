"""Bounded Doubao wait windows with a controlled clock; no desktop/model calls."""
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

from dispatcher.workbuddy_mcp import BridgeError, WorkBuddyBroker


class DoubaoSegmentedWaitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspaces/task/workspace'
        self.workspace.mkdir(parents=True)
        self.now = 100.0
        self.broker = WorkBuddyBroker(self.root, provider='doubao', clock=lambda: self.now, heartbeat_timeout=120)
        self.addCleanup(self.broker.close)
        self.connection = self.broker.request('connect')['connection_id']

    def request(self, op, **params):
        return self.broker.request(op, self.connection, **params)

    def start_wait(self):
        self.request('wait_started', progress_token_seen=True)
        self.assertIsNone(self.request('poll')['task'])
        return self.broker.status()['wait_window_id']

    def advance(self, seconds):
        # Keep the connector alive while advancing only the host's controlled clock.
        while seconds > 0:
            amount = min(seconds, 100); self.now += amount; seconds -= amount
            self.request('heartbeat')

    def test_normal_40_second_segment_then_gap_then_same_window(self):
        window = self.start_wait(); self.advance(39)
        with self.assertRaisesRegex(BridgeError, 'segment_not_elapsed'):
            self.request('wait_segment_end')
        self.advance(1)
        self.assertFalse(self.broker.status()['ready'])
        self.assertTrue(self.request('poll')['segment_elapsed'])
        response = self.request('wait_segment_end')
        self.assertEqual(response['state'], 'idle_continue')
        status = self.broker.status()
        self.assertEqual(status['wait_phase'], 'gap')
        self.assertFalse(status['ready']); self.assertFalse(status['wait_active'])
        self.assertEqual(status['wait_active_seconds'], 40)
        self.advance(10); self.start_wait(); self.advance(30)
        status = self.broker.status()
        self.assertEqual(status['wait_window_id'], window)
        self.assertEqual(status['wait_active_seconds'], 70)
        self.assertEqual(status['wait_segment_count'], 2)
        self.assertEqual(status['wait_gap_seconds'], 0)

    def test_gap_expiry_and_cancel_cannot_rearm_same_connection(self):
        self.start_wait(); self.advance(40); self.request('wait_segment_end'); self.advance(15)
        self.assertEqual(self.broker.status()['wait_end_reason'], 'gap_expired')
        with self.assertRaisesRegex(BridgeError, 'wait_window_closed'): self.start_wait()
        with self.assertRaises(BridgeError): self.broker.submit('late', self.workspace, 'no')

    def test_900_second_wall_budget_includes_gaps(self):
        self.start_wait()
        for _ in range(16):
            self.advance(40); self.request('wait_segment_end'); self.advance(14); self.start_wait()
        self.advance(35); self.assertTrue(self.broker.status()['ready'])
        self.advance(1)
        status = self.broker.status()
        self.assertEqual(status['wait_phase'], 'closed')
        self.assertEqual(status['wait_end_reason'], 'window_expired')
        self.assertEqual(status['wait_active_seconds'], 16 * 40 + 36)
        self.assertEqual(self.request('poll')['state'], 'idle_timeout')
        with self.assertRaisesRegex(BridgeError, 'wait_window_closed'): self.start_wait()

    def test_segment_count_has_independent_hard_cap(self):
        self.start_wait()
        self.broker.submit('last-segment-queued', self.workspace, 'fixed')
        # 900 s is normally the earlier bound. Exercise the defensive count guard.
        self.broker._wait_window['segment_count'] = 24
        self.advance(40)
        self.assertEqual(self.request('wait_segment_end')['state'], 'idle_timeout')
        self.assertEqual(self.broker.status()['wait_end_reason'], 'segment_limit')
        self.assertEqual(self.broker.result('last-segment-queued')['state'], 'expired')
        with self.assertRaises(BridgeError): self.start_wait()

    def test_cancel_ended_and_disconnect_close_window_and_queued_jobs(self):
        for op in ('cancel_lease', 'wait_ended', 'disconnect'):
            with self.subTest(op=op):
                if op != 'cancel_lease':
                    self.connection = self.broker.request('connect')['connection_id']
                self.start_wait(); self.broker.submit(op, self.workspace, 'fixed')
                self.request(op)
                self.assertIn(self.broker.result(op)['state'], ('cancelled', 'disconnected'))
                self.assertEqual(self.broker.status()['wait_phase'], 'closed')
                if op != 'disconnect':
                    with self.assertRaises(BridgeError): self.start_wait()
                    self.request('disconnect')

    def test_queued_job_can_continue_same_window_but_not_escape_closed_window(self):
        self.start_wait(); self.broker.submit('queued', self.workspace, 'fixed')
        self.advance(40); self.request('wait_segment_end')
        self.assertEqual(self.broker.result('queued')['state'], 'queued')
        with self.assertRaises(BridgeError): self.broker.submit('during-gap', self.workspace, 'fixed')
        self.advance(15)
        self.assertEqual(self.broker.result('queued')['state'], 'expired')
        with self.assertRaises(BridgeError): self.start_wait()
        self.request('disconnect'); self.connection = self.broker.request('connect')['connection_id']
        self.start_wait(); self.assertIsNone(self.request('poll')['task'])

    def test_queued_job_survives_only_a_normal_continuation_and_is_claimed_once(self):
        old = self.start_wait(); self.broker.submit('queued', self.workspace, 'fixed')
        self.advance(40); self.request('wait_segment_end'); self.advance(5)
        self.request('wait_started', progress_token_seen=False)
        task = self.request('poll')['task']
        self.assertEqual(task['run_id'], 'queued')
        self.assertEqual(self.broker.status()['wait_window_id'], old)
        self.assertEqual(self.broker.status()['wait_end_reason'], 'task_leased')
        with self.assertRaises(BridgeError): self.request('poll')

    def test_window_budget_cancels_queued_task_before_it_can_be_claimed(self):
        self.start_wait()
        for _ in range(16):
            self.advance(40); self.request('wait_segment_end'); self.advance(14); self.start_wait()
        self.advance(35); self.broker.submit('expires-with-window', self.workspace, 'fixed')
        self.advance(1)
        self.assertEqual(self.broker.result('expires-with-window')['state'], 'expired')
        self.assertEqual(self.request('poll')['state'], 'idle_timeout')

    def test_heartbeat_expiry_revokes_window_and_queued_task(self):
        self.start_wait(); self.broker.submit('lost-connector', self.workspace, 'fixed')
        self.now += 121
        self.assertFalse(self.broker.status()['online'])
        self.assertEqual(self.broker.status()['wait_end_reason'], 'disconnected')
        self.assertEqual(self.broker.result('lost-connector')['state'], 'disconnected')

    def test_leasing_one_job_cancels_other_queued_jobs_from_that_window(self):
        self.start_wait(); self.broker.submit('one', self.workspace, 'fixed')
        other = self.root / 'workspaces/other/workspace'; other.mkdir(parents=True)
        self.broker.submit('two', other, 'fixed', priority=9)
        self.assertEqual(self.request('poll')['task']['run_id'], 'two')
        self.assertEqual(self.broker.result('one')['state'], 'cancelled')

    def test_only_published_lease_allows_fresh_window(self):
        old = self.start_wait(); self.broker.submit('task', self.workspace, 'fixed')
        task = self.request('poll')['task']; self.request('wait_ended')
        self.request('write_file', lease_nonce=task['lease_nonce'], path='out.txt', text='done')
        self.request('publish_result', lease_nonce=task['lease_nonce'], status='complete', summary='done',
                     artifacts=['out.txt'], checks=[], needs_input='')
        new = self.start_wait(); self.assertNotEqual(old, new)
        self.assertEqual(self.broker.status()['wait_active_seconds'], 0)
        self.broker.submit('cancelled-task', self.workspace, 'fixed')
        self.request('poll'); self.broker.cancel('cancelled-task')
        self.request('wait_ended')
        with self.assertRaises(BridgeError): self.start_wait()

    def test_probe_binding_requires_actual_window_and_claim_tracks_it(self):
        window = self.start_wait(); run_id = 'probe-' + 'a' * 32
        self.broker.submit(run_id, self.workspace, 'fixed')
        status = self.broker.status()
        with self.assertRaisesRegex(BridgeError, 'probe_wait_changed'):
            self.broker._bind_probe_wait(run_id, status['wait_calls'], status['connection_generation'], window_id='b' * 32)
        self.broker._bind_probe_wait(run_id, status['wait_calls'], status['connection_generation'], window_id=window)
        self.request('poll')
        evidence = self.broker.result(run_id)['claim_evidence']
        self.assertTrue(evidence['matched']); self.assertEqual(evidence['actual']['window_id'], window)

    def test_workbuddy_keeps_one_call_wait_and_existing_submit_contract(self):
        broker = WorkBuddyBroker(self.root, clock=lambda: self.now, heartbeat_timeout=120)
        self.addCleanup(broker.close)
        connection = broker.request('connect')['connection_id']
        broker.submit('before-wait', self.workspace, 'fixed')
        self.assertEqual(broker.request('poll', connection)['task']['run_id'], 'before-wait')
        broker.cancel('before-wait')
        broker.request('wait_started', connection, progress_token_seen=False)
        broker.request('poll', connection); self.now += 70
        self.assertTrue(broker.status()['ready'])
        self.assertEqual(broker.status()['wait_policy'], 'single_call')


class DoubaoSegmentedStdioTests(unittest.TestCase):
    """The real stdio process sees the controlled-clock broker over loopback."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspaces/task/workspace'; self.workspace.mkdir(parents=True)
        self.now = 100.0
        self.broker = WorkBuddyBroker(self.root, provider='doubao', clock=lambda: self.now).start()
        self.addCleanup(self.broker.close)
        launcher = Path(__file__).resolve().parents[1] / 'scripts/doubao-mcp-worker.py'
        self.process = subprocess.Popen([sys.executable, '-B', str(launcher), '--root', str(self.root),
                '--progress-seconds', '0.05'], cwd=self.temp.name, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.messages = queue.Queue()
        self.reader = threading.Thread(target=self.read_messages, daemon=True); self.reader.start()
        self.addCleanup(self.close_process)
        self.send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}})
        response = self.receive(1)
        self.assertEqual(response['result']['serverInfo']['version'], '0.1.4')
        self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

    def read_messages(self):
        try:
            for line in self.process.stdout: self.messages.put(json.loads(line))
        finally: self.messages.put(None)

    def close_process(self):
        if not self.process.stdin.closed: self.process.stdin.close()
        try: self.process.wait(timeout=5)
        except subprocess.TimeoutExpired: self.process.kill(); self.process.wait(timeout=3)
        self.reader.join(timeout=2)
        self.process.stdout.close(); self.process.stderr.close()

    def send(self, value):
        self.process.stdin.write(json.dumps(value) + '\n'); self.process.stdin.flush()

    def receive(self, request_id):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            value = self.messages.get(timeout=max(.01, deadline - time.monotonic()))
            self.assertIsNotNone(value)
            if value.get('id') == request_id: return value
        self.fail('No response from owned stdio worker')

    def wait(self, request_id):
        self.send({'jsonrpc': '2.0', 'id': request_id, 'method': 'tools/call', 'params': {
            'name': 'wait_for_task', 'arguments': {}, '_meta': {'progressToken': request_id}}})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.broker.status()['ready']: return
            time.sleep(.01)
        self.fail('No active wait')

    def advance(self, seconds):
        with self.broker._lock:
            self.now += seconds
            # Controlled clock jump must not simulate an unrelated heartbeat loss.
            self.broker._worker['seen'] = self.now

    def test_stdio_returns_normal_continuation_then_accepts_task_on_next_segment(self):
        self.send({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        description = self.receive(2)['result']['tools'][0]['description']
        for value in ('40 seconds', '900-second', '24 segments', '15 seconds', 'idle_continue'):
            self.assertIn(value, description)
        self.wait(3); self.advance(40)
        response = self.receive(3)['result']
        self.assertFalse(response['isError'])
        self.assertEqual(json.loads(response['content'][0]['text'])['state'], 'idle_continue')
        self.assertEqual(self.broker.status()['wait_phase'], 'gap')
        self.wait(4); self.advance(30)
        self.assertEqual(self.broker.status()['wait_active_seconds'], 70)
        self.broker.submit('isolated', self.workspace, 'Only isolated fixture data')
        task = json.loads(self.receive(4)['result']['content'][0]['text'])['task']
        self.assertEqual(task['run_id'], 'isolated')
        self.process.stdin.close(); self.process.wait(timeout=5)
        self.assertEqual(self.broker.result('isolated')['state'], 'disconnected')
        self.assertEqual(self.broker.status()['wait_phase'], 'closed')

    def test_standard_cancel_closes_window_and_next_call_cannot_reset_budget(self):
        self.wait(2)
        self.send({'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': 2}})
        self.assertTrue(self.receive(2)['result']['isError'])
        self.assertEqual(self.broker.status()['wait_phase'], 'closed')
        self.send({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'wait_for_task', 'arguments': {}}})
        response = self.receive(3)['result']
        self.assertTrue(response['isError']); self.assertIn('wait_window_closed', response['content'][0]['text'])

    def test_eof_ends_active_window_without_creating_gap_or_rearm(self):
        self.wait(2); self.process.stdin.close(); self.process.wait(timeout=5)
        status = self.broker.status()
        self.assertFalse(status['online']); self.assertFalse(status['ready'])
        self.assertEqual(status['wait_phase'], 'closed')
        # EOF stops the call and disconnects concurrently; either may revoke first.
        self.assertIn(status['wait_end_reason'], ('disconnected', 'cancelled'))


if __name__ == '__main__': unittest.main()
