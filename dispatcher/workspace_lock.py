"""Process-owned OS leases for overlapping local workspace directories.

The caller holds WorkspaceLease across preparation, execution, checks and snapshots.
JSON records locate leases; only a live OS lock proves occupancy. No PID killing,
mtime expiry or database heartbeat can release another process's lease.
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath


MAX_RECORDS = 256
MAX_METADATA_BYTES = 16 * 1024
METADATA_LOCK_SECONDS = 2.0
_LEASE_LOCK_OFFSET = 64 * 1024  # Keep Windows byte locks outside the JSON payload.
_RECORD_NAME = re.compile(r'lease-([0-9a-f]{32})\.json\Z')
_TEMP_NAME = re.compile(r'\.lease-[0-9a-f]{32}\.tmp\Z')
_REGISTRY_ERROR = '工作区独占登记不可用，请检查本机登记状态后重试。'
_OCCUPIED = '该目录或其父子目录正在由另一任务使用。'


class WorkspaceBusy(ValueError):
    """Workspace is occupied, or exclusivity cannot safely be established."""


def _reject_reparse(path):
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise WorkspaceBusy('工作区独占不支持符号链接或重解析点。')


def _canonical(value):
    try:
        text = str(value)
        path = Path(text)
        windows = PureWindowsPath(text)
        if (not text or len(text) > 4096 or '\x00' in text or not path.is_absolute() or
                text.startswith(('\\\\', '//')) or windows.drive.startswith('\\\\') or
                ':' in text[len(windows.drive):]):
            raise WorkspaceBusy('工作区独占需要本机绝对目录路径。')
        _reject_reparse(path)
        resolved = path.resolve(strict=False)
        _reject_reparse(resolved)
        return resolved
    except WorkspaceBusy:
        raise
    except (OSError, ValueError, RuntimeError):
        raise WorkspaceBusy(_REGISTRY_ERROR) from None


def _workspace_path(value):
    path = _canonical(value)
    try:
        if path == Path(path.anchor) or path.exists() and not path.is_dir():
            raise WorkspaceBusy('工作区独占需要普通目录，不能使用磁盘根目录。')
    except OSError:
        raise WorkspaceBusy(_REGISTRY_ERROR) from None
    return path


def _key(path):
    return os.path.normpath(str(path)).replace('\\', '/').casefold().rstrip('/')


def _overlaps(first, second):
    first, second = _key(first), _key(second)
    return first == second or first.startswith(second + '/') or second.startswith(first + '/')


def _registry(root, *, create):
    root = _canonical(root)
    try:
        if not root.is_dir():
            raise WorkspaceBusy(_REGISTRY_ERROR)
        directory = root / 'data' / 'workspace-leases'
        for component in (directory.parent, directory):
            _reject_reparse(component)
            if create:
                component.mkdir(exist_ok=True)
            _reject_reparse(component)
            if component.exists() and not component.is_dir():
                raise WorkspaceBusy(_REGISTRY_ERROR)
        return directory
    except WorkspaceBusy:
        raise
    except OSError:
        raise WorkspaceBusy(_REGISTRY_ERROR) from None


def _check_file(path, *, maximum=MAX_METADATA_BYTES):
    _reject_reparse(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_size > maximum or
            getattr(info, 'st_nlink', 1) != 1):
        raise WorkspaceBusy(_REGISTRY_ERROR)


def _try_lock(handle, offset):
    handle.seek(offset)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            return False
        raise WorkspaceBusy(_REGISTRY_ERROR) from None


@contextmanager
def _metadata_guard(directory):
    path = directory / '.metadata.lock'
    handle = None
    try:
        _reject_reparse(path)
        if path.exists():
            _check_file(path, maximum=1)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        handle = os.fdopen(descriptor, 'r+b')
        _check_file(path, maximum=1)
        deadline = time.monotonic() + METADATA_LOCK_SECONDS
        while not _try_lock(handle, 0):
            if time.monotonic() >= deadline:
                raise WorkspaceBusy('工作区登记正在更新，请稍后重试。')
            time.sleep(0.01)
        yield
    except WorkspaceBusy:
        raise
    except OSError:
        raise WorkspaceBusy(_REGISTRY_ERROR) from None
    finally:
        if handle is not None:
            handle.close()  # Closing the owning handle releases the OS lock.


def _record_files(directory):
    records = []
    try:
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_RECORDS * 2 + 8:
                    raise WorkspaceBusy(_REGISTRY_ERROR)
                path = Path(entry.path)
                if entry.name == '.metadata.lock':
                    _check_file(path, maximum=1)
                elif _RECORD_NAME.fullmatch(entry.name):
                    records.append(path)
                    if len(records) > MAX_RECORDS:
                        raise WorkspaceBusy(_REGISTRY_ERROR)
                elif _TEMP_NAME.fullmatch(entry.name):
                    # Unpublished records cannot own a lease; leave crash remnants alone.
                    _check_file(path)
                else:
                    # Unknown files may contain private data. Never inspect their contents.
                    raise WorkspaceBusy(_REGISTRY_ERROR)
        return sorted(records)
    except FileNotFoundError:
        return []
    except WorkspaceBusy:
        raise
    except OSError:
        raise WorkspaceBusy(_REGISTRY_ERROR) from None


def _inspect_record(path):
    """Read a controlled record and non-blockingly probe its OS lock, without writes."""
    try:
        _check_file(path)
        # BufferedReader may issue a much larger ReadFile than requested and hit
        # the Windows lock byte past EOF. Unbuffered reads stay below that byte.
        with path.open('rb', buffering=0) as handle:
            payload = handle.read(MAX_METADATA_BYTES + 1)
            if len(payload) > MAX_METADATA_BYTES:
                raise WorkspaceBusy(_REGISTRY_ERROR)
            record = json.loads(payload)
            owner = _RECORD_NAME.fullmatch(path.name).group(1)
            if (not isinstance(record, dict) or set(record) != {'version', 'owner', 'path', 'run_id', 'pid'} or
                    type(record['version']) is not int or record['version'] != 1 or
                    record['owner'] != owner or type(record['pid']) is not int or record['pid'] <= 0 or
                    not isinstance(record['path'], str) or
                    not isinstance(record['run_id'], str) or not 0 < len(record['run_id']) <= 256 or
                    '\x00' in record['run_id']):
                raise WorkspaceBusy(_REGISTRY_ERROR)
            record['path'] = str(_workspace_path(record['path']))
            active = not _try_lock(handle, _LEASE_LOCK_OFFSET)
            return record, active
    except FileNotFoundError:
        return None  # A read-only display can race with normal owner cleanup.
    except WorkspaceBusy:
        raise
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise WorkspaceBusy(_REGISTRY_ERROR) from None


def _publish_record(directory, record):
    destination = directory / ('lease-' + record['owner'] + '.json')
    temporary = directory / ('.lease-' + record['owner'] + '.tmp')
    payload = json.dumps(record, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(payload) > MAX_METADATA_BYTES:
        raise WorkspaceBusy(_REGISTRY_ERROR)
    owned_temporary = False
    try:
        with temporary.open('xb') as handle:
            owned_temporary = True
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _reject_reparse(directory)
        if destination.exists():
            raise WorkspaceBusy(_REGISTRY_ERROR)
        os.replace(temporary, destination)
        return destination
    except WorkspaceBusy:
        raise
    except OSError:
        raise WorkspaceBusy(_REGISTRY_ERROR) from None
    finally:
        if owned_temporary:
            temporary.unlink(missing_ok=True)


def busy_paths(root):
    """Return a read-only occupancy snapshot; never register, clean or evict an owner.

