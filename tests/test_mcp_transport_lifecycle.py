"""Fail-stop lifecycle: only owned test helpers, no native app or model calls."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from dispatcher.mcp_namespace import mcp_namespace
from dispatcher.workbuddy_mcp import BridgeError, StdioWorker, WorkBuddyBroker


class FakeTransport:
    def __init__(self, provider='doubao', failure='connection_lost'):
        self.provider = provider; self.namespace = mcp_namespace(provider)
        self.failure = failure; self.calls = []; self.close_count = 0

    def request(self, operation, **params):
        self.calls.append(operation)
        raise BridgeError(self.failure)

    def close(self): self.close_count += 1


class MCPTransportUnitTests(unittest.TestCase):
    def worker(self, client):
        output = io.StringIO()
        return StdioWorker(client, stdin=io.StringIO(), stdout=output), output

    def test_lost_file_or_publish_ack_is_never_replayed_and_library_never_exits_host(self):
        for provider in ('workbuddy', 'doubao'):
            for operation, arguments in (
                ('write_file', {'lease_nonce': 'a' * 64, 'path': 'out.txt', 'text': 'fixed'}),
                ('publish_result', {'lease_nonce': 'a' * 64, 'status': 'complete', 'summary': 'fixed',
                    'artifacts': ['out.txt'], 'checks': [], 'needs_input': ''})):
                with self.subTest(provider=provider, operation=operation), patch('os._exit') as exit_host:
                    client = FakeTransport(provider); worker, output = self.worker(client)
                    params = {'name': operation, 'arguments': arguments}
                    worker._call(1, params, threading.Event())
                    self.assertTrue(worker._stop.is_set())
                    worker._call(2, params, threading.Event())
                    self.assertEqual(client.calls, [operation])
                    exit_host.assert_not_called()
                    response = json.loads(output.getvalue().splitlines()[0])
                    self.assertIn(client.namespace.display_name + ' bridge: connection_lost',
                                  response['result']['content'][0]['text'])

    def test_heartbeat_auth_or_epoch_failure_blocks_late_tool_dispatch(self):
        for reason in ('access_denied', 'connection_invalid', 'broker_stopped', 'connection_lost'):
            with self.subTest(reason=reason):
                client = FakeTransport(failure=reason); worker, output = self.worker(client)
                thread = threading.Thread(target=worker._heartbeat)
                thread.start(); thread.join(timeout=2)
                self.assertFalse(thread.is_alive()); self.assertTrue(worker._stop.is_set())
                worker._call(1, {'name': 'wait_for_task', 'arguments': {}}, threading.Event())
                self.assertEqual(client.calls, ['heartbeat'])

    def test_budget_and_tool_errors_do_not_trigger_transport_stop(self):
        for reason in ('wait_window_closed', 'segment_limit', 'not_waiting', 'request_cancelled', 'lease_invalid'):
            with self.subTest(reason=reason):
                client = FakeTransport(failure=reason); worker, output = self.worker(client)
                worker._call(1, {'name': 'wait_for_task', 'arguments': {}}, threading.Event())
                self.assertFalse(worker._stop.is_set())

    def test_normal_idle_continue_and_idle_timeout_do_not_exit_or_reconnect(self):
        for state in ('idle_continue', 'idle_timeout'):
            with self.subTest(state=state):
                client = FakeTransport(); worker, output = self.worker(client)
                def request(operation, **params):
                    client.calls.append(operation)
                    if operation == 'poll':
                        return {'task': None, 'segment_elapsed': True} if state == 'idle_continue' else {
                            'task': None, 'state': 'idle_timeout', 'reason': 'window_expired'}
                    if operation == 'wait_segment_end': return {'state': 'idle_continue'}
                    return {'state': 'waiting'}
                client.request = request
                worker._call(1, {'name': 'wait_for_task', 'arguments': {}}, threading.Event())
                self.assertFalse(worker._stop.is_set()); self.assertEqual(client.close_count, 0)
                value = json.loads(json.loads(output.getvalue())['result']['content'][0]['text'])
                self.assertEqual(value['state'], state)

    def test_eof_stop_wins_over_late_inflight_transport_error_without_arming_exit(self):
        client = FakeTransport(); worker, _ = self.worker(client)
        def request(operation, **params):
            worker._stop.set()  # Main thread observed EOF during an in-flight HTTP call.
            raise BridgeError('connection_lost')
        client.request = request
        with self.assertRaises(BridgeError): worker._request('heartbeat')
        self.assertFalse(worker._transport_failed.is_set())

    def test_line_returning_after_stop_is_not_parsed_or_dispatched(self):
        client = FakeTransport(); worker, output = self.worker(client)
        class LateInput:
            def readline(self, limit):
                worker._stop.set()
                return json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'}) + '\n'
        worker.stdin = LateInput(); worker.run()
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(client.calls, [])

    def test_watchdog_is_armed_without_output_state_or_close_completion(self):
        client = FakeTransport(); release_close = threading.Event(); exit_requested = threading.Event()
        client.close = lambda: release_close.wait(5)
        worker = StdioWorker(client, stdin=io.StringIO(), stdout=io.StringIO(),
                             transport_exit=lambda code: exit_requested.set())
        worker._state_lock.acquire(); worker._output_lock.acquire()
        try:
            with self.assertRaises(BridgeError): worker._request('heartbeat')
            self.assertTrue(worker._stop.is_set())
            self.assertTrue(exit_requested.wait(4), 'Watchdog waited for an unrelated lock or close')
        finally:
            worker._state_lock.release(); worker._output_lock.release(); release_close.set()
            self.assertTrue(worker._closed.wait(1))

    def test_completed_run_disarms_explicit_exit_callback(self):
        client = FakeTransport(); exit_requested = threading.Event()
        worker = StdioWorker(client, stdout=io.StringIO(), transport_exit=lambda code: exit_requested.set())
        class FailingInput:
            def readline(self, limit):
                try: worker._request('heartbeat')
                except BridgeError: pass
                return ''
        worker.stdin = FailingInput(); worker.run()
        self.assertTrue(worker._transport_failed.is_set())
        self.assertTrue(worker._transport_exit_cancel.is_set())
        self.assertFalse(exit_requested.is_set())

    def test_blocked_tool_response_cannot_disarm_watchdog_after_current_is_cleared(self):
        client = FakeTransport(); writing = threading.Event(); release = threading.Event(); exited = threading.Event()
        reading_next = threading.Event()
        def fail_after_main_reads(operation, **params):
            self.assertTrue(reading_next.wait(2))
            raise BridgeError('connection_lost')
        client.request = fail_after_main_reads
        class BlockingOutput(io.StringIO):
            def write(self, value):
                if json.loads(value).get('id') == 2:
                    writing.set(); release.wait(6)
                return super().write(value)
        lines = iter([
            json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'}) + '\n',
            json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n',
            json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
                'name': 'wait_for_task', 'arguments': {}}}) + '\n'])
        class FinalEof:
            def readline(self, limit):
                try: return next(lines)
                except StopIteration:
                    reading_next.set()
                    writing.wait(2)
                    return ''
        worker = StdioWorker(client, stdin=FinalEof(), stdout=BlockingOutput(),
                             transport_exit=lambda code: exited.set())
        thread = threading.Thread(target=worker.run); thread.start()
        try:
            self.assertTrue(writing.wait(2))
            self.assertIsNone(worker._current)
            self.assertTrue(exited.wait(4), 'Blocked response was mistaken for completed cleanup')
        finally:
            release.set(); thread.join(timeout=3)
            self.assertFalse(thread.is_alive())


class MCPRestartProcessTests(unittest.TestCase):
    def test_restart_exits_each_cli_while_stdin_remains_open_without_connecting_new_epoch(self):
        for provider in ('workbuddy', 'doubao'):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / 'root'; (root / 'workspaces').mkdir(parents=True)
                first = WorkBuddyBroker(root, provider=provider).start()
                second = None; process = None
                try:
                    launcher = Path(__file__).resolve().parents[1] / 'scripts' / (provider + '-mcp-worker.py')
                    process = subprocess.Popen([sys.executable, '-B', str(launcher), '--root', str(root)],
                        cwd=directory, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                    process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                        'params': {'protocolVersion': '2025-03-26'}}) + '\n'); process.stdin.flush()
                    response = json.loads(process.stdout.readline())
                    self.assertEqual(response['result']['serverInfo']['name'], mcp_namespace(provider).server_name)
                    # WorkBuddy discovery is passive. Activate a dedicated session
                    # before testing that an owned connection cannot cross epochs.
                    process.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n')
                    process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                        'params': {'name': 'wait_for_task', 'arguments': {},
                                   '_meta': {'progressToken': 'owned-wait'}}}) + '\n')
                    process.stdin.flush()
                    notice = json.loads(process.stdout.readline())
                    self.assertEqual(notice['method'], 'notifications/progress')
                    self.assertTrue(first.status()['ready'])
                    process.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                        'params': {'requestId': 2}}) + '\n'); process.stdin.flush()
                    while True:
                        response = json.loads(process.stdout.readline())
                        if response.get('id') == 2: break
                    self.assertTrue(response['result']['isError'])
                    self.assertTrue(first.status()['online'])
                    old_epoch = first.epoch
                    first.close(); second = WorkBuddyBroker(root, provider=provider).start()
                    self.assertNotEqual(old_epoch, second.epoch)
                    # No EOF and no additional input: a failed heartbeat must end
                    # the old helper rather than leave it asleep in readline.
                    try: process.wait(timeout=9)
                    except subprocess.TimeoutExpired: self.fail('Old MCP helper remained blocked in stdin after restart')
                    self.assertFalse(process.stdin.closed)
                    self.assertNotEqual(process.returncode, 0)
                    self.assertEqual(process.stdout.read(), '')
                    status = second.status()
                    self.assertEqual(status['connection_generation'], 0)
                    self.assertFalse(status['online']); self.assertEqual(status['queued_count'], 0)
                    self.assertIsNone(status['active_run_id'])
                finally:
                    if process:
                        if process.poll() is None: process.kill(); process.wait(timeout=5)
                        for stream in (process.stdin, process.stdout, process.stderr): stream.close()
                    if second: second.close()
                    first.close()


if __name__ == '__main__': unittest.main()
