"""Read-only run browsing must remain confined to declared local outputs."""

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dispatcher.workspace import WorkspaceManager, _artifact_name


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / 'workspaces' / 'p' / 'task' / 'runs' / 'one'
        self.workspace = self.run / 'workspace'
        self.workspace.mkdir(parents=True)
        self.payload = {'id': 'one', 'projectId': 'p', 'title': 'Historical title'}
        self.row = {'id': 'opaque-run-id', 'task_id': 'p:one', 'run_dir': str(self.run),
                    'payload': self.payload, 'result': {'artifacts': ['answer.md']}, 'state': 'done'}
        self.store = Mock()
        self.store.get_run.side_effect = lambda identifier: copy.deepcopy(self.row) if identifier == self.row['id'] else None
        self.store.list_runs.return_value = [self.row]
        self.store.list_tasks.return_value = []
        self.manager = WorkspaceManager(self.root, self.store)
        (self.workspace / 'answer.md').write_text('Hello 世界', encoding='utf-8')

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def history_runs(self):
        return (self.run, self.root / 'workspaces' / 'zcode' / '中文摘要--5a6ee313ee' / 'runs' / 'two')

    def test_run_details_include_declared_files_without_mutating_store(self):
        before = copy.deepcopy(self.row)
        info = self.manager.get_run(self.row['id'])
        self.assertEqual(info['title'], 'Historical title')
        self.assertEqual(Path(info['workspace']), self.workspace)
        self.assertEqual(info['artifacts'], [{'path': 'answer.md', 'size': 12, 'exists': True}])
        self.assertEqual(self.row, before)
        self.assertEqual(self.manager.list_runs('p:one', False), [info])
        self.store.list_runs.assert_called_with(task_id='p:one', include_archived=False)

    def test_unknown_run_and_root_or_foreign_run_are_rejected(self):
        with self.assertRaises(KeyError):
            self.manager.resolve_run('../not-a-run')
        for path in (self.root, self.root / 'workspaces', self.root.parent):
            with self.subTest(path=path):
                self.row['run_dir'] = str(path)
                with self.assertRaises(ValueError):
                    self.manager.resolve_run(self.row['id'])

    def test_unsafe_declared_paths_and_undeclared_files_are_rejected(self):
        for run in self.history_runs():
            with self.subTest(run=run):
                workspace = run / 'workspace'
                workspace.mkdir(parents=True, exist_ok=True)
                self.row['run_dir'] = str(run)
                for name in ('../secret.txt', '..\\secret.txt', '/absolute.txt', 'C:\\secret.txt',
                             'C:secret.txt', '//server/share.txt', 'answer.md:secret', ''):
                    self.row['result']['artifacts'] = [name]
                    with self.subTest(name=name), self.assertRaises(ValueError):
                        self.manager.resolve_artifact(self.row['id'], name)
                self.row['result']['artifacts'] = ['answer.md']
                (workspace / 'undeclared.md').write_text('not declared')
                with self.assertRaises(ValueError):
                    self.manager.resolve_artifact(self.row['id'], 'undeclared.md')

    def test_missing_artifact_visible_but_cannot_be_downloaded(self):
        self.row['result'] = {'execution': {'artifacts': ['missing.md']}}
        info = self.manager.get_run(self.row['id'])
        self.assertEqual(info['artifacts'], [{'path': 'missing.md', 'size': None, 'exists': False}])
        with self.assertRaises(FileNotFoundError):
            self.manager.resolve_artifact(self.row['id'], 'missing.md')

    def test_private_runtime_and_credential_declarations_are_filtered_before_any_read(self):
        forbidden = ['.runtime/diagnostics.txt', '.Runtime\\vendor.log', '.ssh/id_ed25519',
                     'nested/.aws/credentials', '.env', '.env.local', '.git/config',
                     '.codex/auth.json', 'config/AUTH.JSON', 'nested/provider_config.json',
                     'credentials/key.json', 'secrets/private.pem', '.npmrc', 'id_rsa',
                     'config/service-account.json', 'nested/certificate.p12',
                     '.runtime./diagnostics.txt', '.runtime /diagnostics.txt', 'auth.json ']
        (self.workspace / '.runtime').mkdir()
        (self.workspace / '.runtime' / 'diagnostics.txt').write_text('private test fixture', encoding='utf-8')
        self.row['state'] = 'failed'
        self.row['result'] = {'status': 'failed', 'artifacts': forbidden + ['answer.md']}
        with patch('pathlib.Path.open', side_effect=AssertionError('Private artifact bodies must not be read')):
            info = self.manager.get_run(self.row['id'])
            self.assertEqual([item['path'] for item in info['artifacts']], ['answer.md'])
            for name in forbidden:
                with self.subTest(name=name), self.assertRaises(ValueError):
                    self.manager.preview(self.row['id'], name)

    def test_artifact_path_filter_preserves_normal_deliverables_and_build_outputs(self):
        for name in ('docs/authentication-guide.md', 'src/auth.py', 'build/report.html',
                     'dist/application.zip', 'output/credentials-guide.md'):
            with self.subTest(name=name):
                self.assertEqual(_artifact_name(name), name)

    def test_preview_truncates_utf8_text_and_does_not_read_known_binary(self):
        (self.workspace / 'answer.md').write_text('中' * 100000, encoding='utf-8')
        result = self.manager.preview(self.row['id'], 'answer.md')
        self.assertTrue(result['truncated'])
        self.assertFalse(result['binary'])
        self.assertLessEqual(len(result['text'].encode('utf-8')), 128 * 1024)
        self.assertNotIn('\ufffd', result['text'])
        self.assertEqual(result['size'], 300000)
        self.row['result']['artifacts'].append('report.pdf')
        (self.workspace / 'report.pdf').write_bytes(b'%PDF private body')
        with patch('pathlib.Path.open', side_effect=AssertionError('Binary preview must not read body')):
            binary = self.manager.preview(self.row['id'], 'report.pdf')
        self.assertTrue(binary['binary'])
        self.assertEqual(binary['text'], '')

    def test_preview_detects_unknown_binary(self):
        (self.workspace / 'answer.md').write_bytes(b'hello\x00secret')
        self.assertTrue(self.manager.preview(self.row['id'], 'answer.md')['binary'])

    def test_log_whitelist_redacts_secret_lines_and_bounds_tail(self):
        content = 'old\n' * 40000 + 'Authorization: Bearer private\nkey=sk-test-secret\nhello\n'
        (self.run / 'execution.events.jsonl').write_text(content, encoding='utf-8')
        result = self.manager.log(self.row['id'], 'execution', 'events')
        self.assertTrue(result['truncated'])
        self.assertLessEqual(len(result['text'].encode()), 128 * 1024)
        self.assertNotIn('private', result['text'])
        self.assertNotIn('sk-test-secret', result['text'])
        self.assertTrue(result['text'].endswith('hello\n'))
        for phase, stream in (('../execution', 'events'), ('execution', '../task'), ('task', 'stderr')):
            with self.subTest(phase=phase, stream=stream), self.assertRaises(ValueError):
                self.manager.log(self.row['id'], phase, stream)
        missing = self.manager.log(self.row['id'], 'verification', 'stderr')
        self.assertEqual(missing['text'], '')
        self.assertTrue(missing['message'])

    def test_open_opens_verified_workspace_directory_only(self):
        with patch('dispatcher.workspace.os.startfile', create=True) as startfile:
            result = self.manager.open(self.row['id'])
        startfile.assert_called_once_with(str(self.workspace))
        self.assertEqual(Path(result['workspace']), self.workspace)

    def test_symlink_escape_is_rejected_for_artifacts_logs_and_run(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'secret.md').write_text('secret')
        for run in self.history_runs():
            with self.subTest(run=run):
                workspace = run / 'workspace'
                workspace.mkdir(parents=True, exist_ok=True)
                self.row['run_dir'] = str(run)
                link = workspace / 'linked'
                try:
                    link.symlink_to(outside, target_is_directory=True)
                except OSError as exc:
                    self.skipTest(f'Symlinks unavailable: {exc}')
                self.row['result']['artifacts'] = ['linked/secret.md']
                with self.assertRaises(ValueError):
                    self.manager.resolve_artifact(self.row['id'], 'linked/secret.md')
                (run / 'execution.stderr.log').symlink_to(outside / 'secret.md')
                with self.assertRaises(ValueError):
                    self.manager.log(self.row['id'], 'execution', 'stderr')
                run_link = run.parent / 'linked-run'
                run_link.symlink_to(outside, target_is_directory=True)
                self.row['run_dir'] = str(run_link)
                with self.assertRaises(ValueError):
                    self.manager.resolve_run(self.row['id'])

    def test_invalid_run_remains_listed_with_no_accessible_files(self):
        self.row['run_dir'] = str(self.root.parent)
        info = self.manager.get_run(self.row['id'])
        self.assertIsNone(info['workspace'])
        self.assertEqual(info['artifacts'], [])
        self.assertTrue(info['workspace_error'])

    @unittest.skipUnless(os.name == 'nt', 'Windows junction behavior')
    def test_windows_junction_escape_is_rejected(self):
        import _winapi
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'secret.md').write_text('secret')
        for run in self.history_runs():
            with self.subTest(run=run):
                workspace = run / 'workspace'
                workspace.mkdir(parents=True, exist_ok=True)
                self.row['run_dir'] = str(run)
                junction = workspace / 'linked'
                _winapi.CreateJunction(str(outside), str(junction))
                self.addCleanup(lambda path=junction: path.rmdir() if path.exists() else None)
                self.row['result']['artifacts'] = ['linked/secret.md']
                with self.assertRaises(ValueError):
                    self.manager.resolve_artifact(self.row['id'], 'linked/secret.md')
                self.assertEqual(self.manager.get_run(self.row['id'])['artifacts'], [])
                self.row['run_dir'] = str(junction)
                with self.assertRaises(ValueError):
                    self.manager.resolve_run(self.row['id'])

    def test_import_old_and_executor_layouts_preserves_each_run_and_preview(self):
        from dispatcher.core import Store
        store = Store(self.root / 'queue.sqlite3')
        self.addCleanup(store.close)
        store.sync([dict(self.payload, status=0)], {})
        before = store.get('p:one')
        expected = {}
        for run, executor, text in zip(self.history_runs(), ('codex', 'zcode'), ('旧运行结果', '新运行结果')):
            workspace = run / 'workspace'
            workspace.mkdir(parents=True, exist_ok=True)
            data = text.encode('utf-8')
            digest = hashlib.sha256(data).hexdigest()
            (workspace / 'answer.md').write_bytes(data)
            self.write_json(run / 'task.json', self.payload)
            self.write_json(run / 'execution.json', {
                'status': 'complete', 'executor': executor, 'artifacts': ['answer.md'],
                'sha256': {'answer.md': digest},
            })
            expected[run.resolve()] = (executor, text, digest)

        manager = WorkspaceManager(self.root, store)
        self.assertEqual(manager.import_history(), {
            'scanned': 2, 'imported': 2, 'existing': 0, 'unknown_task': 0, 'invalid': 0,
        })
        runs = store.list_runs(task_id='p:one')
        self.assertEqual(len(runs), 2)
        self.assertEqual(len({row['id'] for row in runs}), 2)
        self.assertEqual({Path(row['run_dir']) for row in runs}, set(expected))
        for row in runs:
            with self.subTest(run=row['run_dir']):
                executor, text, digest = expected[Path(row['run_dir'])]
                self.assertEqual(row['executor'], executor)
                self.assertEqual(row['state'], 'needs_review')
                self.assertEqual(row['review_status'], 'legacy')
                self.assertEqual(row['result']['sha256'], {'answer.md': digest})
                artifact = manager.resolve_artifact(row['id'], 'answer.md')
                self.assertEqual(artifact, Path(row['run_dir']) / 'workspace' / 'answer.md')
                self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), digest)
                self.assertEqual(manager.preview(row['id'], 'answer.md')['text'], text)

        self.assertEqual(manager.import_history(), {
            'scanned': 2, 'imported': 0, 'existing': 2, 'unknown_task': 0, 'invalid': 0,
        })
        self.assertEqual(store.list_runs(task_id='p:one'), runs)
        self.assertEqual(store.get('p:one'), before)

    def test_import_with_real_store_always_marks_legacy_without_mutating_task(self):
        from dispatcher.core import Store
        store = Store(self.root / 'queue.sqlite3')
        self.addCleanup(store.close)
        store.sync([dict(self.payload, status=0)], {})
        before = store.get('p:one')
        self.write_json(self.run / 'task.json', self.payload)
        self.write_json(self.run / 'verification.json', {'status': 'complete', 'artifacts': ['answer.md']})
        manager = WorkspaceManager(self.root, store)
        self.assertEqual(manager.import_history()['imported'], 1)
        imported = store.list_runs()[0]
        self.assertEqual(imported['review_status'], 'legacy')
        self.assertEqual(imported['state'], 'done')
        self.assertEqual(store.get('p:one'), before)
        self.assertEqual(manager.import_history()['existing'], 1)

    def test_import_is_idempotent_and_keeps_current_database_result(self):
        self.write_json(self.run / 'task.json', self.payload)
        self.write_json(self.run / 'execution.json', {'status': 'complete', 'artifacts': ['answer.md']})
        self.write_json(self.run / 'routing.json', {'executor': 'dsh'})
        current = {'id': 'p:one', 'run_dir': str(self.run), 'result': {'summary': 'database'}, 'state': 'failed'}
        self.store.list_tasks.return_value = [current]
        imported = []
        self.store.list_runs.side_effect = lambda **kw: list(imported)
        def record(task_id, run_dir, payload, result, state, executor):
            imported.append({'run_dir': run_dir, 'task_id': task_id, 'payload': payload,
                             'result': result, 'state': state, 'executor': executor})
            return imported[-1]
        self.store.import_run.side_effect = record
        self.assertEqual(self.manager.import_history()['imported'], 1)
        self.assertEqual(imported[0]['result'], {'summary': 'database'})
        self.assertEqual(imported[0]['state'], 'failed')
        self.assertEqual(imported[0]['executor'], 'dsh')
        second = self.manager.import_history()
        self.assertEqual(second['imported'], 0)
        self.assertEqual(second['existing'], 1)
        self.store.import_run.assert_called_once()

    def test_history_skips_unknown_invalid_and_oversize_metadata(self):
        self.store.list_runs.return_value = []
        self.store.list_tasks.return_value = [{'id': 'p:one', 'run_dir': None}]
        self.write_json(self.run / 'task.json', {'id': 'unknown', 'projectId': 'p'})
        invalid = self.run.parent / 'invalid'
        invalid.mkdir()
        (invalid / 'task.json').write_text('not json')
        oversized = self.run.parent / 'oversized'
        oversized.mkdir()
        (oversized / 'task.json').write_text(' ' * (2 * 1024 * 1024 + 1))
        outside_pattern = self.root / 'workspaces' / 'task.json'
        self.write_json(outside_pattern, self.payload)
        counts = self.manager.import_history()
        self.assertEqual(counts['scanned'], 3)
        self.assertEqual(counts['unknown_task'], 1)
        self.assertEqual(counts['invalid'], 2)
        self.store.import_run.assert_not_called()

    def test_history_infers_completion_only_from_completed_verification(self):
        self.store.list_runs.return_value = []
        self.store.list_tasks.return_value = [{'id': 'p:one', 'run_dir': None}]
        expected = {'executed': 'needs_review', 'verified': 'done', 'failed': 'failed'}
        for name in expected:
            folder = self.run.parent / name
            self.write_json(folder / 'task.json', self.payload)
            self.write_json(folder / 'execution.json', {'status': 'failed' if name == 'failed' else 'complete'})
            if name == 'verified':
                self.write_json(folder / 'verification.json', {'status': 'complete', 'artifacts': ['answer.md']})
        counts = self.manager.import_history()
        self.assertEqual(counts['imported'], 3)
        actual = {Path(call.kwargs['run_dir']).name: call.kwargs['state'] for call in self.store.import_run.call_args_list}
        self.assertEqual(actual, expected)

    def test_recovered_history_retains_warning_and_is_not_imported_as_done(self):
        self.store.list_runs.return_value = []
        self.store.list_tasks.return_value = [{'id': 'p:one', 'run_dir': None}]
        self.write_json(self.run / 'task.json', self.payload)
        execution = {'status': 'recovered', 'executor': 'antigravity', 'vendor_status': 'ERROR',
                     'runtime_warning': {'kind': 'completed_eof'}, 'artifacts': ['answer.md']}
        self.write_json(self.run / 'execution.json', execution)
        self.write_json(self.run / 'verification.json', {'status': 'complete', 'artifacts': ['answer.md']})
        self.assertEqual(self.manager.import_history()['imported'], 1)
        imported = self.store.import_run.call_args.kwargs
        self.assertEqual(imported['state'], 'needs_review')
        self.assertEqual(imported['result']['execution'], execution)
        self.assertEqual(imported['result']['runtime_warning']['kind'], 'completed_eof')


if __name__ == '__main__':
    unittest.main()
