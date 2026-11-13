"""Durable manual controls, review binding, run history, and command claims."""

import concurrent.futures
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dispatcher.core import StateConflict, Store, manifest_digest, task_fingerprint


def source(**changes):
    value = {'id': 'one', 'projectId': 'p', 'title': 'Draft notes', 'tags': [], 'status': 0}
    value.update(changes)
    return value


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'queue.sqlite3'
        self.store = Store(self.path)
        self.store.sync([source()], {})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def assign(self, executor='codex', fresh=None, **kwargs):
        row = self.store.get('p:one')
        return self.store.assign('p:one', executor, row['revision'], fresh or source(), {}, **kwargs)

    def await_review(self):
        self.assign()
        row = self.store.claim()
        result = {'status': 'complete', 'sha256': {'answer.md': 'a' * 64}, 'executor': 'codex'}
        return self.store.mark('p:one', 'awaiting_review', expected_run_id=row['current_run_id'],
                               run_dir='runs/first', result=result)

    def decide(self, row, approve, **changes):
        values = dict(task_id='p:one', expected_revision=row['revision'], run_id=row['current_run_id'],
                      fingerprint=row['fingerprint'], manifest_digest=manifest_digest(row['result']),
                      approve=approve, note='Please revise')
        values.update(changes)
        return self.store.decide_review(**values)

    def test_manual_control_survives_sync_and_pause_prevents_queue(self):
        row = self.assign()
        self.assertEqual(row['manual_executor'], 'codex')
        self.assertTrue(row['delegated'])
        self.store.sync([source(etag='transport')], {})
        unchanged = self.store.get('p:one')
        self.assertEqual(unchanged['revision'], row['revision'])
        self.assertEqual(unchanged['state'], 'queued')
        paused = self.store.withdraw('p:one', row['revision'])
        self.store.sync([source(title='Changed', tags=['AI'])], {})
        self.assertTrue(self.store.get('p:one')['paused'])
        self.assertEqual(self.store.get('p:one')['state'], 'ignored')
        self.assertIsNone(self.store.claim())
        self.assertGreater(paused['revision'], row['revision'])

    def test_assign_requires_fresh_cas_identity_and_known_repo(self):
        original = self.store.get('p:one')
        assigned = self.assign('auto')
        self.assertIsNone(assigned['manual_executor'])
        with self.assertRaises(StateConflict):
            self.store.assign('p:one', 'codex', original['revision'], source(), {})
        with self.assertRaises(StateConflict):
            self.store.assign('p:one', 'codex', None, source(), {})
        with self.assertRaises(ValueError):
            self.store.assign('p:one', 'codex', assigned['revision'], source(id='other'), {})
        with self.assertRaises(ValueError):
            self.store.assign('p:one', 'codex', assigned['revision'], source(), {}, repo_alias='D:/arbitrary')
        row = self.store.assign('p:one', 'codex', assigned['revision'], source(),
                                {'repositories': {'demo': 'D:/repos/demo'}}, repo_alias='demo')
        self.assertEqual(row['repo'], 'D:/repos/demo')
        self.assertEqual(row['repo_alias'], 'demo')

    def test_claims_create_one_unique_run_per_task_under_contention(self):
        self.store.sync([source(id=str(i), tags=['AI']) for i in range(16)], {})
        def claims(_):
            store = Store(self.path)
            found = []
            while (row := store.claim()) is not None:
                found.append(row)
            return found
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            rows = [row for group in pool.map(claims, range(4)) for row in group]
        self.assertEqual(len(rows), 16)
        self.assertEqual(len({row['current_run_id'] for row in rows}), 16)
        self.assertEqual(len(self.store.list_runs()), 16)

    def test_review_binds_revision_run_fingerprint_and_manifest(self):
        row = self.await_review()
        self.assertEqual(self.store.get_run(row['current_run_id'])['review_status'], 'pending')
        for change in ({'expected_revision': row['revision'] - 1}, {'run_id': 'wrong'},
                       {'fingerprint': 'wrong'}, {'manifest_digest': 'wrong'}):
            with self.subTest(change=change), self.assertRaises(StateConflict):
                self.decide(row, True, **change)
        approved = self.decide(row, True)
        self.assertEqual(approved['state'], 'approved')
        self.assertEqual(self.store.get_run(row['current_run_id'])['review_status'], 'approved')
        with self.assertRaises(StateConflict):
            self.decide(approved, True)

    def test_manifest_digest_canonical_and_rejects_missing_or_invalid_hashes(self):
        first = {'sha256': {'b': 'B' * 64, 'a': 'a' * 64}}
        second = {'sha256': {'a': 'a' * 64, 'b': 'b' * 64}}
        self.assertEqual(manifest_digest(first), manifest_digest(second))
        for result in (None, {}, {'sha256': {}}, {'sha256': {'a': 'bad'}}, {'sha256': []}):
            self.assertIsNone(manifest_digest(result))

    def test_rejection_preserves_history_feedback_and_never_auto_requeues(self):
        row = self.await_review()
        with self.assertRaises(ValueError):
            self.decide(row, False, note=' ')
        rejected = self.decide(row, False)
        self.assertEqual(rejected['state'], 'revision_requested')
        self.assertTrue(rejected['paused'])
        self.store.sync([source(tags=['AI'], title='Updated')], {})
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.get('p:one')['feedback'], 'Please revise')
        self.assign(fresh=source(tags=['AI'], title='Updated'))
        next_run = self.store.claim()
        self.assertNotEqual(next_run['current_run_id'], row['current_run_id'])
        self.assertEqual(len(self.store.list_runs('p:one')), 2)
        self.assertEqual(self.store.get_run(row['current_run_id'])['result'], row['result'])

    def test_changed_source_invalidates_pending_review_without_replacing_snapshot(self):
        row = self.await_review()
        self.store.sync([source(title='Source changed')], {})
        stale = self.store.get('p:one')
        self.assertEqual(stale['state'], 'needs_review')
        self.assertEqual(stale['payload'], row['payload'])
        self.assertEqual(stale['fingerprint'], row['fingerprint'])
        self.assertTrue(stale['error'])
        with self.assertRaises(StateConflict):
            self.decide(stale, True)

    def test_run_scoped_mark_rejects_old_run_and_latest_result_is_saved(self):
        row = self.await_review()
        with self.assertRaises(StateConflict):
            self.store.mark('p:one', 'failed', expected_run_id='older')
        updated = self.store.mark('p:one', 'needs_review', expected_run_id=row['current_run_id'], result={'status': 'failed'})
        self.assertEqual(self.store.get_run(row['current_run_id'])['result'], updated['result'])

    def test_approval_cannot_follow_a_changed_manifest_or_source(self):
        row = self.await_review()
        approved = self.decide(row, True)
        changed = dict(approved['result'], sha256={'answer.md': 'b' * 64})
        with self.assertRaises(StateConflict):
            self.store.mark('p:one', 'writeback_pending', result=changed)
        self.store.sync([source(title='Edited after approval')], {})
        stale = self.store.get('p:one')
        self.assertEqual(stale['state'], 'needs_review')
        self.assertEqual(stale['payload'], approved['payload'])
        self.assertEqual(self.store.get_run(row['current_run_id'])['review_status'], 'stale')

    def test_reassigned_stale_run_can_be_archived(self):
        row = self.await_review()
        self.store.sync([source(title='New source')], {})
        self.assign(fresh=source(title='New source'))
        current = self.store.get('p:one')
        self.store.archive_run(row['current_run_id'])
        self.assertEqual(self.store.get('p:one'), current)

    def test_completed_command_uses_ui_terminal_status(self):
        row = self.store.submit_command('sync', {}, 'ui-status')
        self.store.next_command()
        self.assertEqual(self.store.finish_command(row['id'], 'done')['status'], 'done')

    def test_legacy_schema_migration_is_reentrant_and_preserves_history(self):
        path = Path(self.temp.name) / 'legacy.sqlite3'
        with sqlite3.connect(path) as connection:
            connection.execute('''CREATE TABLE tasks (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
                difficulty TEXT NOT NULL, kind TEXT NOT NULL, repo TEXT, reason TEXT NOT NULL,
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, run_dir TEXT, result TEXT,
                error TEXT, first_seen TEXT NOT NULL, updated_at TEXT NOT NULL, priority INTEGER NOT NULL,
                due_date TEXT, started_at TEXT, queued_at TEXT NOT NULL)''')
            connection.execute('''INSERT INTO tasks (id, payload, fingerprint, difficulty, kind, reason,
                state, run_dir, result, first_seen, updated_at, priority, queued_at)
                VALUES (?, ?, ?, 'easy', 'document', '', 'done', 'runs/old', ?, 'old', 'old', 0, 'old')''',
                ('p:one', json.dumps(source()), task_fingerprint(source()), json.dumps({'executor': 'codex'})))
        connection.close()
        for _ in range(2):
            legacy = Store(path)
            row = legacy.get('p:one')
            self.assertEqual(row['revision'], 0)
            self.assertTrue(row['current_run_id'])
            self.assertEqual(len(legacy.list_runs()), 1)
            self.assertEqual(legacy.get_run(row['current_run_id'])['review_status'], 'legacy')
            legacy.close()

    def test_archive_is_metadata_only_and_rejects_active_review_runs(self):
        row = self.await_review()
        with self.assertRaises(StateConflict):
            self.store.archive_run(row['current_run_id'])
        rejected = self.decide(row, False)
        self.store.archive_run(row['current_run_id'])
        self.assertEqual(self.store.get('p:one'), rejected)
        self.assertEqual(self.store.list_runs(include_archived=False), [])
        self.store.archive_run(row['current_run_id'], False)
        self.assertEqual(len(self.store.list_runs(include_archived=False)), 1)

    def test_import_is_idempotent_and_legacy_done_never_claims_manual_approval(self):
        result = {'status': 'complete'}
        one = self.store.import_run('p:one', 'runs/legacy', source(), result, 'done', 'codex')
        two = self.store.import_run('p:one', 'runs/legacy', source(), result, 'done', 'codex')
        self.assertEqual(one['id'], two['id'])
        self.assertEqual(one['review_status'], 'legacy')
        self.assertIsNone(self.store.get('p:one')['current_run_id'])

    def test_command_idempotency_claim_finish_and_crash_recovery(self):
        one = self.store.submit_command('assign', {'task_id': 'p:one'}, 'unique')
        same = self.store.submit_command('assign', {'task_id': 'p:one'}, 'unique')
        self.assertEqual(one['id'], same['id'])
        with self.assertRaises(StateConflict):
            self.store.submit_command('withdraw', {'task_id': 'p:one'}, 'unique')
        claimed = self.store.next_command()
        self.assertEqual(claimed['status'], 'processing')
        self.assertIsNone(self.store.next_command())
        self.store.finish_command(one['id'], 'completed', result={'ok': True})
        self.assertEqual(self.store.get_command(one['id'])['result'], {'ok': True})
        two = self.store.submit_command('sync', {}, 'second')
        self.store.next_command()
        self.assertEqual(self.store.recover_commands(), 1)
        interrupted = self.store.get_command(two['id'])
        self.assertEqual(interrupted['status'], 'failed')
        self.assertTrue(interrupted['error'])
        self.assertIsNone(self.store.next_command())

    def test_three_human_revision_cycles_start_new_attempt_budget_but_retain_daily_count(self):
        from dispatcher.service import Service
        from tests.test_service import FakeDida, FakeRunner
        runner = FakeRunner(self.temp.name)
        service = Service(self.temp.name, {'max_attempts': 1}, self.store, FakeDida(source()), runner)
        self.assign()
        run_ids = []
        for cycle in range(4):
            claimed = self.store.claim()
            service.execute(claimed)
            row = self.store.get('p:one')
            self.assertEqual(row['state'], 'awaiting_review')
            self.assertEqual(row['attempts'], 1)
            self.assertEqual(runner.executions, cycle + 1)
            self.assertEqual(self.store.count_started_today(), cycle + 1)
            run_ids.append(row['current_run_id'])
            if cycle < 3:
                self.decide(row, False, note=f'Revision {cycle + 1}')
                self.assign()
        self.assertEqual(len(set(run_ids)), 4)
        self.assertEqual(len(self.store.list_runs('p:one')), 4)


if __name__ == '__main__':
    unittest.main()
