"""In-place execution and independent review snapshots, using temporary fixtures."""
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dispatcher.external import invocation
from dispatcher.runner import CodexRunner, MultiAgentRunner
from dispatcher.workspace_lock import WorkspaceLease, busy_paths


def report(artifacts=None):
    return {'status': 'complete', 'summary': 'done', 'artifacts': artifacts or ['result.md'],
            'checks': ['read actual file'], 'needs_input': ''}


class WorkspaceRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('dispatcher.workspace_runtime'))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'dispatcher'
        self.root.mkdir()
        self.source = self.base / 'existing'
        self.source.mkdir()
        (self.source / 'result.md').write_text('existing result', encoding='utf-8')
        self.config = {'codex_command': ['fake-codex'], 'model': 'example-model'}
        self.runner = CodexRunner(self.root, self.config)

    def row(self, mode='existing', **changes):
        key = os.path.normpath(str(self.source)).replace('\\', '/').casefold().rstrip('/')
        identifier = 'ws_' + hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]
        choice = {'mode': mode}
        if mode == 'existing':
            choice['workspace_id'] = identifier
        elif mode == 'new':
            choice['name'] = '研究项目'
        row = {'payload': {'projectId': 'p', 'id': 't', 'title': 'Workspace task'},
               'difficulty': 'easy', 'kind': 'document', 'repo': None,
               'current_run_id': 'test-run', 'workspace_choice': choice,
               '_workspace_source': {'id': identifier, 'path': str(self.source),
                                     'label': 'Existing', 'origin': 'codex', 'available': True,
                                     'credential': 'must never be frozen'}}
        row.update(changes)
        self.addCleanup(lambda: self.runner.release_workspace(row))
        return row

    def test_existing_prepare_binds_original_and_separate_snapshot_with_lease(self):
        row = self.row()
        with patch('dispatcher.runner.run_process') as process:
            run = self.runner.prepare(row)
        process.assert_not_called()
        binding = row['_workspace_binding']
        self.assertEqual(binding['strategy'], 'in_place')
        self.assertEqual(Path(binding['workspace_path']), self.source)
        self.assertEqual(Path(binding['snapshot_path']), run / 'workspace')
        self.assertFalse((run / 'workspace').exists())
        self.assertNotIn('credential', binding['source'])
        self.assertEqual(json.loads((run / 'workspace-binding.json').read_text(encoding='utf-8')), binding)
        self.assertIn(str(self.source), busy_paths(self.root))
        self.runner.release_workspace(row)
        self.assertNotIn(str(self.source), busy_paths(self.root))
        self.assertEqual((self.source / 'result.md').read_text(), 'existing result')

    def test_new_mode_creates_persistent_project_and_independent_run(self):
        row = self.row('new')
        run = self.runner.prepare(row)
        binding = row['_workspace_binding']
        actual = Path(binding['workspace_path'])
        self.assertEqual(actual.name, 'project')
        self.assertTrue(actual.parent.name.startswith('研究项目--ws-'))
        self.assertEqual(actual.parent.parent, self.root / 'workspaces' / 'codex')
        self.assertEqual(run.parent.parent, actual.parent)
        self.assertTrue(actual.is_dir())
        self.assertEqual(Path(binding['snapshot_path']), run / 'workspace')
        self.assertFalse((run / 'workspace').exists())

    def test_selected_collection_metadata_is_frozen_including_unassigned_owner(self):
        row=self.row()
        metadata={'collection_root':str(self.base),'collection_label':'Buddy',
                  'collection_agent':'','relative_path':'existing','category':''}
        row['_workspace_source'].update(metadata)
        run=self.runner.prepare(row)
        frozen=self.runner.workspace_binding(row,run)['source']
        row['_workspace_source']['collection_label']='Changed later'
        self.assertEqual({key:frozen[key] for key in metadata},metadata)
        self.assertEqual(self.runner.workspace_binding(row,run)['source']['collection_label'],'Buddy')

    def test_new_workspace_freezes_manual_repository_identity_without_source_tags(self):
        row = self.row('new', repo_alias='chosen-repo', repo=str(self.source))

        def clone(argv, **kwargs):
            Path(argv[-1]).mkdir()
            return '', ''

        with patch('dispatcher.runner.run_process', side_effect=clone):
            run = self.runner.prepare(row)
        binding = row['_workspace_binding']
        self.assertEqual(binding.get('repo_alias'), 'chosen-repo')
        self.assertEqual(binding.get('repo_path'), str(self.source))
        self.assertEqual(json.loads((run / 'workspace-binding.json').read_text(encoding='utf-8')), binding)

    def test_default_mode_keeps_original_layout_and_fresh_binding(self):
        row = self.row('default')
        run = self.runner.prepare(row)
        binding = row['_workspace_binding']
        self.assertEqual(binding['strategy'], 'fresh')
        self.assertEqual(Path(binding['workspace_path']), run / 'workspace')
        self.assertEqual(binding['workspace_path'], binding['snapshot_path'])
        self.assertEqual(json.loads((run / 'workspace-layout.json').read_text())['version'], 2)
        self.assertIn(binding['workspace_path'],busy_paths(self.root))

    def test_guard_rejects_original_workspace_and_overlapping_parent_or_child(self):
        from dispatcher.workspace_guard import start_guard, quarantine_guard
        original=self.source
        child=original/'child';child.mkdir()
        grandchild=child/'nested';grandchild.mkdir()
        frozen=start_guard(self.root,'unfinished',child)
        quarantine_guard(self.root,frozen,'timeout')
        for path in (original,child,grandchild):
            self.source=path
            with self.subTest(path=path),self.assertRaisesRegex(ValueError,'隔离'):
                self.runner.prepare(self.row())
        self.assertEqual(busy_paths(self.root),[])
        self.assertEqual((original/'result.md').read_text(),'existing result')

    def test_default_fresh_directory_is_checked_before_creation(self):
        from dispatcher.workspace_guard import start_guard
        from dispatcher.workspace_runtime import prepare_workspace
        task=self.root/'workspaces/codex/task'
        actual=task/'runs/fixed/workspace'
        start_guard(self.root,'old-owner',actual)
        with self.assertRaisesRegex(ValueError,'保护记录'):
            prepare_workspace(self.root,self.row('default'),'codex',task,'fixed',Mock())
        self.assertFalse(actual.exists())
        self.assertEqual(busy_paths(self.root),[])

    def test_new_persistent_directory_is_checked_before_creation(self):
        from dispatcher.workspace_guard import start_guard
        from dispatcher.workspace_runtime import prepare_workspace
        actual=self.root/'workspaces/codex'/('研究项目--ws-'+'a'*32)/'project'
        start_guard(self.root,'old-owner',actual)
        with patch('dispatcher.workspace_runtime.uuid.uuid4',return_value=Mock(hex='a'*32)):
            with self.assertRaisesRegex(ValueError,'保护记录'):
                prepare_workspace(self.root,self.row('new'),'codex',self.root/'unused','fixed',Mock())
        self.assertFalse(actual.exists())

    def test_guard_created_during_lease_acquisition_is_rechecked(self):
        from dispatcher.workspace_guard import start_guard
        from dispatcher import workspace_runtime
        original=workspace_runtime._assert_unprotected
        calls=[]
        def recheck(root,path):
            calls.append(path)
            if len(calls)==2:start_guard(root,'raced-owner',path)
            return original(root,path)
        row=self.row()
        with patch('dispatcher.workspace_runtime._assert_unprotected',side_effect=recheck):
            with self.assertRaisesRegex(ValueError,'保护记录'):
                self.runner.prepare(row)
        self.assertEqual(len(calls),2)
        self.assertNotIn('_workspace_lease',row)
        self.assertEqual(busy_paths(self.root),[])

    def test_corrupt_guard_registry_blocks_all_workspace_modes(self):
        from dispatcher.workspace_guard import start_guard
        start_guard(self.root,'old-owner',self.source)
        next((self.root/'data/workspace-guards').glob('*.json')).write_text('{',encoding='utf-8')
        for mode in ('default','new','existing'):
            with self.subTest(mode=mode),self.assertRaisesRegex(ValueError,'保护记录'):
                self.runner.prepare(self.row(mode))
        self.assertEqual(busy_paths(self.root),[])

    def test_source_mismatch_and_metadata_overlap_are_rejected(self):
        row = self.row()
        row['_workspace_source']['id'] = 'ws_' + 'f' * 24
        with self.assertRaises(ValueError):
            self.runner.prepare(row)
        for path in (self.root, self.root / 'data'):
            path.mkdir(exist_ok=True)
            self.source = path
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.runner.prepare(self.row())

    def test_invalid_choice_cannot_silently_fall_back_to_default(self):
        for choice in ({}, [], ''):
            with self.subTest(choice=choice), self.assertRaises(ValueError):
                self.runner.prepare(self.row(workspace_choice=choice))

    def test_busy_workspace_cannot_be_prepared(self):
        row = self.row()
        with WorkspaceLease(self.root, self.source, 'other-run'):
            with self.assertRaises(ValueError):
                self.runner.prepare(row)
        self.assertNotIn('_workspace_lease', row)

    def test_managed_workspace_requires_archive_store_before_reuse(self):
        self.source = self.root / 'workspaces' / 'codex' / 'old' / 'runs' / 'one' / 'workspace'
        self.source.mkdir(parents=True)
        with self.assertRaises(ValueError):
            self.runner.prepare(self.row())
        self.assertEqual(busy_paths(self.root), [])

    def test_archive_is_called_while_holding_the_workspace_lease(self):
        row = self.row(_workspace_store=object())
        manager = Mock()
        manager.archive_for_reuse.side_effect = lambda path: self.assertIn(str(path), busy_paths(self.root))
        with patch('dispatcher.workspace.WorkspaceManager', return_value=manager):
            self.runner.prepare(row)
        manager.archive_for_reuse.assert_called_once_with(self.source)

    def test_tampered_binding_is_rejected_before_execution(self):
        row = self.row()
        run = self.runner.prepare(row)
        path = run / 'workspace-binding.json'
        modified = dict(row['_workspace_binding'], workspace_path=str(self.base / 'other'))
        path.write_text(json.dumps(modified), encoding='utf-8')
        with patch.object(self.runner, '_invoke') as invoke:
            with self.assertRaises(ValueError):
                self.runner.execute(row, run)
            invoke.assert_not_called()

    def test_released_workspace_cannot_be_executed_or_verified(self):
        row = self.row()
        run = self.runner.prepare(row)
        self.runner.release_workspace(row)
        with patch.object(self.runner, '_invoke') as invoke:
            with self.assertRaises(ValueError):
                self.runner.execute(row, run)
            with self.assertRaises(ValueError):
                self.runner.verify(row, run, report())
            invoke.assert_not_called()

    def test_metadata_failure_releases_the_acquired_workspace(self):
        row = self.row()
        write_text = Path.write_text

        def fail_task_file(path, *args, **kwargs):
            if path.name == 'task.json':
                raise OSError('simulated metadata failure')
            return write_text(path, *args, **kwargs)

        with patch.object(Path, 'write_text', autospec=True, side_effect=fail_task_file):
            with self.assertRaises(OSError):
                self.runner.prepare(row)
        self.assertEqual(busy_paths(self.root), [])
        self.assertNotIn('_workspace_lease', row)

    def test_codex_execution_uses_actual_cwd_and_host_metadata(self):
        row = self.row()
        run = self.runner.prepare(row)

        def fake_process(argv, **kwargs):
            self.assertEqual(kwargs['cwd'], self.source)
            self.assertEqual(Path(argv[argv.index('--cd') + 1]), self.source)
            self.assertEqual(kwargs['log_prefix'], run / 'execution')
            output = Path(argv[argv.index('--output-last-message') + 1])
            self.assertEqual(output, run / 'execution.json')
            output.write_text(json.dumps(report()), encoding='utf-8')
            return '{"type":"thread.started","thread_id":"test-session"}\n', ''

        with patch('dispatcher.runner.run_process', side_effect=fake_process):
            result = self.runner.execute(row, run)
        self.assertEqual(result['workspace_binding'], row['_workspace_binding'])
        self.assertIn(str(self.source), (run / 'execution-resume.txt').read_text())
        prompt = (run / 'execution.prompt.txt').read_text(encoding='utf-8')
        self.assertIn('原地', prompt)
        self.assertNotIn('不得修改原项目', prompt)

    def test_external_runtime_is_per_run_and_only_runtime_is_added_to_sandbox(self):
        registry = {'dsh': {'enabled': True, 'command': ['fake-dsh']},
                    'codex': {'enabled': True, 'command': ['fake-codex']}}
        with patch('dispatcher.runner.discover_executors', return_value=registry):
            self.runner = MultiAgentRunner(self.root, self.config)
        row = self.row(manual_executor='dsh')
        run = self.runner.prepare(row)
        with patch('dispatcher.runner.run_process', return_value=('', '')) as process, \
                patch('dispatcher.runner.normalize_result', return_value=dict(report(), executor='dsh')), \
                patch('dispatcher.runner.sandbox_command', return_value=['sandbox']) as sandbox:
            result = self.runner.execute(row, run)
        self.assertEqual(process.call_args.kwargs['cwd'], self.source)
        state_roots = sandbox.call_args.args[4]
        self.assertIn(run / 'runtime', state_roots)
        self.assertNotIn(run, state_roots)
        self.assertFalse((self.source / '.runtime').exists())
        settings = json.loads((run / 'runtime' / 'dsh.patch.json').read_text(encoding='utf-8'))
        argv=sandbox.call_args.args[1]
        self.assertEqual(Path(argv[argv.index('--patch')+1]),run/'runtime'/'dsh.patch.json')
        sessions = next(item for item in settings if item['id'] == 'session-persistence-jsonl')
        self.assertEqual(Path(sessions['config']['root']), run / 'runtime' / 'sessions')
        self.assertEqual(result['workspace_binding'], row['_workspace_binding'])

    def test_verify_checks_actual_then_reviews_immutable_snapshot(self):
        row = self.row()
        run = self.runner.prepare(row)

        def checks(*args, **kwargs):
            self.assertEqual(kwargs['workspace'], self.source)
            (self.source / 'REPORT.md').write_text('passed', encoding='utf-8')
            return {'report': 'REPORT.md', 'commands': []}

        with patch('dispatcher.checks.run_repository_checks', side_effect=checks), \
                patch.object(self.runner, '_invoke', return_value=report(['result.md', 'REPORT.md'])) as verify:
            result = self.runner.verify(row, run, report())
        self.assertEqual(verify.call_args.kwargs.get('workspace', run / 'workspace'), run / 'workspace')
        self.assertTrue(verify.call_args.args[4])
        self.assertTrue((run / 'workspace-snapshot.json').is_file())
        self.assertEqual(result['workspace_binding'], row['_workspace_binding'])
        self.assertEqual(result['workspace_snapshot']['file_count'], 2)
        (self.source / 'result.md').write_text('later changes', encoding='utf-8')
        self.assertEqual((run / 'workspace' / 'result.md').read_text(), 'existing result')

    def test_external_vendor_inputs_stay_with_authorized_per_run_runtime(self):
        row=self.row()
        run=self.runner.prepare(row)
        runtime=run/'runtime'
        argv,_,_,roots=invocation('zcode',['node',str(self.base/'vendor'/'lib'/'zcode.cjs')],
            self.source,run,'isolated probe',self.config,
            settings={'model':'glm-5.3-flash','effort':'low'},runtime_dir=runtime)
        for flag in ('--command-json','--request'):
            request=Path(argv[argv.index(flag)+1])
            self.assertEqual(request.parent,runtime)
            self.assertTrue(request.is_file())
        argv,_,_,roots=invocation('antigravity',['agy'],self.source,run,'isolated probe',
            self.config,runtime_dir=runtime)
        schema=Path(argv[argv.index('--json-schema')+1])
        self.assertEqual(schema.parent,runtime)
        self.assertEqual(schema.read_bytes(),(run/'result.schema.json').read_bytes())
        self.assertIn(runtime,roots)
        self.assertNotIn(run,roots)

    def test_protected_artifacts_and_snapshot_failure_do_not_reach_verifier(self):
        row = self.row()
        run = self.runner.prepare(row)
        (self.source / '.env').write_text('private', encoding='utf-8')
        with patch.object(self.runner, '_invoke') as verify:
            with self.assertRaises(ValueError):
                self.runner.verify(row, run, report(['.env']))
            verify.assert_not_called()
        with patch('dispatcher.workspace_runtime.copy_workspace', side_effect=ValueError('changed source')), \
                patch.object(self.runner, '_invoke') as verify:
            with self.assertRaises(ValueError):
                self.runner.verify(row, run, report())
            verify.assert_not_called()

    def test_doubao_protocol_manifests_are_not_artifacts_or_snapshot_content(self):
        from dispatcher.workspace_runtime import snapshot_workspace, validate_artifact_path
        row=self.row();run=self.runner.prepare(row)
        names=['.dispatcher-doubao-test.json','nested/.DISPATCHER-DOUBAO-old.json']
        for name in names:
            protocol=self.source/name;protocol.parent.mkdir(exist_ok=True)
            protocol.write_text('protocol-only',encoding='utf-8')
            with self.assertRaises(ValueError):validate_artifact_path(self.source,name)
        receipt=snapshot_workspace(row,run)
        self.assertEqual(receipt['file_count'],1)
        self.assertEqual(receipt['excluded_count'],2)
        for name in names:
            self.assertFalse((run/'workspace'/name).exists())
            self.assertTrue((self.source/name).exists())

    def test_build_and_dist_deliverables_survive_snapshot(self):
        row = self.row()
        run = self.runner.prepare(row)
        for directory in ('build', 'dist', 'out', 'target'):
            (self.source / directory).mkdir()
            (self.source / directory / 'answer.txt').write_text('deliverable')
        names = [directory + '/answer.txt' for directory in ('build', 'dist', 'out', 'target')]
        with patch.object(self.runner, '_invoke', return_value=report(names)):
            result = self.runner.verify(row, run, report(names))
        self.assertEqual(len(result['sha256']), 4)
        self.assertTrue(all((run / 'workspace' / name).is_file() for name in names))

    def test_checks_can_receive_explicit_workspace(self):
        from dispatcher.checks import run_repository_checks
        run = self.root / 'checks'
        run.mkdir()
        config = {'repositories': {'example': str(self.source)},
                  'repository_checks': {'example': {'commands': [['fake-test']], 'report': 'REPORT.md'}}}
        with patch('dispatcher.adapters.run_process', return_value=('passed', '')) as process:
            result = run_repository_checks({'repo': str(self.source)}, run, config, ['codex'], workspace=self.source)
        self.assertEqual(process.call_args.kwargs['cwd'], self.source)
        self.assertEqual(result['report'], 'REPORT.md')
        self.assertTrue((self.source / 'REPORT.md').is_file())


if __name__ == '__main__':
    unittest.main()
