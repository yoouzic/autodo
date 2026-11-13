"""Least-privilege startup supervisor for the local dispatcher.

The scheduler owns this long-lived process. The daemon still owns its existing
lock, queue and SSH supervisor. An explicit stop request always wins over retry.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .service import InstanceLock


MIN_RETRY_SECONDS = 5
MAX_RETRY_SECONDS = 60
STABLE_SECONDS = 60
DAEMON_LOG_LIMIT = 5 * 1024 * 1024
STARTUP_LOG_LIMIT = 1024 * 1024


def windows_identity():
    """Read the process token, rather than trusting user-name environment vars."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        length = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        if not length.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(length.value)
        if not advapi.GetTokenInformation(token, 1, buffer, len(buffer), ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        sid_text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            sid = sid_text.value
        finally:
            kernel.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        elevation = wintypes.DWORD()
        if not advapi.GetTokenInformation(token, 20, ctypes.byref(elevation), ctypes.sizeof(elevation), ctypes.byref(length)):
            raise ctypes.WinError(ctypes.get_last_error())
        return {'sid': sid, 'elevated': bool(elevation.value)}
    finally:
        kernel.CloseHandle(token)


def ensure_safe_runtime():
    if sys.platform == 'win32':
        identity = windows_identity()
        if identity['sid'] == 'S-1-5-18' or identity['elevated']:
            raise PermissionError('Dispatcher startup must run as the unelevated user, never SYSTEM or administrator')


class SupervisorLock(InstanceLock):
    def __init__(self, root):
        self.path = Path(root)/'data/startup.lock'

    def __enter__(self):
        try:
            return super().__enter__()
        except RuntimeError as exc:
            raise RuntimeError('A dispatcher startup supervisor is already running') from exc


class Supervisor:
    def __init__(self, root, poll_seconds=1.0, *, python_executable=None):
        self.root = Path(root).expanduser().resolve()
        self.data = (self.root/'data').resolve()
        if not self.root.is_dir() or self.data == self.root or not self.data.is_relative_to(self.root):
            raise ValueError('Startup requires an existing root with a contained data directory')
        if not 0 < poll_seconds <= 5:
            raise ValueError('poll_seconds must be positive and at most five seconds')
        self.poll_seconds = poll_seconds
        self.python_executable = python_executable or sys.executable
        self.process = None
        self.state = 'stopped'
        self._readers = []
        self._log_lock = threading.Lock()

    def _file(self, name):
        path = self.data/name
        if path.resolve().parent != self.data:
            raise ValueError('Startup files must stay inside the data directory')
        return path

    @property
    def stop_request(self):
        return self._file('stop.request')

    def daemon_running(self):
        try:
            with InstanceLock(self.root):
                return False
        except RuntimeError:
            return True

    def command(self):
        return [self.python_executable, '-m', 'dispatcher', '--root', str(self.root), 'daemon']

    def write_log(self, name, data, limit=DAEMON_LOG_LIMIT):
        self.data.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            data = data.encode('utf-8')
        data = data[-limit:]
        with self._log_lock:
            path, backup = self._file(name), self._file(name+'.1')
            if path.exists() and path.stat().st_size + len(data) > limit:
                path.replace(backup)
            with path.open('ab') as stream:
                stream.write(data)

    def log(self, message):
        stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
        self.write_log('startup.log', stamp+' '+message+'\n', STARTUP_LOG_LIMIT)

    def write_status(self, state, error=None, next_retry=None):
        self.state = state
        value = {'pid': os.getpid(), 'child_pid': self.process.pid if self.process is not None else None,
                 'state': state, 'next_retry': next_retry, 'error': error,
                 'updated_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
        self.data.mkdir(parents=True, exist_ok=True)
        temporary = self._file('startup-status.tmp')
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        temporary.replace(self._file('startup-status.json'))

    def _pump(self, stream, name):
        try:
            read = getattr(stream, 'read1', stream.read)
            while data := read(4096):
                self.write_log(name, data)
        except (OSError, ValueError):
            self.log('Dispatcher output capture ended unexpectedly')
        finally:
            stream.close()

    def _launch(self):
        env = dict(os.environ, PYTHONUTF8='1', PYTHONUNBUFFERED='1')
        self.process = subprocess.Popen(self.command(), cwd=self.root, env=env, shell=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0)
        self._readers = [threading.Thread(target=self._pump, args=(stream, name), daemon=True)
                         for stream, name in ((self.process.stdout, 'daemon.stdout.log'),
                                              (self.process.stderr, 'daemon.stderr.log'))]
        for reader in self._readers:
            reader.start()
        self.log('Started dispatcher child process')
        self.write_status('running')

    def _finish_child(self):
        for reader in self._readers:
            reader.join(timeout=2)
        self._readers = []
        self.process = None

    def _wait(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.stop_request.exists():
                return False
            time.sleep(min(self.poll_seconds, max(0, deadline-time.monotonic())))
        return not self.stop_request.exists()

    def _drain(self):
        """Signal graceful stop and wait; never kill work or someone else's PID."""
        self.write_status('stopping')
        while (self.process is not None and self.process.poll() is None) or self.daemon_running():
            # Reassert the user's stop if a newly starting daemon cleared it.
            self.stop_request.touch()
            time.sleep(self.poll_seconds)
        self._finish_child()

    def run(self):
        ensure_safe_runtime()
        self.data.mkdir(parents=True, exist_ok=True)
        # Validate fixed managed paths before a lock or log can follow a link.
        self._file('startup.lock')
        self._file('daemon.lock')
        with SupervisorLock(self.root):
            if not self.daemon_running():
                self.stop_request.unlink(missing_ok=True)
            retry = MIN_RETRY_SECONDS
            self.log('Startup supervisor started')
            try:
                while True:
                    if self.stop_request.exists():
                        self._drain()
                        return 0
                    if self.daemon_running():
                        if self.state != 'observing':
                            self.write_status('observing')
                            self.log('Observing an existing dispatcher; no duplicate started')
                        time.sleep(self.poll_seconds)
                        continue
                    started = time.monotonic()
                    try:
                        self._launch()
                        while self.process.poll() is None:
                            if self.stop_request.exists():
                                self._drain()
                                return 0
                            time.sleep(self.poll_seconds)
                        error = f'Dispatcher exited with code {self.process.returncode}'
                    except OSError as exc:
                        # Arbitrary error strings may contain values from the environment.
                        error = f'{type(exc).__name__} starting dispatcher'
                    finally:
                        self._finish_child()
                    if self.stop_request.exists():
                        return 0
                    if time.monotonic()-started >= STABLE_SECONDS:
                        retry = MIN_RETRY_SECONDS
                    self.log(error)
                    self.log(f'Retrying after {retry:g} seconds')
                    self.write_status('retrying', error=error, next_retry=time.time()+retry)
                    if not self._wait(retry):
                        return 0
                    retry = min(retry*2, MAX_RETRY_SECONDS)
            except KeyboardInterrupt:
                if self.process is not None:
                    self.stop_request.touch()
                    self._drain()
                return 0
            finally:
                self.write_status('stopped')
                self.log('Startup supervisor stopped')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run the dispatcher startup supervisor without elevated privileges')
    parser.add_argument('--root', required=True)
    args = parser.parse_args(argv)
    try:
        return Supervisor(args.root).run()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'Startup failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
