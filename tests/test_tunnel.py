import io
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from dispatcher.tunnel import TunnelManager, WINDOWS_SSH


class FakeProcess:
    def __init__(self, code=None, stderr=b''):
        self.returncode = code
        self.stderr = io.BytesIO(stderr)
        self.exited = threading.Event()
        self.terminated = 0
        self.killed = 0
        if code is not None:
            self.exited.set()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self.exited.wait(timeout):
            raise subprocess.TimeoutExpired('ssh', timeout)
        return self.returncode

    def terminate(self):
        self.terminated += 1
        self.returncode = -1
        self.exited.set()

    def kill(self):
        self.killed += 1
        self.returncode = -9
        self.exited.set()


def until(condition, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(.005)
    raise AssertionError('Condition did not become true before deadline')


class TunnelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = TunnelManager(self.root)

    def tearDown(self):
        self.manager.stop()
        self.temp.cleanup()

    def test_fixed_argv_and_no_shell_or_visible_window(self):
        process = FakeProcess()
        with patch('dispatcher.tunnel.subprocess.Popen', return_value=process) as start:
            self.manager.start()
            until(lambda: self.manager.snapshot()['state'] == 'running')
            argv = start.call_args.args[0]
            self.assertEqual(argv[1:], ['-N', '-T', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
                                       '-R', '127.0.0.1:18766:127.0.0.1:8766', 'autodo-relay'])
            if __import__('os').name == 'nt':
                self.assertEqual(argv[0], WINDOWS_SSH)
                self.assertEqual(start.call_args.kwargs['creationflags'], subprocess.CREATE_NO_WINDOW)
            self.assertFalse(start.call_args.kwargs['shell'])
            self.assertEqual(start.call_args.kwargs['stdin'], subprocess.DEVNULL)
            self.assertEqual(start.call_args.kwargs['stdout'], subprocess.DEVNULL)
            self.assertEqual(start.call_args.kwargs['stderr'], subprocess.PIPE)
            self.manager.stop()
        self.assertEqual(process.terminated, 1)
        self.assertEqual(self.manager.snapshot()['state'], 'stopped')

    def test_start_is_idempotent_and_restart_after_stop_is_explicit(self):
        first, second = FakeProcess(), FakeProcess()
        with patch('dispatcher.tunnel.subprocess.Popen', side_effect=[first, second]) as start:
            for _ in range(8):
                self.manager.start()
            until(lambda: self.manager.snapshot()['state'] == 'running')
            self.assertEqual(start.call_count, 1)
            self.manager.stop()
            self.manager.start()
            until(lambda: start.call_count == 2)
            self.manager.stop()
        self.assertEqual(first.terminated, 1)
        self.assertEqual(second.terminated, 1)

    def test_exit_reconnects_without_reusing_old_process(self):
        failed = FakeProcess(255, b'remote port forwarding failed\n')
        recovered = FakeProcess()
        with patch('dispatcher.tunnel.subprocess.Popen', side_effect=[failed, recovered]) as start, \
                patch('dispatcher.tunnel.MIN_RETRY_SECONDS', .01), patch('dispatcher.tunnel.MAX_RETRY_SECONDS', .02):
            self.manager.start()
            until(lambda: self.manager.snapshot()['attempts'] == 2 and self.manager.snapshot()['state'] == 'running')
            self.assertEqual(start.call_count, 2)
            self.assertIn('remote port forwarding failed', self.manager.snapshot()['last_error'])
            self.manager.stop()
        self.assertEqual(failed.terminated, 0)
        self.assertEqual(recovered.terminated, 1)

    def test_spawn_error_retries_and_stop_interrupts_backoff(self):
        with patch('dispatcher.tunnel.subprocess.Popen', side_effect=OSError('client unavailable')) as start:
            self.manager.start()
            until(lambda: self.manager.snapshot()['state'] == 'reconnecting')
            before = time.monotonic()
            self.manager.stop()
            self.assertLess(time.monotonic() - before, 1)
            self.assertEqual(start.call_count, 1)
            self.assertIn('client unavailable', self.manager.snapshot()['last_error'])

    def test_stop_escalates_only_the_owned_process_when_terminate_times_out(self):
        class SlowTerminate(FakeProcess):
            def terminate(self):
                self.terminated += 1

            def wait(self, timeout=None):
                if self.terminated and self.returncode is None:
                    raise subprocess.TimeoutExpired('ssh', timeout)
                return super().wait(timeout)

        process = SlowTerminate()
        with patch('dispatcher.tunnel.subprocess.Popen', return_value=process):
            self.manager.start()
            until(lambda: self.manager.snapshot()['state'] == 'running')
            self.manager.stop()
        self.assertEqual(process.terminated, 1)
        self.assertEqual(process.killed, 1)
        self.assertEqual(self.manager.snapshot()['state'], 'stopped')

    def test_logs_are_bounded_and_secrets_are_redacted(self):
        with patch('dispatcher.tunnel.LOG_LIMIT', 300):
            for _ in range(12):
                self.manager._append_log('expected diagnostic ' + 'x'*80)
            self.manager._append_log('password=never-log-this')
            self.manager._append_log('token=never-log-that')
        files = sorted((self.root/'data').glob('relay-tunnel.log*'))
        self.assertEqual(len(files), 2)
        self.assertTrue(all(path.stat().st_size <= 300 for path in files))
        text = ''.join(path.read_text(encoding='utf-8') for path in files)
        self.assertNotIn('never-log', text)
        self.assertIn('redacted', text)

    def test_private_key_blocks_are_not_written(self):
        process = FakeProcess(255, b'normal diagnostic\n-----BEGIN OPENSSH PRIVATE KEY-----\nsecret-base64-material\n-----END OPENSSH PRIVATE KEY-----\nPermission denied (publickey).\n')
        errors = []
        self.manager._read_stderr(process, errors)
        text = (self.root/'data/relay-tunnel.log').read_text(encoding='utf-8')
        self.assertNotIn('secret-base64-material', text)
        self.assertNotIn('PRIVATE KEY', text)
        self.assertIn('Permission denied', text)

    def test_rejects_argument_injection_and_invalid_ports(self):
        for alias in ('-R', 'host name', 'user@host', 'host;whoami', '', 'x'*65):
            with self.subTest(alias=alias), self.assertRaises(ValueError):
                TunnelManager(self.root, ssh_alias=alias)
        for port in (True, 0, -1, 65536, '18766'):
            with self.subTest(port=port), self.assertRaises(ValueError):
                TunnelManager(self.root, remote_port=port)
            with self.subTest(local_port=port), self.assertRaises(ValueError):
                TunnelManager(self.root, local_port=port)

    def test_nonwindows_uses_resolved_ssh_executable(self):
        with patch('dispatcher.tunnel.os.name', 'posix'), patch('dispatcher.tunnel.shutil.which', return_value='/usr/bin/ssh'):
            self.assertEqual(self.manager.command()[0], '/usr/bin/ssh')
        with patch('dispatcher.tunnel.os.name', 'posix'), patch('dispatcher.tunnel.shutil.which', return_value=None):
            with self.assertRaises(FileNotFoundError):
                self.manager.command()


if __name__ == '__main__':
    unittest.main()