Non-blocking lock probes use read-only file handles and immediately release any
unoccupied lock. A UI snapshot is advisory; acquisition always rechecks atomically.
"""
    directory = _registry(root, create=False)
    result = set()
    for path in _record_files(directory):
        inspected = _inspect_record(path)
        if inspected is not None and inspected[1]:
            result.add(inspected[0]['path'])
    return sorted(result, key=_key)


class WorkspaceLease:
    def __init__(self, root, workspace_path, run_id):
        if not isinstance(run_id, str) or not 0 < len(run_id) <= 256 or '\x00' in run_id:
            raise WorkspaceBusy('工作区独占需要有效的运行标识。')
        self.root = _canonical(root)
        self.workspace_path = _workspace_path(workspace_path)
        self.run_id = run_id
        self.owner = uuid.uuid4().hex
        self.lease_path = None
        self._handle = None
        self.cleanup_error = None

    def __enter__(self):
        if self._handle is not None:
            raise WorkspaceBusy('工作区独占不能重复进入。')
        self.workspace_path = _workspace_path(self.workspace_path)
        directory = _registry(self.root, create=True)
        with _metadata_guard(directory):
            active_count = 0
            for path in _record_files(directory):
                inspected = _inspect_record(path)
                if inspected is None:
                    continue
                record, active = inspected
                if active:
                    active_count += 1
                    if _overlaps(self.workspace_path, record['path']):
                        raise WorkspaceBusy(_OCCUPIED)
                else:
                    # Valid module-owned name/schema and an actually free OS lock are required.
                    try:
                        path.unlink()
                    except OSError:
                        raise WorkspaceBusy(_REGISTRY_ERROR) from None
            if active_count >= MAX_RECORDS:
                raise WorkspaceBusy(_REGISTRY_ERROR)
            record = {'version': 1, 'owner': self.owner, 'path': str(self.workspace_path),
                      'run_id': self.run_id, 'pid': os.getpid()}
            self.lease_path = _publish_record(directory, record)
            handle = None
            try:
                _check_file(self.lease_path)
                handle = self.lease_path.open('r+b')
                if not _try_lock(handle, _LEASE_LOCK_OFFSET):
                    raise WorkspaceBusy(_REGISTRY_ERROR)
                self._handle = handle
            except (OSError, WorkspaceBusy):
                if handle is not None:
                    handle.close()
                # Leave uncertain registrations intact; a later owner must prove them free.
                raise WorkspaceBusy(_REGISTRY_ERROR) from None
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self._handle is None:
            return False
        try:
            directory = _registry(self.root, create=False)
            with _metadata_guard(directory):
                self._handle.close()
                self._handle = None
                inspected = _inspect_record(self.lease_path)
                if inspected is not None:
                    record, active = inspected
                    if (active or record['owner'] != self.owner or record['run_id'] != self.run_id or
                            _key(record['path']) != _key(self.workspace_path)):
                        raise WorkspaceBusy(_REGISTRY_ERROR)
                    self.lease_path.unlink()
        except (OSError, WorkspaceBusy):
            self.cleanup_error = _REGISTRY_ERROR
            if exc_type is None:
                raise WorkspaceBusy(_REGISTRY_ERROR) from None
        finally:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
        return False
