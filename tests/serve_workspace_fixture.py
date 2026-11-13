"""Temporary browser QA for workspace selection; never contacts DIDA or an agent.

Run: python -m tests.serve_workspace_fixture --port 8879
Check without a server: python -m tests.serve_workspace_fixture --smoke
All source directories, app indexes, tasks, locks and snapshots belong to one
TemporaryDirectory. The real runtime prepares workspaces and preserves snapshots;
the fixture runner only writes and then reads a small local Markdown artifact.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from dispatcher import workspace_runtime
from dispatcher.control import Controller
from dispatcher.core import Store
from dispatcher.dashboard import start_dashboard
from dispatcher.runner import CodexRunner
from dispatcher.service import Service
from dispatcher.workspace import WorkspaceManager
from dispatcher.workspace_catalog import WorkspaceCatalog
from dispatcher.workspace_lock import WorkspaceLease


class FakeDida:
    def __init__(self, sources):
        self.sources = sources
        self.comments = []
        self.completions = []

    def fetch_tasks(self):
        return [copy.deepcopy(task) for task in self.sources.values()], []

    def get(self, task):
        return copy.deepcopy(self.sources[task['id']])

    def ensure_comment(self, task, marker, text):
        self.comments.append((task['id'], marker, text))

    def complete(self, task):
        self.completions.append(task['id'])
        self.sources[task['id']]['status'] = 2


class FixtureRunner(CodexRunner):
    def __init__(self, root, config, fixture_base):
        super().__init__(root, config)
        self.fixture_base = Path(fixture_base).resolve()

    def _assert_temporary(self, path):
        path = Path(path).resolve()
        if not path.is_relative_to(self.fixture_base):
            raise AssertionError('Fixture refused a directory outside its temporary tree')
        return path

    def _invoke(self, *args, **kwargs):
        raise AssertionError('This fixture never invokes a real agent or subprocess')

    def prepare(self, row):
        if row.get('repo'):
            raise AssertionError('This fixture does not clone repositories')
        if row.get('_workspace_source'):
            self._assert_temporary(row['_workspace_source']['path'])
        executor = row.get('manual_executor') or 'codex'
        run = super().prepare(row, executor=executor)
        route = {'executor': executor, 'reason': '隔离 QA 模拟执行器；未调用真实 agent'}
        (run / 'routing.json').write_text(json.dumps(route, ensure_ascii=False), encoding='utf-8')
        return run

    def execute(self, row, run_dir):
        actual = self._assert_temporary(self.workspace_path(row, run_dir))
        text = (f'# QA FIXTURE SNAPSHOT\n\n任务：{row["payload"]["title"]}\n\n'
                f'运行：{row["current_run_id"]}\n\n'
                '这份内容由隔离模拟执行器写入临时目录。没有调用真实 agent 或滴答账户。\n')
        (actual / 'answer.md').write_text(text, encoding='utf-8')
        (Path(run_dir) / 'execution.events.jsonl').write_text(
            json.dumps({'fixture': True, 'event': 'wrote_temporary_answer',
                        'workspace': str(actual)}, ensure_ascii=False) + '\n', encoding='utf-8')
        return {'status': 'complete', 'summary': '隔离模拟执行完成，等待快照核对与人工复审。',
                'artifacts': ['answer.md'], 'checks': ['仅写入临时目录中的 answer.md'],
                'needs_input': '', 'executor': row['_execution_settings']['executor'],
                'execution_settings': copy.deepcopy(row['_execution_settings']),
                'workspace_binding': self.workspace_binding(row, run_dir)}

    def verify(self, row, run_dir, result):
        receipt = workspace_runtime.snapshot_workspace(row, run_dir)
        snapshot = self._assert_temporary(Path(run_dir) / 'workspace')
        artifact = snapshot / 'answer.md'
        content = artifact.read_bytes()
        if content.splitlines()[0] != b'# QA FIXTURE SNAPSHOT':
            raise AssertionError('Fixture snapshot content does not match the generated artifact')
        reviewed = {'status': 'complete', 'summary': '已只读核对临时验收快照；未调用真实 agent。',
                    'artifacts': ['answer.md'], 'checks': ['实际读取快照 answer.md 并计算 SHA-256'],
                    'needs_input': '', 'execution': copy.deepcopy(result),
                    'workspace_binding': self.workspace_binding(row, run_dir),
                    'sha256': {'answer.md': hashlib.sha256(content).hexdigest()}}
        if receipt is not None:
            reviewed['workspace_snapshot'] = {key: value for key, value in receipt.items()
                                              if key != 'manifest'}
        (Path(run_dir) / 'verification.events.jsonl').write_text(
            json.dumps({'fixture': True, 'event': 'read_snapshot', 'workspace': str(snapshot)},
                       ensure_ascii=False) + '\n', encoding='utf-8')
        return reviewed


@contextmanager
def workspace_fixture():
    with tempfile.TemporaryDirectory(prefix='dispatcher-workspace-browser-qa-') as directory:
        base = Path(directory).resolve()
        root, home = base / 'dispatcher', base / 'fake-user-home'
        root.mkdir()
        paths = {name: base / name for name in
                 ('codex-existing', 'dsh-existing', 'history-live', 'busy-directory', 'missing-directory')}
        for name, path in paths.items():
            if name != 'missing-directory':
                path.mkdir()
                (path / 'seed.txt').write_text('Temporary fixture seed; safe to edit.\n', encoding='utf-8')
        codex_index = home / '.codex' / '.codex-global-state.json'
        codex_index.parent.mkdir(parents=True)
        codex_index.write_text(json.dumps({'electron-saved-workspace-roots': [str(paths[name]) for name in
                               ('codex-existing', 'history-live', 'busy-directory', 'missing-directory')]},
                               ensure_ascii=False), encoding='utf-8')
        dsh_index = home / '.dsh' / 'storages' / 'workspace.json'
        dsh_index.parent.mkdir(parents=True)
        dsh_index.write_text(json.dumps({'tables': {'workspaces': {
            'fixture-dsh': {'path': str(paths['dsh-existing'])}}}}, ensure_ascii=False), encoding='utf-8')
        titles = {
            'default': 'QA 1 · 默认工作区文档',
            'new': 'QA 2 · 新建命名工作区文档',
            'existing': 'QA 3 · 原地修改已有目录文档',
            'history': 'QA 4 · 历史快照与已变化原目录文档',
            'archived': 'QA 5 · 归档重定向的历史快照文档',
            'busy': 'QA 6 · 忙碌目录与草稿保留文档',
        }
        sources = {key: {'id': key, 'projectId': 'workspace-qa', 'title': title, 'status': 0,
                        'tags': [], 'priority': 5 if key in {'default', 'new', 'existing'} else 3,
                        'content': '隔离浏览器验收，全部文件在临时目录。只运行模拟执行器，不写真实滴答。'}
                   for key, title in titles.items()}
        config = {'codex_command': ['fixture-never-executed'], 'model': 'gpt-6.1-sol',
                  'enabled_executors': ['codex', 'dsh', 'zcode', 'qoder', 'antigravity'],
                  'require_human_review': True, 'max_attempts': 20, 'repositories': {},
                  'workspace_discovery': {'native_agents': True}}
        store = Store(root / 'data' / 'tasks.db')
        busy_lease = None
        try:
            service = Service(root, config, store, FakeDida(sources), FixtureRunner(root, config, base))
            service.workspace_policy.catalog = WorkspaceCatalog(root, store, config, home=home)
            manager = WorkspaceManager(root, store)
            control = Controller(service, manager, [{'name': name, 'enabled': True,
                'status': '隔离模拟执行器'} for name in config['enabled_executors']])
            service.sync()

            def seed(key, choice):
                task = store.get('workspace-qa:' + key)
                command = control.submit({'action': 'assign', 'request_id': uuid.uuid4().hex,
                    'task_id': task['id'], 'revision': task['revision'], 'executor': 'codex',
                    'workspace_choice': choice})
                control.process_pending()
                saved = store.get_command(command['id'])
                if saved['status'] != 'done':
                    raise AssertionError(saved['error'])
                claimed = store.claim()
                if not claimed or claimed['id'] != task['id']:
                    raise AssertionError('Fixture claimed an unexpected task')
                service.execute(claimed)
                completed = store.get(task['id'])
                if completed['state'] != 'awaiting_review':
                    raise AssertionError(completed.get('error') or completed['state'])
                return manager.get_run(completed['current_run_id'])

            catalog = control.workspace_catalog(force=True)
            history_source = next(item for item in catalog['items'] if item['path'] == str(paths['history-live']))
            history = seed('history', {'mode': 'existing', 'workspace_id': history_source['id']})
            (paths['history-live'] / 'answer.md').write_text(
                'LIVE_CHANGED_AFTER_SNAPSHOT: this newer text must not appear in the old artifact preview.\n',
                encoding='utf-8')
            archived = seed('archived', {'mode': 'default'})
            archive_original = Path(archived['workspace'])
            manager.archive_for_reuse(archive_original)
            (archive_original / 'answer.md').write_text(
                'LIVE_CHANGED_AFTER_ARCHIVE: history must read the redirected snapshot.\n', encoding='utf-8')
            busy_lease = WorkspaceLease(root, paths['busy-directory'], 'fixture-held-busy-run')
            busy_lease.__enter__()
            service.workspace_policy.invalidate()
            fixture = {'base': base, 'root': root, 'paths': paths, 'store': store, 'service': service,
                       'control': control, 'history_run_id': history['id'], 'archived_run_id': archived['id'],
                       'tasks': {key: 'workspace-qa:' + key for key in titles}}
            yield fixture
        finally:
            if busy_lease is not None:
                busy_lease.__exit__(None, None, None)
            store.close()


def verify_fixture(fixture):
    control, service = fixture['control'], fixture['service']
    state = control.state()
    assert state['workspace_policy']['reuse_strategy'] == 'in_place'
    assert len(state['tasks']) == 6
    catalog = control.workspace_catalog(force=True)
    by_path = {item['path']: item for item in catalog['items']}
    assert by_path[str(fixture['paths']['codex-existing'])]['available']
    assert by_path[str(fixture['paths']['dsh-existing'])]['agents'] == ['dsh']
    assert not by_path[str(fixture['paths']['missing-directory'])]['available']
    assert not by_path[str(fixture['paths']['busy-directory'])]['available']
    for run_id in (fixture['history_run_id'], fixture['archived_run_id']):
        preview = control.workspaces.preview(run_id, 'answer.md')['text']
        assert 'QA FIXTURE SNAPSHOT' in preview and 'LIVE_CHANGED_' not in preview
    archived = control.workspaces.get_run(fixture['archived_run_id'])
    assert archived['artifact_root'] == archived['workspace']
    assert archived['workspace'] != archived['workspace_binding']['snapshot_path']
    assert not service.dida.comments and not service.dida.completions
    return {'tasks': len(state['tasks']), 'catalog_items': len(catalog['items']),
            'historical_snapshots_preserved': 2, 'real_agent_calls': 0, 'real_dida_calls': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8879)
    parser.add_argument('--smoke', action='store_true')
    args = parser.parse_args()
    with workspace_fixture() as fixture:
        checks = verify_fixture(fixture)
        if args.smoke:
            print(json.dumps({'fixture_check': 'passed', **checks}, ensure_ascii=False), flush=True)
            return
        service, control = fixture['service'], fixture['control']
        with start_dashboard(service.snapshot, args.port, control) as server:
            address = f'http://127.0.0.1:{server.server_address[1]}'
            print(json.dumps({'url': address, 'temporary_root': str(fixture['base']),
                'tasks': fixture['tasks'], 'source_directories': {key: str(path) for key, path in fixture['paths'].items()},
                'history_run_id': fixture['history_run_id'], 'archived_run_id': fixture['archived_run_id'],
                'checks': checks}, ensure_ascii=False), flush=True)
            try:
                while True:
                    control.process_pending()
                    service.process_writebacks()
                    row = fixture['store'].claim()
                    if row:
                        service.execute(row)
                    time.sleep(.1)
            except KeyboardInterrupt:
                service.stop.set()


if __name__ == '__main__':
    main()
