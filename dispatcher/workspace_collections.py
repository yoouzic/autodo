"""Discover projects under explicit collection containers without reading files."""
from __future__ import annotations

import os
import stat
import unicodedata
from pathlib import Path, PureWindowsPath

from .routing import EXECUTORS
from .workspace_layout import WINDOWS_DEVICE_NAMES


MAX_DIRS_PER_ROOT = 500
MAX_SCAN_ENTRIES = 2000
MAX_GROUP_DEPTH = 4
SKIPPED_NAMES = frozenset({
    'node_modules', 'venv', 'env', '__pycache__', 'cache', 'caches',
    'build', 'dist', 'out', 'target', 'coverage', 'bower_components',
})


def _key(path):
    return os.path.normpath(str(path)).replace('\\', '/').casefold().rstrip('/')


def _within(path, root):
    value, base = _key(path), _key(root)
    return value == base or value.startswith(base + '/')


def _reparse(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _check_components(path):
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if _reparse(info):
            raise ValueError('Collection paths cannot use links or reparse points')


def _local_root(value, home, roaming, *, inspect_filesystem=True):
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError('Collection path must be a local absolute directory')
    text = str(value)
    path, windows = Path(text), PureWindowsPath(text)
    if (not path.is_absolute() or text.startswith(('\\\\', '//')) or
            windows.drive.startswith('\\\\') or ':' in text[len(windows.drive):] or
            any(unicodedata.category(char) in {'Cc', 'Cf', 'Cs'} for char in text)):
        raise ValueError('Collection path must be a local absolute directory')
    path = Path(os.path.abspath(path))
    if inspect_filesystem:
        _check_components(path)
        path = path.resolve(strict=False)
        _check_components(path)
    if path == Path(path.anchor) or _within(home, path):
        raise ValueError('Disk roots and user profile roots cannot be collections')
    private = [home / name for name in (
        '.ssh', '.aws', '.azure', '.kube', '.gnupg', '.codex', '.claude', '.agents',
        '.dsh', '.gemini', '.qoder', '.zcode', '.codebuddy', '.workbuddy', '.workbuddy-key-fallback', '.config/gcloud')]
    private += [roaming / name for name in ('Codex', 'ZCode', 'Qoder', 'Antigravity', 'com.qoder.app.stable')]
    private.append(home/'AppData/Local/CodeBuddyExtension')
    if any(_within(path, candidate) or _within(candidate, path) for candidate in private):
        raise ValueError('Authentication and agent-private directories cannot be collections')
    return path


def _skip_name(name):
    return name.startswith('.') or name.casefold() in SKIPPED_NAMES


def _relative(value, *, group):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError('Collection relative paths must be nonempty strings')
    windows = PureWindowsPath(value)
    raw_parts = value.replace('\\', '/').split('/')
    if windows.drive or windows.root or value.startswith('/') or '..' in raw_parts or ':' in value:
        raise ValueError('Collection relative paths must stay inside their root')
    parts = [part for part in raw_parts if part not in {'', '.'}]
    if not parts or group and len(parts) > MAX_GROUP_DEPTH:
        raise ValueError('Collection groups support at most four relative directory levels')
    for part in parts:
        normalized = unicodedata.normalize('NFKC', part)
        for name in (part, normalized):
            if (not name or name != name.strip() or name in {'.', '..'} or name.endswith('.') or
                    any(char in '<>:"/\\|?*' or unicodedata.category(char) in {'Cc', 'Cf', 'Cs'} for char in name) or
                    name.split('.', 1)[0].rstrip().upper() in WINDOWS_DEVICE_NAMES or
                    group and _skip_name(name)):
                raise ValueError('Collection relative paths must use safe directory names without globs')
    return '/'.join(parts)


def _normalize_collections(specs, *, home=None, roaming=None, inspect_filesystem):
    if not isinstance(specs, list):
        raise ValueError('Workspace collections must be a list')
    profile = Path(home) if home is not None else Path.home()
    profile = Path(os.path.abspath(profile))
    roaming = (Path(roaming) if roaming is not None else profile / 'AppData' / 'Roaming'
               if home is not None else Path(os.environ.get('APPDATA', str(profile / 'AppData/Roaming'))))
    normalized, roots, labels = [], [], set()
    for spec in specs:
        if not isinstance(spec, dict) or set(spec) - {'path', 'label', 'agent', 'groups', 'exclude'}:
            raise ValueError('Collection specifications contain unsupported fields')
        label = spec.get('label')
        if (not isinstance(label, str) or not label.strip() or len(label.strip()) > 160 or
                any(unicodedata.category(char) in {'Cc', 'Cf', 'Cs'} for char in label)):
            raise ValueError('Collection label must be nonempty text of at most 160 characters')
        label_key=unicodedata.normalize('NFKC',label.strip()).casefold()
        if label_key in labels:
            raise ValueError('Collection labels must be unique so projects remain distinguishable')
        labels.add(label_key)
        if 'agent' in spec and spec['agent'] not in EXECUTORS:
            raise ValueError('Collection agent must name a supported executor')
        root = _local_root(spec.get('path'), profile, roaming, inspect_filesystem=inspect_filesystem)
        if any(_within(root, existing) or _within(existing, root) for existing in roots):
            raise ValueError('Collection roots must not overlap')
        roots.append(root)
        groups, excluded = {}, {}
        for field in ('groups', 'exclude'):
            values = spec.get(field, [])
            if not isinstance(values, list):
                raise ValueError(f'Collection {field} must be a list')
            for value in values:
                relative = _relative(value, group=field == 'groups')
                if field == 'groups':
                    parts = relative.split('/')
                    for depth in range(1, len(parts) + 1):
                        parent = '/'.join(parts[:depth])
                        groups.setdefault(parent.casefold(), parent)
                else:
                    excluded.setdefault(relative.casefold(), relative)
        result = {'path': str(root), 'label': label.strip(),
                  'groups': sorted(groups.values(), key=lambda value: (value.count('/'), value.casefold())),
                  'exclude': sorted(excluded.values(), key=str.casefold)}
        if 'agent' in spec:
            result['agent'] = spec['agent']
        normalized.append(result)
    return normalized


def validate_collections(specs, *, home=None, roaming=None):
    """Strict configuration validation includes current filesystem path safety."""
    return _normalize_collections(specs, home=home, roaming=roaming, inspect_filesystem=True)


class _LimitReached(Exception):
    def __init__(self, global_limit):
        self.global_limit = global_limit


def discover_collections(specs, *, home=None, roaming=None):
    """Read directory entries only; never infer groups from project file contents."""
    result = {'items': [], 'containers': [], 'excluded': [], 'warnings': []}
    try:
        specs = _normalize_collections(specs, home=home, roaming=roaming, inspect_filesystem=False)
    except (OSError, ValueError, RuntimeError):
        result['warnings'].append('工作区汇总目录配置无效或当前不可安全访问，已停止该来源扫描。')
        return result
    # Reserve every declared container/exclusion before scanning, including
    # missing directories and containers beyond a scan limit.
    for spec in specs:
        root = Path(spec['path'])
        result['containers'].extend(str(root / relative) for relative in ['', *spec['groups']])
        result['excluded'].extend(str(root / relative) for relative in spec['exclude'])
    scanned = 0

    def warning(spec, message):
        text = f'汇总目录「{spec["label"]}」{message}'
        if text not in result['warnings']:
            result['warnings'].append(text)

    for spec in specs:
        root = Path(spec['path'])
        try:
            checked = validate_collections([spec], home=home, roaming=roaming)[0]
            if _key(checked['path']) != _key(root):
                raise ValueError('Collection root identity changed from its configured path')
        except (OSError, ValueError, RuntimeError):
            warning(spec, '根目录不可读、已变化或使用了链接，已跳过；分类与排除边界仍保留。')
            continue
        groups = {value.casefold() for value in spec['groups']}
        exclusions = [root / relative for relative in spec['exclude']]
        directory_count = 0
        observed_groups = set()

        def visit(directory, relative=''):
            nonlocal scanned, directory_count
            try:
                _check_components(directory)
                before = directory.lstat()
                if not stat.S_ISDIR(before.st_mode) or _reparse(before):
                    raise ValueError('Not an ordinary directory')
                with os.scandir(directory) as entries:
                    for entry in entries:
                        scanned += 1
                        if scanned > MAX_SCAN_ENTRIES:
                            raise _LimitReached(True)
                        name = entry.name
                        if _skip_name(name):
                            continue
                        path = directory / name
                        if any(_within(path, excluded) for excluded in exclusions):
                            continue
                        info = entry.stat(follow_symlinks=False)
                        if _reparse(info):
                            warning(spec, '包含链接或重解析点，已跳过。')
                            continue
                        if not stat.S_ISDIR(info.st_mode):
                            continue
                        directory_count += 1
                        if directory_count > MAX_DIRS_PER_ROOT:
                            raise _LimitReached(False)
                        if getattr(info, 'st_file_attributes', 0) & (0x2 | 0x4):
                            continue
                        relative_path = path.relative_to(root).as_posix()
                        if relative_path.casefold() in groups:
                            observed_groups.add(relative_path.casefold())
                            visit(path, relative_path)
                        else:
                            agent = spec.get('agent', '')
                            result['items'].append({
                                'path': str(path), 'agents': [agent] if agent else [], 'origin': 'collection',
                                'label': name, 'collection_label': spec['label'], 'collection_root': str(root),
                                'relative_path': relative_path, 'category': relative,
                                'collection_agent': agent})
                _check_components(directory)
                after = directory.lstat()
                if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    raise ValueError('Directory identity changed')
            except (OSError, ValueError, RuntimeError):
                warning(spec, '有目录不存在、不可读或已变化，已跳过。')

        try:
            visit(root)
        except _LimitReached as exc:
            warning(spec, '达到全局扫描条目上限，剩余目录未扫描。' if exc.global_limit else
                    '达到本根目录扫描上限，剩余目录未扫描。')
            if exc.global_limit:
                break
        else:
            missing = [root / relative for relative in spec['groups'] if relative.casefold() not in observed_groups]
            if any(not any(_within(path, excluded) for excluded in exclusions) for path in missing):
                warning(spec, '部分分类目录不存在或已跳过。')
    result['items'].sort(key=lambda item: (item['collection_root'].casefold(), item['relative_path'].casefold()))
    return result
