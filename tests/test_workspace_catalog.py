"""Workspace discovery reads bounded path metadata, never source contents."""

import copy
import hashlib
import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch


class WorkspaceCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'app'
        self.root.mkdir()
        self.home = Path(self.temp.name) / 'profile'
        self.home.mkdir()
        self.roaming = self.home / 'AppData' / 'Roaming'
        self.roaming.mkdir(parents=True)
        self.store = Mock()
        self.store.list_runs.return_value = []
        self.config = {'repositories': {}}

    def catalog(self):
        self.assertIsNotNone(importlib.util.find_spec('dispatcher.workspace_catalog'),
                             'The read-only workspace catalog has not been implemented')
        from dispatcher.workspace_catalog import WorkspaceCatalog
        return WorkspaceCatalog(self.root, self.store, self.config,
                                home=self.home, roaming=self.roaming)

    def test_durable_guard_blocks_parent_child_and_exact_catalog_entries(self):
        from dispatcher.workspace_guard import start_guard, quarantine_guard
        parent=self.directory('projects')
        workspace=self.directory('projects/chosen')
        child=self.directory('projects/chosen/child')
        sibling=self.directory('projects/sibling')
        self.config['repositories']={'parent':str(parent),'workspace':str(workspace),
                                     'child':str(child),'sibling':str(sibling)}
        frozen=start_guard(self.root,'guarded',workspace)
        quarantine_guard(self.root,frozen,'client_failed')
        value=self.catalog().list(include_external=False)
        items={item['repo_alias']:item for item in value['items']}
        for name in ('parent','workspace','child'):
            self.assertFalse(items[name]['available'])
            self.assertIn('隔离',items[name]['reason'])
        self.assertTrue(items['sibling']['available'])
        self.assertNotIn(frozen['guard_id'],json.dumps(value))

    def test_default_run_workspace_remains_unavailable_after_database_failure_state(self):
        from dispatcher.workspace_guard import start_guard
        _,workspace=self.add_run(state='failed')
        start_guard(self.root,'unfinished-native',workspace)
        item=self.catalog().list(include_external=False)['items'][0]
        self.assertFalse(item['available'])
        self.assertIn('保护记录',item['reason'])

    def test_bad_guard_registry_fails_closed_without_scanning_new_sources(self):
        from dispatcher.workspace_guard import start_guard
        workspace=self.directory('selected')
        self.config['repositories']={'selected':str(workspace)}
        start_guard(self.root,'guarded',workspace)
        next((self.root/'data/workspace-guards').glob('*.json')).write_text('{',encoding='utf-8')
        catalog=self.catalog()
        with patch.object(catalog,'_json_paths',side_effect=AssertionError('unexpected external scan')):
            value=catalog.list(include_external=False)
        self.assertFalse(value['items'][0]['available'])
        self.assertTrue(value['warnings'])

    def directory(self, name):
        path = Path(self.temp.name) / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def add_run(self, name='new', agent='zcode', state='done'):
        folder = self.root / 'workspaces' / agent / 'summary--hash' / 'runs' / name
        (folder / 'workspace').mkdir(parents=True)
        row = {'id': name, 'task_id': 'project:' + name, 'run_dir': str(folder), 'executor': agent, 'state': state,
               'payload': {'title': name}, 'updated_at': '2026-11-07T12:00:00Z'}
        self.store.list_runs.return_value.append(row)
        return row, folder / 'workspace'

    def dsh_metadata(self, paths):
        path = self.home / '.dsh' / 'storages' / 'workspace.json'
        self.write_json(path, {'tables': {'workspaces': {
            str(index): {'path': str(value), 'sessionIds': ['DO_NOT_READ_SESSION']}
            for index, value in enumerate(paths)}}})
        return path

    def zcode_metadata(self, path, deleted=0):
        db = self.home / '.zcode' / 'v2' / 'tasks-index.sqlite'
        db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.executescript('CREATE TABLE projects (project_id TEXT, deleted INTEGER);'
                                     'CREATE TABLE project_roots (project_id TEXT, root_path TEXT);'
                                     'CREATE TABLE tasks (searchable_text TEXT);')
            connection.execute('INSERT INTO projects VALUES (?, ?)', ('project', deleted))
            connection.execute('INSERT INTO project_roots VALUES (?, ?)', ('project', str(path)))
            connection.execute('INSERT INTO tasks VALUES (?)', ('DO_NOT_READ_CHAT',))
        return db

    def qoder_metadata(self, path, archived=0, deleted_at=None):
        db = self.roaming / 'com.qoder.app.stable' / 'main.sqlite'
        db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute('CREATE TABLE workspaces (root_paths_json TEXT, archived INTEGER, '
                               'deleted_at TEXT, extra_json TEXT)')
            connection.execute('INSERT INTO workspaces VALUES (?, ?, ?, ?)',
                               (json.dumps([str(path)]), archived, deleted_at, 'DO_NOT_READ_EXTRA'))
        return db

    def test_managed_new_and_legacy_runs_and_public_repositories(self):
        _, new = self.add_run()
        old = self.root / 'workspaces' / 'project-id' / 'task-version' / 'runs' / 'old'
        (old / 'workspace').mkdir(parents=True)
        self.store.list_runs.return_value.append({'id': 'old', 'run_dir': str(old),
            'state': 'done', 'result': {'execution': {'executor': 'codex'}}})
        repo = self.directory('repository')
        self.config['repositories']['demo'] = str(repo)
        before = copy.deepcopy(self.store.list_runs.return_value)
        result = self.catalog().list(include_external=False)
        by_path = {Path(item['path']): item for item in result['items']}
        self.assertEqual(set(by_path), {new, old / 'workspace', repo})
        self.assertEqual(by_path[new]['source_run_id'], 'new')
        self.assertEqual(by_path[new].get('task_id'), 'project:new')
        self.assertIn('派工台', by_path[new].get('source_label', ''))
        self.assertIn('zcode', by_path[new].get('source_label', ''))
        self.assertEqual(by_path[old / 'workspace']['agents'], ['codex'])
        self.assertEqual(by_path[repo]['repo_alias'], 'demo')
        self.assertEqual(by_path[repo]['agents'], [])
        self.assertTrue(all(item['available'] for item in by_path.values()))
        self.assertEqual(result['warnings'], [])
        self.assertEqual(self.store.list_runs.return_value, before)

    def test_stable_casefold_identity_merges_agents_and_prioritizes_requested_agent(self):
        shared, other = self.directory('Shared'), self.directory('Other')
        self.dsh_metadata([shared])
        self.zcode_metadata(str(shared).upper())
        self.qoder_metadata(other)
        catalog = self.catalog()
        all_items = catalog.list()['items']
        shared_item = next(item for item in all_items if 'dsh' in item['agents'])
        self.assertEqual(shared_item['agents'], ['dsh', 'zcode'])
        self.assertTrue(shared_item['id'].startswith('ws_'))
        self.assertEqual(len(shared_item['id']), 27)
        self.assertRegex(shared_item['id'], r'^ws_[0-9a-f]{24}$')
        self.assertEqual(shared_item['id'], catalog.list()['items'][all_items.index(shared_item)]['id'])
        preferred = catalog.list(executor='qoder')['items']
        self.assertIn('qoder', preferred[0]['agents'])
        self.assertEqual({item['id'] for item in preferred}, {item['id'] for item in all_items})

    def test_collection_projects_merge_native_paths_and_hide_summary_containers(self):
        collection=self.directory('desktop/workspace_dsh')
        group=collection/'study';project=group/'course';project.mkdir(parents=True)
        (project/'src').mkdir()
        self.config['workspace_discovery']={'native_agents':True,'collections':[
            {'path':str(collection),'label':'DSH','agent':'dsh','groups':['study']}]}
        self.dsh_metadata([collection,group,project])
        self.qoder_metadata(project)
        before=self.catalog().list()['items']
        self.assertEqual(len(before),1)
        item=before[0]
        self.assertEqual(item['path'],str(project))
        self.assertEqual(item['agents'],['dsh','qoder'])
        self.assertEqual(item['relative_path'],'study/course')
        self.assertEqual(item['category'],'study')
        self.assertEqual(item['collection_label'],'DSH')
        self.assertTrue(item['available'])
        self.config['repositories']['course']=str(project)
        merged=self.catalog().list()['items'][0]
        self.assertEqual(merged['id'],item['id'])
        self.assertEqual(merged['repo_alias'],'course')
        self.assertEqual(merged['collection_label'],'DSH')

    def test_collection_discovery_is_independent_of_recent_native_history_toggle(self):
        collection=self.directory('desktop/workspace_gravity')
        project=collection/'project';project.mkdir()
        self.config['workspace_discovery']={'native_agents':False,'collections':[
            {'path':str(collection),'label':'Antigravity','agent':'antigravity','groups':[]}]}
        items=self.catalog().list(include_external=False)['items']
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['agents'],['antigravity'])
        self.assertEqual(items[0]['category'],'')

    def test_native_subworkspace_keeps_id_and_gains_collection_category(self):
        collection=self.directory('desktop/workspace_dsh')
        project=collection/'study/course';sub=project/'chapter-one';sub.mkdir(parents=True)
        self.dsh_metadata([sub])
        original=self.catalog().list()['items'][0]
        self.config['workspace_discovery']={'collections':[
            {'path':str(collection),'label':'DSH','agent':'dsh','groups':['study']}]}
        found={item['id']:item for item in self.catalog().list()['items']}
        item=found[original['id']]
        self.assertEqual(item['category'],'study')
        self.assertEqual(item['relative_path'],'study/course/chapter-one')
        self.assertEqual(item['collection_agent'],'dsh')

    def test_managed_in_place_source_keeps_collection_category_after_native_merge(self):
        collection=self.directory('desktop/workspace_dsh')
        sub=collection/'study/course/chapter-one';sub.mkdir(parents=True)
        run,_=self.add_run(agent='dsh')
        run['workspace_binding']={'workspace_path':str(sub)}
        self.dsh_metadata([sub])
        self.config['workspace_discovery']={'collections':[
            {'path':str(collection),'label':'DSH','agent':'dsh','groups':['study']}]}
        item=next(item for item in self.catalog().list()['items'] if item['path']==str(sub))
        self.assertEqual(item['source_run_id'],run['id'])
        self.assertEqual(item['origin'],'managed')
        self.assertEqual(item['category'],'study')
        self.assertEqual(item['relative_path'],'study/course/chapter-one')

    def test_scan_failure_does_not_restore_native_collection_containers_or_exclusions(self):
        first=self.directory('desktop/first')
        second=self.directory('desktop/second')
        project=second/'study/course';project.mkdir(parents=True)
        archive=second/'archives/old';archive.mkdir(parents=True)
        self.config['workspace_discovery']={'collections':[
            {'path':str(first),'label':'First','agent':'codex','groups':[]},
            {'path':str(second),'label':'Second','agent':'dsh','groups':['study'],'exclude':['archives']}]}
        self.dsh_metadata([first,second,second/'study',project,archive])
        from dispatcher import workspace_collections
        original=workspace_collections._check_components
        def inaccessible(path):
            if Path(path)==first:raise PermissionError('unavailable')
            return original(path)
        with patch.object(workspace_collections,'_check_components',side_effect=inaccessible):
            result=self.catalog().list()
        self.assertEqual({item['path'] for item in result['items']},{str(project)})
        self.assertTrue(result['warnings'])

    def test_all_sources_merge_without_losing_identity_or_busy_state(self):
        collection=self.directory('desktop/workspace_dsh')
        project=collection/'project';project.mkdir()
        run,_=self.add_run(agent='dsh',state='running')
        run['workspace_binding']={'workspace_path':str(project)}
        self.config['repositories']['linked']=str(project)
        self.config['workspace_discovery']={'collections':[
            {'path':str(collection),'label':'DSH','agent':'dsh'}]}
        self.dsh_metadata([project])
        result=self.catalog().list()
        self.assertEqual(len(result['items']),1)
        item=result['items'][0]
        self.assertFalse(item['available'])
        self.assertEqual(item['source_run_id'],run['id'])
        self.assertEqual(item['task_id'],run['task_id'])
        self.assertEqual(item['repo_alias'],'linked')
        self.assertEqual(item['collection_agent'],'dsh')
        expected='ws_'+hashlib.sha256(str(project).replace('\\','/').casefold().encode()).hexdigest()[:24]
        self.assertEqual(item['id'],expected)

    def test_managed_repository_alias_uses_saved_source_before_unique_configured_tag(self):
        self.config['repositories'] = {'alpha': str(self.directory('repo-alpha')),
                                       'beta': str(self.directory('repo-beta'))}
        row, _ = self.add_run()
        row['payload']['tags'] = ['repo:alpha']
        row['workspace_binding'] = {'source': {'repo_alias': 'beta'}}
        catalog = self.catalog()
        def managed():
            return next(item for item in catalog.list(include_external=False)['items']
                        if item.get('source_run_id') == row['id'])
        self.assertEqual(managed().get('repo_alias'), 'beta')
        row['workspace_binding'] = None
        self.assertEqual(managed().get('repo_alias'), 'alpha')
        row['payload']['tags'] = ['repo:alpha', 'repo:beta']
        self.assertNotIn('repo_alias', managed())
        row['payload']['tags'] = ['repo:missing']
        self.assertNotIn('repo_alias', managed())
        row['payload']['tags'] = []
        self.assertNotIn('repo_alias', managed())

    def test_in_place_run_discovers_native_project_and_never_its_review_snapshot(self):
        row, snapshot = self.add_run()
        native = self.directory('native-project')
        frozen = self.root / 'workspaces' / 'zcode' / 'history' / 'artifact-archive'
        frozen.mkdir(parents=True)
        row['artifact_root'] = str(frozen)
        row['workspace_binding'] = {'mode': 'existing', 'strategy': 'in_place',
            'workspace_path': str(native), 'snapshot_path': str(snapshot)}
        items = self.catalog().list(include_external=False)['items']
        self.assertEqual([Path(item['path']) for item in items], [native])
        self.assertTrue(items[0]['available'])
        self.assertEqual(items[0]['task_id'], row['task_id'])
        self.assertEqual(items[0]['source_run_id'], row['id'])

    def test_native_binding_cannot_bypass_managed_metadata_boundary(self):
        native = self.directory('native-project')
        outside = self.directory('unmanaged-record')
        self.store.list_runs.return_value = [{'id': 'outside', 'task_id': 'p:outside',
            'run_dir': str(outside), 'executor': 'codex', 'state': 'done',
            'workspace_binding': {'strategy': 'in_place', 'workspace_path': str(native)}}]
        items = self.catalog().list(include_external=False)['items']
        self.assertEqual([Path(item['path']) for item in items], [native])
        self.assertFalse(items[0]['available'])

    def test_invalid_native_path_does_not_fall_back_to_review_snapshot(self):
        row, snapshot = self.add_run()
        for invalid in (None, '', [], 'relative-project'):
            with self.subTest(invalid=invalid):
                row['workspace_binding'] = {'strategy': 'in_place', 'workspace_path': invalid,
                                            'snapshot_path': str(snapshot)}
                items = self.catalog().list(include_external=False)['items']
                self.assertFalse(any(Path(item['path']) == snapshot or item['available'] for item in items))

    def test_configured_top_level_repository_alias_precedes_saved_source_and_tags(self):
        self.config['repositories'] = {'alpha': str(self.directory('repo-alpha')),
                                       'beta': str(self.directory('repo-beta')),
                                       'gamma': str(self.directory('repo-gamma'))}
        row, _ = self.add_run()
        row['payload']['tags'] = ['repo:alpha']
        row['workspace_binding'] = {'repo_alias': 'gamma', 'source': {'repo_alias': 'beta'}}
        catalog = self.catalog()
        def alias():
            return next(item for item in catalog.list(include_external=False)['items']
                        if item.get('source_run_id') == row['id']).get('repo_alias')
        self.assertEqual(alias(), 'gamma')
        row['workspace_binding']['repo_alias'] = 'not-configured'
        self.assertEqual(alias(), 'beta')
        row['workspace_binding'].pop('source')
        self.assertEqual(alias(), 'alpha')

    def test_unknown_saved_repository_is_not_replaced_with_a_different_tagged_repository(self):
        self.config['repositories'] = {'alpha': str(self.directory('repo-alpha'))}
        row, _ = self.add_run()
        row['payload']['tags'] = ['repo:alpha']
        row['workspace_binding'] = {'source': {'repo_alias': 'removed-repository'}}
        item = next(item for item in self.catalog().list(include_external=False)['items']
                    if item.get('source_run_id') == row['id'])
        self.assertNotIn('repo_alias', item)

    def test_running_and_verifying_paths_remain_unavailable_when_also_a_repository(self):
        for state in ('running', 'verifying'):
            _, path = self.add_run(name=state, state=state)
            self.config['repositories'][state] = str(path)
        items = self.catalog().list(include_external=False)['items']
        self.assertEqual(len(items), 2)
        self.assertTrue(all(not item['available'] and item['reason'] for item in items))

    def test_rejects_missing_root_profile_unc_private_and_outside_managed_paths(self):
        private = self.home / '.ssh'
        private.mkdir()
        self.config['repositories'] = {'missing': str(self.root / 'absent'), 'drive': self.root.anchor,
            'profile': str(self.home), 'unc': r'\\server\share\repo', 'private': str(private),
            'relative': 'relative/repo'}
        outside = self.directory('outside')
        (outside / 'workspace').mkdir()
        self.store.list_runs.return_value = [{'id': 'escape', 'run_dir': str(outside), 'state': 'done'}]
        result = self.catalog().list(include_external=False)
        self.assertTrue(all(not item['available'] and item['reason'] for item in result['items']))
        self.assertFalse(any(item['available'] for item in result['items']))

    def test_codex_only_consumes_verified_saved_roots_field(self):
        saved, unverified = self.directory('saved'), self.directory('unverified')
        path = self.home / '.codex' / '.codex-global-state.json'
        self.write_json(path, {'electron-saved-workspace-roots': [str(saved)],
            'electron-active-workspace-roots': [str(unverified)],
            'prompt-history': 'DO_NOT_READ_PROMPT'})
        self.write_json(self.home / '.codex' / 'auth.json', {'secret': 'DO_NOT_READ_AUTH'})
        result = self.catalog().list()
        self.assertEqual([Path(item['path']) for item in result['items']], [saved])
        self.assertEqual(result['items'][0]['agents'], ['codex'])
        self.assertNotIn('DO_NOT_READ', json.dumps(result))

    def test_private_metadata_parent_directories_are_not_selectable(self):
        for name in ('.qoder', '.zcode', 'AppData'):
            path = self.home / name
            path.mkdir(exist_ok=True)
            self.config['repositories'][name] = str(path)
        items = self.catalog().list(include_external=False)['items']
        self.assertTrue(all(not item['available'] for item in items))

    def test_sqlite_sources_are_read_only_filter_deleted_and_do_not_change_databases(self):
        zcode, qoder = self.directory('zcode-root'), self.directory('qoder-root')
        databases = [self.zcode_metadata(zcode), self.qoder_metadata(qoder)]
        before = [hashlib.sha256(path.read_bytes()).digest() for path in databases]
        real_connect = sqlite3.connect
        statements = []
        def connect(database, *args, **kwargs):
            self.assertIn('mode=ro', database)
            self.assertTrue(kwargs.get('uri'))
            connection = real_connect(database, *args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection
        with patch('dispatcher.workspace_catalog.sqlite3.connect', side_effect=connect):
            result = self.catalog().list()
            items = result['items']
        self.assertEqual({Path(item['path']) for item in items}, {zcode, qoder}, result)
        self.assertEqual(before, [hashlib.sha256(path.read_bytes()).digest() for path in databases])
        self.assertFalse(any('SELECT *' in sql.upper() or 'SEARCHABLE_TEXT' in sql.upper()
                             or 'EXTRA_JSON' in sql.upper() for sql in statements))
        with closing(real_connect(databases[0])) as connection, connection:
            connection.execute('UPDATE projects SET deleted=1')
        with closing(real_connect(databases[1])) as connection, connection:
            connection.execute('UPDATE workspaces SET archived=1')
        self.assertEqual(self.catalog().list()['items'], [])

    def test_source_format_failures_have_fixed_warnings_without_raw_error_text(self):
        self.write_json(self.home / '.codex' / '.codex-global-state.json',
                        {'electron-saved-workspace-roots': 'DO_NOT_LEAK'})
        self.write_json(self.home / '.dsh' / 'storages' / 'workspace.json', {'DO_NOT_LEAK': []})
        db = self.home / '.zcode' / 'v2' / 'tasks-index.sqlite'
        db.parent.mkdir(parents=True)
        db.write_bytes(b'DO_NOT_LEAK invalid sqlite')
        result = self.catalog().list()
        self.assertEqual(result['items'], [])
        self.assertEqual(len(result['warnings']), 3)
        self.assertNotIn('DO_NOT_LEAK', json.dumps(result))
        self.assertNotIn(str(self.home), json.dumps(result))

    def test_external_false_never_opens_external_metadata_or_source_files(self):
        _, workspace = self.add_run()
        (workspace / 'private.txt').write_text('DO_NOT_READ')
        self.dsh_metadata([self.directory('external')])
        with patch('pathlib.Path.open', side_effect=AssertionError('No file bodies needed')):
            result = self.catalog().list(include_external=False)
        self.assertEqual(len(result['items']), 1)

    def test_metadata_file_and_source_item_limits_are_enforced(self):
        root = self.directory('bounded')
        self.dsh_metadata([root / str(index) for index in range(5)])
        with patch('dispatcher.workspace_catalog.MAX_SOURCE_ITEMS', 2):
            result = self.catalog().list()
        self.assertLessEqual(len(result['items']), 2)
        self.assertTrue(result['warnings'])
        with patch('dispatcher.workspace_catalog.MAX_JSON_BYTES', 8):
            result = self.catalog().list()
        self.assertEqual(result['items'], [])
        self.assertTrue(result['warnings'])

    @unittest.skipUnless(os.name == 'nt', 'Windows reparse point behavior')
    def test_junction_source_and_metadata_are_not_followed(self):
        import _winapi
        target = self.directory('target')
        junction = self.root / 'junction'
        _winapi.CreateJunction(str(target), str(junction))
        self.addCleanup(lambda: junction.rmdir() if junction.exists() else None)
        self.config['repositories']['linked'] = str(junction)
        linked_child = target / 'child'
        linked_child.mkdir()
        self.config['repositories']['linked-child'] = str(junction / 'child')
        items = self.catalog().list(include_external=False)['items']
        self.assertTrue(all(not item['available'] for item in items))
        outside = self.directory('metadata')
        self.write_json(outside / 'storages' / 'workspace.json',
                        {'tables': {'workspaces': {'a': {'path': str(target)}}}})
        link = self.home / '.dsh'
        _winapi.CreateJunction(str(outside), str(link))
        self.addCleanup(lambda: link.rmdir() if link.exists() else None)
        self.config['repositories'] = {}
        result = self.catalog().list()
        self.assertEqual(result['items'], [])
        self.assertTrue(result['warnings'])


if __name__ == '__main__':
    unittest.main()
