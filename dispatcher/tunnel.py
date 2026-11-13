"""Own one noninteractive SSH reverse tunnel and reconnect after process exits."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


WINDOWS_SSH = r'C:\Windows\System32\OpenSSH\ssh.exe'
LOG_LIMIT = 256 * 1024
MIN_RETRY_SECONDS = 5
MAX_RETRY_SECONDS = 30
POLL_SECONDS = .25
SECRET_LINE = re.compile(r'password|passphrase|token|secret|authorization|proxycommand|private[ _-]?key|://[^\s/]+:[^\s/@]+@', re.I)


class TunnelManager:
    def __init__(self, root, ssh_alias='autodo-relay', remote_port=18766, local_port=8766):
        if not isinstance(ssh_alias, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', ssh_alias):
            raise ValueError('ssh_alias must be one SSH host alias, not command arguments')
        for name, port in (('remote_port', remote_port), ('local_port', local_port)):
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError(f'{name} must be an integer between 1 and 65535')
        self.root = Path(root).resolve()
        self.ssh_alias, self.remote_port, self.local_port = ssh_alias, remote_port, local_port
        self._lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._process = None
        self._state, self._error, self._attempts = 'stopped', None, 0

    def command(self):
        executable = WINDOWS_SSH if os.name == 'nt' else shutil.which('ssh')
        if not executable:
            raise FileNotFoundError('OpenSSH client is not installed')
        return [executable, '-N', '-T', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
                '-R', f'127.0.0.1:{self.remote_port}:127.0.0.1:{self.local_port}', self.ssh_alias]

    def snapshot(self):
        with self._lock:
            return {'state': self._state, 'last_error': self._error, 'attempts': self._attempts,
                    'ssh_alias': self.ssh_alias, 'remote_port': self.remote_port,
                    'local_port': self.local_port}

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._state, self._error, self._attempts = 'starting', None, 0
            self._thread = threading.Thread(target=self._supervise, name='dispatcher-ssh-tunnel', daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=12)
            if thread.is_alive():
                raise RuntimeError('SSH tunnel supervisor did not stop within its deadline')

    def _log_paths(self):
        directory = (self.root/'data').resolve()
        if directory == self.root or not directory.is_relative_to(self.root):
            raise ValueError('Tunnel log directory must stay inside the dispatcher root')
        directory.mkdir(parents=True, exist_ok=True)
        paths = [directory/name for name in ('relay-tunnel.log', 'relay-tunnel.log.1')]
        if any(path.resolve().parent != directory for path in paths):
            raise ValueError('Tunnel log files must stay inside the dispatcher data directory')
        return paths

    @staticmethod
    def _safe_line(value):
        text = str(value).replace('\x00', '')
        if SECRET_LINE.search(text):
            return '[SSH diagnostic line redacted]'
        return ''.join(c for c in text if c in '\n\t' or ord(c) >= 32).strip()[:4096]

    def _append_log(self, message):
        safe = self._safe_line(message)
        if not safe:
            return
        stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
        encoded = (stamp + ' ' + safe + '\n').encode('utf-8')
        with self._log_lock:
            path, backup = self._log_paths()
            if path.exists() and path.stat().st_size + len(encoded) > LOG_LIMIT:
                path.replace(backup)
            with path.open('ab') as stream:
                stream.write(encoded[:LOG_LIMIT])

    def _read_stderr(self, process, errors):
        private_key_block = False
        try:
            while True:
                raw = process.stderr.readline(4096)
                if not raw:
                    break
                line = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else raw
                if re.search(r'BEGIN .*PRIVATE KEY', line):
                    private_key_block = True
                if private_key_block:
                    if re.search(r'END .*PRIVATE KEY', line):
                        private_key_block = False
                    continue
                safe = self._safe_line(line)
                if safe:
                    errors[:] = [safe]
                    self._append_log(safe)
        except (OSError, ValueError) as exc:
            errors[:] = [f'SSH log capture stopped: {type(exc).__name__}']

    @staticmethod
    def _terminate(process):
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        except ProcessLookupError:
            pass

    def _supervise(self):
        retry = MIN_RETRY_SECONDS
        try:
            while not self._stop.is_set():
                process, reader, errors = None, None, []
                started = time.monotonic()
                try:
                    with self._lock:
                        self._state = 'starting'
                        self._attempts += 1
                    process = subprocess.Popen(self.command(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE, shell=False,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0)
                    with self._lock:
                        self._process, self._state = process, 'running'
                    self._append_log(f'SSH process started; forwarding remote loopback {self.remote_port} to local loopback {self.local_port}')
                    reader = threading.Thread(target=self._read_stderr, args=(process, errors), daemon=True)
                    reader.start()
                    while not self._stop.is_set():
                        try:
                            code = process.wait(timeout=POLL_SECONDS)
                            break
                        except subprocess.TimeoutExpired:
                            continue
                    else:
                        code = None
                    if not self._stop.is_set():
                        if reader is not None:
                            reader.join(timeout=1)
                        error = errors[-1] if errors else f'SSH process exited with code {code}'
                        with self._lock:
                            self._error = error
                        self._append_log(error)
                except (OSError, ValueError) as exc:
                    error = self._safe_line(f'{type(exc).__name__}: {exc}')
                    with self._lock:
                        self._error = error
                    try:
                        self._append_log(error)
                    except (OSError, ValueError):
                        pass
                finally:
                    if process is not None:
                        self._terminate(process)
                    if reader is not None:
                        reader.join(timeout=2)
                    if process is not None and process.stderr is not None:
                        process.stderr.close()
                    with self._lock:
                        self._process = None
                if self._stop.is_set():
                    break
                if time.monotonic() - started >= 60:
                    retry = MIN_RETRY_SECONDS
                with self._lock:
                    self._state = 'reconnecting'
                self._stop.wait(retry)
                retry = min(retry * 2, MAX_RETRY_SECONDS)
        finally:
            with self._lock:
                self._state = 'stopped'
                self._process = None
