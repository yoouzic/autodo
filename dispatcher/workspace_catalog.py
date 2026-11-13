"""Bounded, read-only discovery of local workspace paths.

Catalog membership is discovery metadata, not permission to write a directory.
External readers deliberately project only known workspace-path fields.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import sqlite3
import stat
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

from .core import task_tags
from .routing import EXECUTORS
from .workspace_guard import WorkspaceGuardError, active_guards


MAX_SOURCE_ITEMS = 500
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_SQLITE_BYTES = 256 * 1024 * 1024
MAX_PATH_LENGTH = 4096
_SOURCE_LABELS = {'managed': '派工台', 'repository': '配置仓库', 'codex': 'Codex',
                  'dsh': 'DSH', 'zcode': 'ZCode', 'qoder': 'Qoder'}


def _path_key(path):
    return os.path.normpath(str(path)).replace('\\', '/').casefold().rstrip('/')


def _within(path, parent):
    key, base = _path_key(path), _path_key(parent)
    return key == base or key.startswith(base + '/')


def _has_reparse(path):
    """Check every component before following any symlink or Windows junction."""
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            return True
    return False


def apply_workspace_guards(root, value):
    """Overlay current durable occupancy without caching or exposing guard IDs."""
    try:
        guards = active_guards(root)
    except WorkspaceGuardError:
        reason = '工作区持续保护记录暂不可确认；已有目录暂不可分配。'
        if reason not in value['warnings']:
            value['warnings'].append(reason)
        for item in value['items']:
            item.update(available=False, reason=reason)
        return value
    for item in value['items']:
        matches = [guard for guard in guards if _within(item['path'], guard['workspace']) or
                   _within(guard['workspace'], item['path'])]
        if matches:
            quarantined = any(guard['status'] == 'quarantined' for guard in matches)
            reason = ('该目录或其上下级已隔离：上次任务未确认停止，不能复用。' if quarantined else
                      '该目录或其上下级仍有未确认结束的任务保护记录，暂不能复用。')
            item.update(available=False, reason=reason)
    return value


class WorkspaceCatalog:
    def __init__(self, root, store, config, *, home=None, roaming=None):
        self.root = Path(os.path.abspath(root))
        self.store, self.config = store, config
        self.home = Path(home) if home is not None else Path.home()
        # An injected home must never fall back to the real user's APPDATA.
        self.roaming = (Path(roaming) if roaming is not None else
                        self.home / 'AppData' / 'Roaming' if home is not None else
                        Path(os.environ.get('APPDATA', str(self.home / 'AppData' / 'Roaming'))))
        self._private_roots = [self.home / name for name in
            ('.ssh', '.aws', '.azure', '.kube', '.gnupg', '.codex', '.dsh',
             '.gemini', '.qoder/.auth', '.zcode/v2', '.codebuddy', '.workbuddy', '.workbuddy-key-fallback', '.config/gcloud')]
        self._private_roots += [self.roaming / name for name in
                               ('Codex', 'ZCode', 'Qoder', 'Antigravity', 'com.qoder.app.stable')]
        local=(self.home/'AppData/Local' if home is not None else
               Path(os.environ.get('LOCALAPPDATA',str(self.home/'AppData/Local'))))
        self._private_roots.append(local/'CodeBuddyExtension')

    @staticmethod
    def _warning(warnings, source, kind='unreadable'):
        suffix = {'unreadable': '工作区索引暂不可读，已跳过。',
                  'limit': '工作区目录数量超过限制，仅显示部分目录。',
                  'size': '工作区索引过大，已跳过。'}[kind]
        text = _SOURCE_LABELS[source] + suffix
        if text not in warnings:
            warnings.append(text)

    def _bounded(self, values, source, warnings):
        for index, value in enumerate(itertools.islice(values, MAX_SOURCE_ITEMS + 1)):
            if index == MAX_SOURCE_ITEMS:
                self._warning(warnings, source, 'limit')
                break
            yield value

    def _candidate(self, value, *, agents=(), origin, label=None, source_run_id=None,
                   repo_alias=None, blocked_reason=None, task_id=None, source_label=None):
        if not isinstance(value, (str, Path)) or not str(value).strip():
            return None
        value = str(value)
        if '\x00' in value or len(value) > MAX_PATH_LENGTH:
            return None
        path = Path(os.path.normpath(value))
        windows = PureWindowsPath(value)
        reason = blocked_reason or ''
        modified_at = None
        if (value.startswith(('\\\\', '//')) or windows.drive.startswith('\\\\') or
                not path.is_absolute() or ':' in value[len(windows.drive):]):
            reason = '目录路径必须为本机绝对路径。'
        elif path == Path(path.anchor):
            reason = '不能选择磁盘根目录。'
        elif _path_key(path) == _path_key(self.home):
            reason = '不能选择用户主目录。'
        elif any(_within(path, private) or _within(private, path) for private in self._private_roots):
            reason = '不能选择认证或应用私有数据目录。'
        else:
            try:
                if _has_reparse(path):
                    reason = '不能选择符号链接或重解析点目录。'
                else:
                    info = path.stat()
                    if not stat.S_ISDIR(info.st_mode):
                        reason = '目录不存在或不是文件夹。'
                    else:
                        modified_at = datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat()
            except (OSError, ValueError, OverflowError):
                reason = '目录不存在或当前不可访问。'
        item = {'id': 'ws_' + hashlib.sha256(_path_key(path).encode('utf-8')).hexdigest()[:24],
                'label': str(label or path.name or path)[:160], 'path': str(path),
                'agents': sorted(set(agents) & set(EXECUTORS)), 'origin': origin,
                'available': not reason, 'reason': reason, 'modified_at': modified_at}
        if source_run_id:
            item['source_run_id'] = str(source_run_id)
        if repo_alias:
            item['repo_alias'] = str(repo_alias)
        if task_id:
            item['task_id'] = str(task_id)
        if source_label:
            item['source_label'] = str(source_label)[:160]
        return item

    def _managed_repo_alias(self, row, payload):
        repositories = self.config.get('repositories') or {}
        if not isinstance(repositories, Mapping):
            return None
        binding = row.get('workspace_binding')
        alias = binding.get('repo_alias') if isinstance(binding, Mapping) else None
        if isinstance(alias, str) and alias in repositories:
            return alias
        source = binding.get('source') if isinstance(binding, Mapping) else None
        if isinstance(source, Mapping) and source.get('repo_alias') is not None:
            alias = source['repo_alias']
            # A removed saved source must not be replaced by a different current tag.
            return alias if isinstance(alias, str) and alias in repositories else None
        aliases = {tag[5:] for tag in task_tags(payload) if tag.lower().startswith('repo:')}
        if len(aliases) == 1:
            alias = next(iter(aliases))
            return alias if alias in repositories else None
        return None

    def _managed(self, warnings):
        try:
            rows = self.store.list_runs()
            for row in self._bounded(rows, 'managed', warnings):
                if not isinstance(row, Mapping) or not isinstance(row.get('run_dir'), str):
                    continue
                run = Path(row['run_dir'])
                if not run.is_absolute():
                    run = self.root / run
                run = Path(os.path.normpath(run))
                result = row.get('result') if isinstance(row.get('result'), Mapping) else {}
                execution = result.get('execution') if isinstance(result.get('execution'), Mapping) else result
                agent = row.get('executor') or execution.get('executor')
                if agent not in EXECUTORS:
                    agent = next((name for name in EXECUTORS
                                  if _within(run, self.root / 'workspaces' / name)), None)
                if agent is None and execution.get('thread_id'):
                    agent = 'codex'
                payload = row.get('payload') if isinstance(row.get('payload'), Mapping) else {}
                binding = row.get('workspace_binding')
                binding = binding if isinstance(binding, Mapping) else {}
                # A present but invalid native path must never become the review snapshot.
                workspace = binding.get('workspace_path', run / 'workspace')
                reason = None
                if not _within(run, self.root / 'workspaces') or run == self.root / 'workspaces':
                    reason = '工作区不在派工台管理目录内。'
                elif _has_reparse(run) or not run.is_dir():
                    reason = '运行记录目录不存在或使用了重解析点。'
                elif row.get('state') in {'running', 'verifying'}:
                    reason = '来源任务正在运行或复核，暂不可选择。'
                yield self._candidate(workspace, agents=[agent] if agent else [],
                    origin='managed', label=payload.get('title'), source_run_id=row.get('id'),
                    blocked_reason=reason, task_id=row.get('task_id'),
                    repo_alias=self._managed_repo_alias(row, payload),
                    source_label=' · '.join(str(value) for value in
                        ('派工台', agent, row.get('id')) if value))
        except (OSError, ValueError, TypeError, sqlite3.Error):
            self._warning(warnings, 'managed')

    def _repositories(self, warnings):
        repositories = self.config.get('repositories') or {}
        if not isinstance(repositories, Mapping):
            self._warning(warnings, 'repository')
            return
        for alias, value in self._bounded(repositories.items(), 'repository', warnings):
            yield self._candidate(value, origin='repository', label=alias, repo_alias=alias)

    def _metadata_file(self, path, source, warnings, maximum):
        try:
            if _has_reparse(path):
                self._warning(warnings, source)
                return False
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                self._warning(warnings, source)
                return False
            if info.st_size > maximum:
                self._warning(warnings, source, 'size')
                return False
            return True
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            self._warning(warnings, source)
            return False

    def _json_paths(self, source, path, warnings):
        if not self._metadata_file(path, source, warnings, MAX_JSON_BYTES):
            return
        try:
            with path.open('rb') as handle:
                raw = handle.read(MAX_JSON_BYTES + 1)
            if len(raw) > MAX_JSON_BYTES:
                self._warning(warnings, source, 'size')
                return
            data = json.loads(raw)
            if source == 'codex':
                values = data['electron-saved-workspace-roots']
                if not isinstance(values, list):
                    raise ValueError('schema')
            else:
                records = data['tables']['workspaces']
                if not isinstance(records, dict):
                    raise ValueError('schema')
                values = (record.get('path') if isinstance(record, dict) else None
                          for record in records.values())
            for value in self._bounded(values, source, warnings):
                if not isinstance(value, str):
                    self._warning(warnings, source)
                    continue
                yield value
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            self._warning(warnings, source)

    def _sqlite_paths(self, source, path, warnings):
        if not self._metadata_file(path, source, warnings, MAX_SQLITE_BYTES):
            return
        connection = None
        try:
            connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.2)
            connection.execute('PRAGMA query_only=ON')
            steps = 0
            def bounded_query():
                nonlocal steps
                steps += 1
                return steps > 100
            connection.set_progress_handler(bounded_query, 10000)
            if source == 'zcode':
                query = ('SELECT DISTINCT r.root_path FROM project_roots AS r '
                         'JOIN projects AS p ON p.project_id=r.project_id '
                         'WHERE p.deleted=0 LIMIT ?')
            else:
                query = ('SELECT root_paths_json FROM workspaces '
                         'WHERE deleted_at IS NULL AND archived=0 LIMIT ?')
            count = 0
            for (raw,) in connection.execute(query, (MAX_SOURCE_ITEMS + 1,)):
                if source == 'qoder':
                    if not isinstance(raw, str) or len(raw) > 64 * 1024:
                        raise ValueError('schema')
                    values = json.loads(raw)
                    if not isinstance(values, list):
                        raise ValueError('schema')
                else:
                    values = [raw]
                for value in values:
                    if count >= MAX_SOURCE_ITEMS:
                        self._warning(warnings, source, 'limit')
                        return
                    count += 1
                    if not isinstance(value, str):
                        raise ValueError('schema')
                    yield value
        except (OSError, ValueError, TypeError, sqlite3.Error, RecursionError):
            self._warning(warnings, source)
        finally:
            if connection is not None:
                connection.close()

    def list(self, executor=None, include_external=True, *, include_guards=True):
        """List all sources, sorting a requested agent first without hiding others."""
        warnings, items = [], {}
        sources = [self._managed(warnings), self._repositories(warnings)]
        from .workspace_collections import discover_collections
        collections=discover_collections(self.config.get('workspace_discovery',{}).get('collections',[]),
                                          home=self.home,roaming=self.roaming)
        warnings.extend(collections['warnings'])
        collection_items=[]
        for entry in collections['items']:
            candidate=self._candidate(entry['path'],agents=entry.get('agents',[]),origin='collection',label=entry['label'])
            if candidate is not None:
                candidate.update({key:entry[key] for key in ('collection_label','collection_root','relative_path','category')})
                candidate['collection_agent']=entry.get('collection_agent','')
                collection_items.append(candidate)
        sources.append(collection_items)
        if include_external:
            for source, path, reader in (
                ('codex', self.home / '.codex' / '.codex-global-state.json', self._json_paths),
                ('dsh', self.home / '.dsh' / 'storages' / 'workspace.json', self._json_paths),
                ('zcode', self.home / '.zcode' / 'v2' / 'tasks-index.sqlite', self._sqlite_paths),
                ('qoder', self.roaming / 'com.qoder.app.stable' / 'main.sqlite', self._sqlite_paths),
            ):
                # Materialize each bounded source here, avoiding late-bound generator variables.
                sources.append([self._candidate(value, agents=[source], origin='external')
                                for value in reader(source, path, warnings)])
        for source_items in sources:
            for item in source_items:
                if item is None:
                    continue
                previous = items.get(item['id'])
                if previous is None:
                    items[item['id']] = item
                    continue
                previous['agents'] = sorted(set(previous['agents']) | set(item['agents']))
                for key in ('source_run_id', 'repo_alias', 'task_id', 'source_label'):
                    if key in item and key not in previous:
                        previous[key] = item[key]
                if 'collection_root' in item:
                    previous.update({key:item[key] for key in ('label','collection_label','collection_root','collection_agent','relative_path','category')})
                if not item['available']:
                    previous.update(available=False, reason=item['reason'])
        containers={_path_key(path) for path in collections['containers']}
        excluded=collections.get('excluded',[])
        for item in items.values():
            if 'collection_root' in item or (item['origin']=='managed' and _within(item['path'],self.root/'workspaces')):continue
            for spec in self.config.get('workspace_discovery',{}).get('collections',[]):
                parent=Path(spec['path']);path=Path(item['path'])
                if not _within(path,parent):continue
                try:relative=path.relative_to(parent).as_posix()
                except ValueError:continue
                parents=[Path(group) for group in collections['containers']
                         if _within(group,parent) and _path_key(group)!=_path_key(parent) and _within(path,group)]
                category=max(parents,key=lambda folder:len(folder.parts)).relative_to(parent).as_posix() if parents else ''
                item.update(collection_root=str(parent),collection_label=spec['label'],
                            collection_agent=spec.get('agent',''),relative_path=relative,category=category)
                if spec.get('agent'):item['agents']=sorted(set(item['agents'])|{spec['agent']})
                break
        result = sorted((item for item in items.values() if _path_key(item['path']) not in containers and
                         not any(_within(item['path'],path) for path in excluded)), key=lambda item: (
            0 if executor and (item.get('collection_agent')==executor if 'collection_agent' in item else executor in item['agents']) else 1 if not item['agents'] else 2,
            'collection_root' not in item, not item['available'],
            item.get('collection_label','').casefold(),item.get('category','').casefold(),item['label'].casefold(), item['id']))
        value = {'items': result, 'warnings': warnings}
        return apply_workspace_guards(self.root, value) if include_guards else value
