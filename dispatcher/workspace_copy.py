"""Bounded byte copies of authorized workspaces, without running source code.

The caller owns authorization and must not execute a destination unless this
function returns successfully. Failures deliberately leave partial copies for
diagnosis; source files are only opened for reading.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PureWindowsPath


MAX_FILES = 10_000
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_DEPTH = 40
MAX_ENTRIES = 20_000
CHUNK_BYTES = 1024 * 1024
EXCLUDED_DIRECTORIES = frozenset({
    '.git', '.hg', '.svn', '.runtime', '.codex', '.claude', '.gemini', '.dsh',
    '.zcode', '.qoder', '.codebuddy', '.workbuddy', '.ssh', '.aws', '.azure', '.kube', '.gnupg', '.auth',
    'credentials', 'secrets', 'keys', 'node_modules', 'venv', '.venv', 'env',
    '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', '.cache',
    'cache', 'caches', '.next', '.nuxt',
    '.turbo', 'coverage', '.tox', '.nox', '.gradle', '.parcel-cache',
})
EXCLUDED_FILES = frozenset({
    '.npmrc', '.yarnrc', '.yarnrc.yml', '.pypirc', '.netrc', '_netrc',
    '.git-credentials', '.gitconfig', '.workbuddy-key-fallback', 'id_rsa', 'id_dsa', 'id_ecdsa',
    'id_ed25519', '.coverage', '.ds_store', 'thumbs.db',
})
CONFIG_SUFFIXES = frozenset({
    '', '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf', '.xml',
    '.properties', '.db', '.sqlite', '.sqlite3', '.txt',
})
SECRET_NAME = re.compile(
    r'(?:^|[_.-])(?:auth|credentials?|secrets?|tokens?|api[_-]?keys?|'
    r'providers?|oauth|service[_-]?account)(?:[_.-]|$)', re.I)


def _path_key(path):
    return os.path.normcase(os.path.normpath(str(path))).replace('\\', '/').rstrip('/')


def _within(path, root):
    value, base = _path_key(path), _path_key(root)
    return value == base or value.startswith(base + '/')


def _identity(info):
    # Windows lstat/fstat can expose different creation/change-time meanings for
    # ctime. Device/inode identify the file; size/mtime and content hashes detect
    # changes without treating that API discrepancy as a replacement.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)


def _no_link(info):
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError('Workspace copies cannot contain links or reparse points')


def _check_components(path):
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        _no_link(info)


def _safe_path(value):
    path = Path(value)
    text = str(path)
    windows = PureWindowsPath(text)
    if (not path.is_absolute() or text.startswith(('\\\\', '//')) or
            windows.drive.startswith('\\\\') or ':' in text[len(windows.drive):]):
        raise ValueError('Workspace copy paths must be local absolute paths')
    path = Path(os.path.abspath(path))
    _check_components(path)
    path = path.resolve(strict=False)
    home = Path.home()
    if path == Path(path.anchor) or _within(home, path):
        raise ValueError('Disk roots and user profile roots cannot be copied')
    private = [home / name for name in
               ('.ssh', '.aws', '.azure', '.kube', '.gnupg', '.codex', '.claude',
                '.dsh', '.gemini', '.qoder', '.zcode', '.codebuddy', '.workbuddy', '.workbuddy-key-fallback', '.config/gcloud')]
    roaming = Path(os.environ.get('APPDATA', str(home / 'AppData/Roaming')))
    private += [roaming / name for name in ('Codex', 'ZCode', 'Qoder', 'Antigravity', 'com.qoder.app.stable')]
    private.append(Path(os.environ.get('LOCALAPPDATA',str(home/'AppData/Local')))/'CodeBuddyExtension')
    if any(_within(path, item) or _within(item, path) for item in private):
        raise ValueError('Agent and authentication roots cannot be copied')
    if any(_excluded(component.name, True) for component in (path, *path.parents) if component.name):
        raise ValueError('Excluded runtime or private directories cannot be workspace roots')
    _check_components(path)
    return path


def _directory(path):
    _check_components(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError('Workspace copy requires ordinary directories')
    return info


def _excluded(name, is_directory):
    name = name.casefold()
    if name.startswith(('.env', '.dispatcher-doubao-')) or name in EXCLUDED_DIRECTORIES:
        return True
    if is_directory:
        return False
    suffix = Path(name).suffix
    return (name in EXCLUDED_FILES or suffix in {'.pem', '.key', '.p12', '.pfx', '.jks', '.keystore', '.pyc', '.pyo'} or
            (suffix in CONFIG_SUFFIXES and bool(SECRET_NAME.search(name))))


def _read_file(path, expected, output=None):
    """Check identity before reading and after EOF, then verify the pathname."""
    _check_components(path.parent)
    current = path.lstat()
    _no_link(current)
    if not stat.S_ISREG(current.st_mode) or _identity(current) != expected:
        raise ValueError('Workspace file changed before opening')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, 'rb') as source:
        opened = os.fstat(source.fileno())
        _no_link(opened)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != expected:
            raise ValueError('Workspace file identity changed while opening')
        digest, count = hashlib.sha256(), 0
        while block := source.read(CHUNK_BYTES):
            count += len(block)
            if count > MAX_FILE_BYTES or count > expected[3]:
                raise ValueError('Workspace file grew or exceeds the per-file limit')
            digest.update(block)
            if output is not None:
                output.write(block)
        if count != expected[3] or _identity(os.fstat(source.fileno())) != expected:
            raise ValueError('Workspace file changed while reading')
    _check_components(path.parent)
    final = path.lstat()
    _no_link(final)
    if _identity(final) != expected:
        raise ValueError('Workspace file changed after reading')
    return digest.hexdigest()


def _scan(root):
    files, directories, excluded = {}, {}, {}
    total_bytes, entries = 0, 0

    def visit(directory, depth):
        nonlocal total_bytes, entries
        before = _directory(directory)
        relative_directory = directory.relative_to(root).as_posix()
        directories[relative_directory] = _identity(before)
        with os.scandir(directory) as iterator:
            for entry in iterator:
                entries += 1
                if entries > MAX_ENTRIES:
                    raise ValueError('Workspace contains too many directory entries')
                if depth + 1 > MAX_DEPTH:
                    raise ValueError('Workspace exceeds the directory depth limit')
                path = directory / entry.name
                info = path.lstat()
                _no_link(info)
                is_directory = stat.S_ISDIR(info.st_mode)
                if not is_directory and not stat.S_ISREG(info.st_mode):
                    raise ValueError('Workspace contains a non-regular file')
                relative = path.relative_to(root).as_posix()
                if _excluded(entry.name, is_directory):
                    excluded[relative] = _identity(info)
                    continue
                if is_directory:
                    visit(path, depth + 1)
                    continue
                if len(files) >= MAX_FILES or info.st_size > MAX_FILE_BYTES:
                    raise ValueError('Workspace exceeds the file count or per-file size limit')
                total_bytes += info.st_size
                if total_bytes > MAX_TOTAL_BYTES:
                    raise ValueError('Workspace exceeds the total byte limit')
                identity = _identity(info)
                files[relative] = {'identity': identity, 'sha256': _read_file(path, identity),
                                   'bytes': info.st_size}
        if _identity(_directory(directory)) != _identity(before):
            raise ValueError('Workspace directory changed while scanning')

    visit(root, 0)
    return {'files': files, 'directories': directories, 'excluded': excluded,
            'total_bytes': total_bytes}


def _manifest(snapshot):
    return [{'path': name, 'sha256': value['sha256'], 'bytes': value['bytes']}
            for name, value in sorted(snapshot['files'].items())]


def _copy_file(source, destination, expected):
    _directory(destination.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(destination, flags, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        opened = os.fstat(output.fileno())
        _no_link(opened)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ValueError('Destination is not a new ordinary file')
        _check_components(destination.parent)
        if _identity(destination.lstat()) != _identity(opened):
            raise ValueError('Destination file identity changed while opening')
        digest = _read_file(source, expected['identity'], output)
        output.flush()
        final = os.fstat(output.fileno())
        if digest != expected['sha256'] or final.st_size != expected['bytes']:
            raise ValueError('Workspace content changed during copying')
    _check_components(destination.parent)
    current = destination.lstat()
    _no_link(current)
    if _identity(current) != _identity(final):
        raise ValueError('Destination file changed during copying')


def copy_workspace(source: Path, destination: Path):
    """Return a verified manifest only after the complete filtered copy succeeds."""
    source, destination = _safe_path(source), _safe_path(destination)
    if _within(source, destination) or _within(destination, source):
        raise ValueError('Source and destination must not overlap')
    _directory(source)
    _directory(destination.parent)
    destination_identity = None
    if destination.exists():
        destination_identity = _identity(_directory(destination))
        with os.scandir(destination) as entries:
            if next(entries, None) is not None:
                raise ValueError('Destination must not exist or must be empty')
    before = _scan(source)
    if destination_identity is None:
        destination.mkdir()
    else:
        if _identity(_directory(destination)) != destination_identity:
            raise ValueError('Destination directory changed while preparing the copy')
        with os.scandir(destination) as entries:
            if next(entries, None) is not None:
                raise ValueError('Destination is no longer empty')
    _directory(destination)
    for relative in sorted(before['directories'], key=lambda value: (len(Path(value).parts), value)):
        if relative != '.':
            path = destination / relative
            _directory(path.parent)
            path.mkdir()
            _directory(path)
    for relative, expected in sorted(before['files'].items()):
        _copy_file(source / relative, destination / relative, expected)
    copied = _scan(destination)
    manifest = _manifest(before)
    if (copied['excluded'] or _manifest(copied) != manifest or
            set(copied['directories']) != set(before['directories'])):
        raise ValueError('Destination does not match the verified source snapshot')
    after = _scan(source)
    if after != before:
        raise ValueError('Source workspace changed while copying; partial copy retained')
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return {'file_count': len(manifest), 'total_bytes': before['total_bytes'],
            'manifest_sha256': hashlib.sha256(encoded).hexdigest(),
            'excluded_count': len(before['excluded']), 'manifest': manifest}
