"""Reusing a live directory must preserve previous reviewed bytes and records."""

import copy
import hashlib
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from dispatcher.core import StateConflict, Store, manifest_digest
from dispatcher.workspace import WorkspaceManager


class WorkspaceArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'data' / 'queue.sqlite3')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)
        self.manager = WorkspaceManager(self.root, self.store)
        self.payload = {'id': 'one', 'projectId': 'p', 'title': 'Reviewed answer', 'tags': [], 'status': 0}
        self.store.sync([self.payload], {})
        self.run = self.root / 'workspaces' / 'codex' / 'history--task' / 'runs' / 'one'
        self.live = self.run / 'workspace'
        self.live.mkdir(parents=True)
        self.original = b'original reviewed answer\n'
        (self.live / 'answer.md').write_bytes(self.original)
        self.binding = {'mode': 'default', 'workspace': str(self.live)}
        self.result = {'status': 'complete', 'artifacts': ['answer.md'],
                       'sha256': {'answer.md': hashlib.sha256(self.original).hexdigest()},
                       'workspace_binding': self.binding}

    def approved_run(self):
        current = self.store.get('p:one')
        self.store.assign('p:one', 'codex', current['revision'], self.payload, {})
        claimed = self.store.claim()
        identifier = claimed['current_run_id']
        row = self.store.mark('p:one', 'awaiting_review', expected_run_id=identifier,
                              run_dir=str(self.run), result=copy.deepcopy(self.result))
        self.store.decide_review('p:one', row['revision'], identifier, row['fingerprint'],
                                manifest_digest(self.result), True)
        return self.store.get_run(identifier)

    def historical_run(self, state='done', result=None):
        return self.store.import_run(task_id='p:one', run_dir=str(self.run), payload=self.payload,
            result=copy.deepcopy(self.result if result is None else result), state=state, executor='codex')

    def archive(self, path=None):
        method = getattr(self.manager, 'archive_for_reuse', None)
        self.assertTrue(callable(method), 'Historical workspace preservation is not implemented')
        return method(self.live if path is None else path)

    def freeze(self, run_id, path, expected=None):
        method = getattr(self.store, 'freeze_artifact_root', None)
        self.assertTrue(callable(method), 'Artifact root freezing is not implemented')
        return method(run_id, path, str(self.run) if expected is None else expected)

    def test_approved_history_keeps_original_bytes_and_all_review_fields_after_live_reuse(self):
        before = self.approved_run()
        task_before = self.store.get('p:one')
        receipts = self.archive()
        after = self.store.get_run(before['id'])
        frozen = Path(after['artifact_root'])
        self.assertTrue(frozen.is_relative_to(self.run / 'artifact-archive'))
        self.assertFalse(frozen.is_relative_to(self.live))
        self.assertEqual(receipts[0]['run_id'], before['id'])
        self.assertEqual({k: v for k, v in after.items() if k != 'artifact_root'},
                         {k: v for k, v in before.items() if k != 'artifact_root'})
        self.assertEqual(self.store.get('p:one'), task_before)
        (self.live / 'answer.md').write_text('next task changed the live file', encoding='utf-8')
        self.assertEqual(self.manager.preview(before['id'], 'answer.md')['text'], self.original.decode())
        self.assertEqual(self.manager.resolve_artifact(before['id'], 'answer.md').read_bytes(), self.original)
        self.assertEqual(Path(self.manager.get_run(before['id'])['workspace']), frozen)
        with patch('dispatcher.workspace.os.startfile', create=True) as startfile:
            self.manager.open(before['id'])
        startfile.assert_called_once_with(str(frozen))

    def test_changed_approved_manifest_prevents_reuse_without_changing_run_or_live_bytes(self):
        before = self.approved_run()
        (self.live / 'answer.md').write_text('already changed', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.archive()
        self.assertEqual(self.store.get_run(before['id']), before)
        self.assertEqual((self.live / 'answer.md').read_text(), 'already changed')

    def test_busy_history_prevents_copy_and_freeze(self):
        for state in ('running', 'verifying'):
            with self.subTest(state=state):
                with self.store._transaction() as connection:
                    connection.execute('DELETE FROM runs')
                row = self.historical_run(state)
                with self.assertRaises(ValueError):
                    self.archive()
                self.assertFalse((self.run / 'artifact-archive').exists())
                destination = self.run / 'manual-snapshot'
                destination.mkdir(exist_ok=True)
                with self.assertRaises((ValueError, StateConflict)):
                    self.freeze(row['id'], destination)

    def test_archive_is_idempotent_and_hidden_history_is_preserved(self):
        row = self.historical_run()
        self.store.archive_run(row['id'])
        first = self.archive()
        frozen = self.store.get_run(row['id'])['artifact_root']
        self.assertEqual(len(first), 1)
        self.assertEqual(self.archive(), [])
        self.assertEqual(self.store.get_run(row['id'])['artifact_root'], frozen)
        self.assertEqual(len(list((self.run / 'artifact-archive').iterdir())), 1)
        (self.live / 'answer.md').write_bytes(b'changed while hidden')
        self.store.archive_run(row['id'], False)
        self.assertEqual(self.manager.preview(row['id'], 'answer.md')['text'], self.original.decode())

    def test_parent_and_child_selections_are_not_treated_as_exact_history_matches(self):
        row = self.historical_run()
        child = self.live / 'nested'
        child.mkdir()
        self.assertEqual(self.archive(self.run), [])
        self.assertEqual(self.archive(child), [])
        self.assertIsNone(self.store.get_run(row['id']).get('artifact_root'))
        self.assertFalse((self.run / 'artifact-archive').exists())

    def test_frozen_snapshot_itself_cannot_be_reused_in_place(self):
        row = self.historical_run()
        self.archive()
        root = self.store.get_run(row['id'])['artifact_root']
        with self.assertRaises(ValueError):
            self.archive(root)
        self.assertEqual(self.store.get_run(row['id'])['artifact_root'], root)

    def test_freeze_checks_expected_run_and_cannot_change_an_existing_binding(self):
        row = self.historical_run()
        first, second = self.run / 'snapshot-one', self.run / 'snapshot-two'
        first.mkdir()
        second.mkdir()
        with self.assertRaises((ValueError, StateConflict)):
            self.freeze(row['id'], first, expected=str(self.run.parent / 'different'))
        saved = self.freeze(row['id'], first)
        self.assertEqual(saved['artifact_root'], str(first.resolve()))
        self.assertEqual(self.freeze(row['id'], first), saved)
        with self.assertRaises((ValueError, StateConflict)):
            self.freeze(row['id'], second)
        self.assertEqual(self.store.get_run(row['id']), saved)

    def test_freeze_rejects_relative_outside_live_and_live_descendant_roots(self):
        row = self.historical_run()
        child = self.live / 'child'
        child.mkdir()
        for value in ('relative', self.root, self.run, self.live, child):
            with self.subTest(value=value), self.assertRaises((ValueError, StateConflict)):
                self.freeze(row['id'], value)
        self.assertEqual(self.store.get_run(row['id']), row)

    def test_invalid_nonnull_artifact_root_cannot_fall_back_to_mutable_live_files(self):
        row = self.historical_run()
        row['artifact_root'] = ''
        with self.assertRaises(ValueError):
            self.manager._workspace(row)

    def test_migration_adds_nullable_artifact_root_and_keeps_choices_and_bindings(self):
        row = self.approved_run()
        self.store.close()
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            columns = {value[1] for value in connection.execute('PRAGMA table_info(runs)')}
            if 'artifact_root' in columns:
                connection.execute('ALTER TABLE runs DROP COLUMN artifact_root')
        self.store = Store(self.store.path)
        self.addCleanup(self.store.close)
        migrated = self.store.get_run(row['id'])
        self.assertIn('artifact_root', migrated)
        self.assertIsNone(migrated['artifact_root'])
        self.assertEqual(migrated['workspace_binding'], self.binding)
        self.assertEqual(self.store.get('p:one')['workspace_choice'], {'mode': 'default'})

    def test_declared_file_excluded_by_copy_policy_cannot_be_silently_dropped(self):
        (self.live / '.env').write_text('test fixture only', encoding='utf-8')
        self.result['artifacts'].append('.env')
        self.result['sha256']['.env'] = hashlib.sha256((self.live / '.env').read_bytes()).hexdigest()
        row = self.historical_run()
        with self.assertRaises(ValueError):
            self.archive()
        self.assertIsNone(self.store.get_run(row['id']).get('artifact_root'))

    @unittest.skipUnless(os.name == 'nt', 'Windows junction behavior')
    def test_archive_roots_and_live_aliases_reject_junctions_even_within_the_run(self):
        import _winapi
        row = self.historical_run()
        alias = self.run / 'live-alias'
        _winapi.CreateJunction(str(self.live), str(alias))
        self.addCleanup(lambda: alias.rmdir() if alias.exists() else None)
        with self.assertRaises(ValueError):
            self.archive(alias)
        target = self.run / 'real-snapshot'
        target.mkdir()
        link = self.run / 'linked-snapshot'
        _winapi.CreateJunction(str(target), str(link))
        self.addCleanup(lambda: link.rmdir() if link.exists() else None)
        with self.assertRaises(ValueError):
            self.freeze(row['id'], link)
        with self.store._transaction() as connection:
            connection.execute('UPDATE runs SET artifact_root=? WHERE id=?', (str(link), row['id']))
        with self.assertRaises(ValueError):
            self.manager._workspace(self.store.get_run(row['id']))


if __name__ == '__main__':
    unittest.main()
