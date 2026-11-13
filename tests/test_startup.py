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

from dispatcher.service import InstanceLock


class FakeProcess:
    def __init__(self, returncode=None, pid=12345):
        self.returncode, self.pid = returncode, pid
        self.stdout, self.stderr = io.BytesIO(), io.BytesIO()
        self.terminated = self.killed = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1
        raise AssertionError('The supervisor must not terminate task execution')

    def kill(self):
        self.killed += 1
        raise AssertionError('The supervisor must not kill task execution')


def until(predicate, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError('Condition did not become true')


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('dispatcher.startup'), 'startup supervisor is not implemented')
        from dispatcher import startup
        self.module = startup
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root/'data').mkdir()
        self.supervisor = startup.Supervisor(self.root, poll_seconds=.01)
        self.thread = None
        self.errors = []
        self.safe = patch('dispatcher.startup.ensure_safe_runtime')
        self.safe.start()

    def tearDown(self):
        if not hasattr(self, 'temp'):
            return
        (self.root/'data/stop.request').touch()
        if self.thread:
            self.thread.join(timeout=2)
            self.assertFalse(self.thread.is_alive(), 'Test left a supervisor running')
        self.safe.stop()
        self.temp.cleanup()

    def launch(self):
        def run():
            try:
                self.supervisor.run()
            except Exception as exc:
                self.errors.append(exc)
        self.thread = threading.Thread(target=run)
        self.thread.start()

    def finish(self, process):
        (self.root/'data/stop.request').touch()
        process.returncode = 0
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.errors, [])

    def test_same_interpreter_hidden_child_and_only_one_supervisor(self):
        process = FakeProcess()
        with patch('dispatcher.startup.subprocess.Popen', return_value=process) as popen:
            self.launch()
            until(lambda: popen.call_count == 1)
            argv = popen.call_args.args[0]
            self.assertEqual(argv, [sys.executable, '-m', 'dispatcher', '--root', str(self.root.resolve()), 'daemon'])
            options = popen.call_args.kwargs
            self.assertEqual(options['cwd'], self.root.resolve())
            self.assertFalse(options['shell'])
            self.assertEqual(options['stdin'], subprocess.DEVNULL)
            self.assertEqual(options['env']['PYTHONUTF8'], '1')
            if os.name == 'nt':
                self.assertEqual(options['creationflags'], subprocess.CREATE_NO_WINDOW)
            with self.assertRaises(RuntimeError):
                self.module.Supervisor(self.root).run()
            self.finish(process)
        self.assertEqual(popen.call_count, 1)

    def test_existing_daemon_is_observed_not_replaced(self):
        with patch('dispatcher.startup.subprocess.Popen') as popen:
            with InstanceLock(self.root):
                self.launch()
                until(lambda: self.supervisor.state == 'observing')
                (self.root/'data/stop.request').touch()
                until(lambda: self.supervisor.state == 'stopping')
                self.assertTrue(self.thread.is_alive())
                popen.assert_not_called()
            self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.errors, [])

    def test_first_start_clears_only_stale_stop_request(self):
        request = self.root/'data/stop.request'
        request.touch()
        process = FakeProcess()
        with patch('dispatcher.startup.subprocess.Popen', return_value=process) as popen:
            self.launch()
            until(lambda: popen.call_count == 1)
            self.assertFalse(request.exists())
            self.finish(process)
        self.assertTrue(request.exists())

    def test_start_does_not_cancel_an_existing_daemons_stop(self):
        request = self.root/'data/stop.request'
        request.touch()
        with patch('dispatcher.startup.subprocess.Popen') as popen:
            with InstanceLock(self.root):
                self.launch()
                until(lambda: self.supervisor.state == 'stopping')
                self.assertTrue(request.exists())
                popen.assert_not_called()
            self.thread.join(timeout=3)
        self.assertEqual(self.errors, [])

    def test_manual_stop_waits_for_owned_work_without_force_kill(self):
        process = FakeProcess()
        with patch('dispatcher.startup.subprocess.Popen', return_value=process) as popen:
            self.launch()
            until(lambda: popen.call_count == 1)
            (self.root/'data/stop.request').touch()
            until(lambda: self.supervisor.state == 'stopping')
            self.assertTrue(self.thread.is_alive())
            self.assertEqual((process.terminated, process.killed), (0, 0))
            self.finish(process)
        self.assertEqual(popen.call_count, 1)

    def test_unexpected_exit_restarts_with_backoff(self):
        failed, recovered = FakeProcess(9, 111), FakeProcess(pid=222)
        with patch('dispatcher.startup.subprocess.Popen', side_effect=[failed, recovered]) as popen, \
                patch('dispatcher.startup.MIN_RETRY_SECONDS', .02), patch('dispatcher.startup.MAX_RETRY_SECONDS', .05):
            self.launch()
            until(lambda: popen.call_count == 2)
            self.finish(recovered)
        log = (self.root/'data/startup.log').read_text(encoding='utf-8')
        self.assertIn('code 9', log)
        self.assertIn('Retrying', log)

    def test_stop_during_backoff_prevents_relaunch(self):
        with patch('dispatcher.startup.subprocess.Popen', side_effect=OSError('unexpected secret=must-not-log')) as popen:
            self.launch()
            until(lambda: self.supervisor.state == 'retrying')
            (self.root/'data/stop.request').touch()
            self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(popen.call_count, 1)
        all_logs = ''.join(p.read_text(encoding='utf-8') for p in (self.root/'data').glob('startup*') if p.suffix != '.lock')
        self.assertNotIn('must-not-log', all_logs)

    def test_backoff_is_capped_and_a_stable_run_resets_it(self):
        delays = []
        def pause(delay):
            delays.append(delay)
            return len(delays) < 6
        with patch('dispatcher.startup.subprocess.Popen', side_effect=lambda *a, **k: FakeProcess(7)), \
                patch.object(self.supervisor, '_wait', side_effect=pause):
            self.supervisor.run()
        self.assertEqual(delays, [5, 10, 20, 40, 60, 60])
        delays.clear()
        launches = []
        def start(*args, **kwargs):
            launches.append(True)
            if len(launches) == 2:
                time.sleep(.03)
            return FakeProcess(7)
        def stable_pause(delay):
            delays.append(delay)
            return len(delays) < 2
        with patch('dispatcher.startup.subprocess.Popen', side_effect=start), \
                patch.object(self.supervisor, '_wait', side_effect=stable_pause), \
                patch('dispatcher.startup.STABLE_SECONDS', .02):
            self.supervisor.run()
        self.assertEqual(delays, [5, 5])

    def test_logs_are_bounded_and_snapshot_contains_only_metadata(self):
        for _ in range(8):
            self.supervisor.write_log('daemon.stdout.log', b'x'*90, limit=100)
        files = list((self.root/'data').glob('daemon.stdout.log*'))
        self.assertEqual(len(files), 2)
        self.assertTrue(all(p.stat().st_size <= 100 for p in files))
        self.supervisor.write_status('retrying', error='OSError starting dispatcher', next_retry=123)
        status = json.loads((self.root/'data/startup-status.json').read_text(encoding='utf-8'))
        self.assertEqual(status['state'], 'retrying')
        self.assertEqual(set(status), {'pid', 'child_pid', 'state', 'next_retry', 'error', 'updated_at'})

    def test_partial_output_is_saved_before_process_eof(self):
        reader_fd, writer_fd = os.pipe()
        stream = os.fdopen(reader_fd, 'rb')
        writer = os.fdopen(writer_fd, 'wb', buffering=0)
        reader = threading.Thread(target=self.supervisor._pump, args=(stream, 'daemon.stdout.log'))
        reader.start()
        try:
            writer.write(b'dispatcher ready\n')
            until(lambda: (self.root/'data/daemon.stdout.log').exists(), timeout=.3)
            self.assertEqual((self.root/'data/daemon.stdout.log').read_bytes(), b'dispatcher ready\n')
        finally:
            writer.close()
            reader.join(timeout=2)


class RuntimeSafetyTests(unittest.TestCase):
    def test_system_and_elevated_tokens_are_rejected(self):
        from dispatcher.startup import ensure_safe_runtime
        for identity in ({'sid': 'S-1-5-18', 'elevated': False}, {'sid': 'S-1-5-21-test', 'elevated': True}):
            with self.subTest(identity=identity), patch('dispatcher.startup.sys.platform', 'win32'), \
                    patch('dispatcher.startup.windows_identity', return_value=identity):
                with self.assertRaises(PermissionError):
                    ensure_safe_runtime()


if __name__ == '__main__':
    unittest.main()
