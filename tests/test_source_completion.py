"""Source completion must cancel delegation without inventing run success."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dispatcher.core import StateConflict, Store, manifest_digest, task_fingerprint


def source(**changes):
    value = {'id': 'one', 'projectId': 'p', 'title': 'Draft notes',
             'tags': ['AI'], 'status': 0}
    value.update(changes)
    return value


class SourceCompletionTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')

    def tearDown(self):
        self.store.close()

    def running(self, state='running'):
        self.store.sync([source()], {})
        row = self.store.claim()
        result = {'status': 'complete', 'artifacts': ['answer.md'],
                  'sha256': {'answer.md': 'a' * 64}, 'checks': ['read'],
                  'executor': 'codex'}
        row = self.store.mark(row['id'], 'awaiting_review',
                              expected_run_id=row['current_run_id'],
                              run_dir='runs/original', result=result)
        if state in {'approved', 'writeback_pending', 'done'}:
            row = self.store.decide_review(row['id'], row['revision'],
                row['current_run_id'], row['fingerprint'], manifest_digest(result), True)
        if row['state'] != state:
            row = self.store.mark(row['id'], state, expected_run_id=row['current_run_id'])
        return row

    def test_confirmed_completion_cancels_unstarted_work_and_pauses_delegation(self):
        for tags in ([], ['AI']):
            with self.subTest(tags=tags):
                store = Store(':memory:')
                self.addCleanup(store.close)
                store.sync([source(tags=tags)], {})
                before = store.get('p:one')
                store.sync([source(tags=tags, status=2)], {})
                after = store.get('p:one')
                self.assertEqual(after['state'], 'cancelled')
                self.assertTrue(after['paused'])
                self.assertEqual(after.get('source_status'), 2)
                self.assertTrue(after.get('source_checked_at'))
                self.assertEqual(after['payload'], before['payload'])
                self.assertEqual(after['fingerprint'], before['fingerprint'])
                self.assertIsNone(after['result'])
                self.assertIsNone(store.claim())

    def test_completed_source_preserves_every_existing_run_snapshot(self):
        for state in ('running', 'verifying', 'awaiting_review', 'needs_review',
                      'approved', 'writeback_pending', 'failed', 'needs_input'):
            with self.subTest(state=state):
                self.store.close()
                self.store = Store(':memory:')
                before = self.running(state)
                before_run = self.store.get_run(before['current_run_id'])
                self.store.sync([source(status='2', title='Changed at completion')], {})
                after = self.store.get('p:one')
                self.assertEqual(after['state'], 'cancelled')
                self.assertTrue(after['paused'])
                self.assertEqual(after.get('source_status'), 2)
                for field in ('payload', 'fingerprint', 'result', 'current_run_id',
                              'run_dir', 'attempts', 'started_at'):
                    self.assertEqual(after[field], before[field], field)
                after_run = self.store.get_run(before['current_run_id'])
                for field in ('payload', 'fingerprint', 'result', 'run_dir',
                              'execution_settings', 'review_note', 'reviewed_at'):
                    self.assertEqual(after_run[field], before_run[field], field)
                self.assertEqual(after_run['state'], 'cancelled')
                if before_run['review_status'] in {'pending', 'approved'}:
                    self.assertEqual(after_run['review_status'], 'stale')
                self.assertIsNone(self.store.claim())

    def test_repeated_completion_only_refreshes_observation_time(self):
        self.running('approved')
        self.store.sync([source(status=2)], {})
        first = self.store.get('p:one')
        first_run = self.store.get_run(first['current_run_id'])
        events = self.store.events(100)
        self.store.sync([source(status=2, etag='new-transport-value')], {})
        second = self.store.get('p:one')
        self.assertEqual(second['revision'], first['revision'])
        self.assertEqual(second['updated_at'], first['updated_at'])
        self.assertEqual(self.store.events(100), events)
        self.assertEqual(self.store.get_run(first['current_run_id']), first_run)
        self.assertTrue(second.get('source_checked_at'))

    def test_source_completion_invalidates_pending_and_approved_reviews(self):
        for state in ('awaiting_review', 'approved'):
            with self.subTest(state=state):
                self.store.close()
                self.store = Store(':memory:')
                before = self.running(state)
                self.store.sync([source(status=2)], {})
                latest = self.store.get('p:one')
                for revision in (before['revision'], latest['revision']):
                    with self.assertRaises(StateConflict):
                        self.store.decide_review(before['id'], revision, before['current_run_id'],
                            before['fingerprint'], manifest_digest(before['result']), True)
                self.assertEqual(self.store.get_run(before['current_run_id'])['review_status'], 'stale')

    def test_reopen_preserves_pause_and_cannot_revive_old_worker(self):
        before = self.running()
        self.store.sync([source(status=2)], {})
        completed = self.store.get('p:one')
        self.store.sync([source(status=0, title='Reopened with revised scope')], {})
        reopened = self.store.get('p:one')
        self.assertEqual(reopened.get('source_status'), 0)
        self.assertEqual(reopened['state'], 'cancelled')
        self.assertTrue(reopened['paused'])
        self.assertGreater(reopened['revision'], completed['revision'])
        self.assertEqual(reopened['payload'], before['payload'])
        self.assertEqual(reopened['current_run_id'], before['current_run_id'])
        self.assertIsNone(self.store.claim())
        for state in ('running', 'verifying', 'awaiting_review', 'approved',
                      'writeback_pending', 'done', 'failed', 'needs_input', 'needs_review'):
            with self.subTest(state=state), self.assertRaises(StateConflict):
                self.store.mark(before['id'], state, expected_run_id=before['current_run_id'],
                                result={'status': 'complete', 'summary': 'Late worker'})
        self.assertEqual(self.store.get('p:one'), reopened)

    def test_completed_cancelled_task_requires_fresh_explicit_assignment(self):
        before = self.running()
        self.store.sync([source(status=2)], {})
        cancelled = self.store.get('p:one')
        self.assertEqual(cancelled['state'], 'cancelled')
        with self.assertRaises(StateConflict):
            self.store.assign('p:one', 'codex', cancelled['revision'], source(status=2), {})
        with self.assertRaises(StateConflict):
            self.store.assign('p:one', 'codex', before['revision'], source(), {})
        assigned = self.store.assign('p:one', 'codex', cancelled['revision'], source(), {})
        self.assertEqual(assigned['state'], 'queued')
        self.assertEqual(assigned.get('source_status'), 0)
        self.assertFalse(assigned['paused'])
        self.assertIsNone(assigned['current_run_id'])
        self.assertIsNone(assigned['result'])
        self.assertEqual(self.store.get_run(before['current_run_id'])['result'], before['result'])
        with self.assertRaises(StateConflict):
            self.store.mark(before['id'], 'done', expected_run_id=before['current_run_id'])
        claimed = self.store.claim()
        self.assertNotEqual(claimed['current_run_id'], before['current_run_id'])

    def test_completed_observation_does_not_downgrade_successful_history(self):
        before = self.running('done')
        self.store.sync([source(status=2)], {})
        after = self.store.get('p:one')
        self.assertEqual(after['state'], 'done')
        self.assertEqual(after.get('source_status'), 2)
        self.assertFalse(after['paused'])
        self.assertEqual(after['result'], before['result'])
        self.assertEqual(after['current_run_id'], before['current_run_id'])
        self.assertEqual(self.store.get_run(before['current_run_id'])['review_status'], 'approved')

    def test_missing_source_rows_do_not_change_last_observed_status(self):
        self.store.sync([source()], {})
        before = self.store.get('p:one')
        self.assertEqual(before.get('source_status'), 0)
        self.store.sync([], {})
        self.assertEqual(self.store.get('p:one'), before)

    def test_new_completed_source_is_paused_and_never_queued_on_reopen(self):
        self.store.sync([source(status=2)], {})
        completed = self.store.get('p:one')
        self.assertEqual(completed['state'], 'cancelled')
        self.assertTrue(completed['paused'])
        self.store.sync([source()], {})
        self.assertEqual(self.store.get('p:one')['state'], 'cancelled')
        self.assertIsNone(self.store.claim())

    def test_ordinary_withdrawal_remains_assignable(self):
        self.store.sync([source()], {})
        row = self.store.get('p:one')
        withdrawn = self.store.withdraw(row['id'], row['revision'])
        self.assertEqual(withdrawn['state'], 'ignored')
        assigned = self.store.assign(row['id'], 'codex', withdrawn['revision'], source(), {})
        self.assertEqual(assigned['state'], 'queued')

    def test_withdraw_cannot_revive_a_cancelled_run_after_source_reopens(self):
        before = self.running()
        self.store.sync([source(status=2)], {})
        self.store.sync([source()], {})
        reopened = self.store.get('p:one')
        withdrawn = self.store.withdraw(reopened['id'], reopened['revision'])
        self.assertEqual(withdrawn['state'], 'cancelled')
        self.assertTrue(withdrawn['paused'])
        with self.assertRaises(StateConflict):
            self.store.mark(before['id'], 'awaiting_review', expected_run_id=before['current_run_id'])

    def test_legacy_schema_backfills_source_status_once_without_changing_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.sqlite3'
            with sqlite3.connect(path) as connection:
                connection.execute('''CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    difficulty TEXT NOT NULL, kind TEXT NOT NULL, repo TEXT, reason TEXT NOT NULL,
                    state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, run_dir TEXT, result TEXT,
                    error TEXT, first_seen TEXT NOT NULL, updated_at TEXT NOT NULL, priority INTEGER NOT NULL,
                    due_date TEXT, started_at TEXT, queued_at TEXT NOT NULL)''')
                connection.execute('''INSERT INTO tasks (id, payload, fingerprint, difficulty, kind,
                    reason, state, run_dir, result, first_seen, updated_at, priority, queued_at)
                    VALUES (?, ?, ?, 'easy', 'document', '', 'done', 'runs/old', ?, 'old', 'old', 0, 'old')''',
                    ('p:one', json.dumps(source(status='2')), task_fingerprint(source(status='2')),
                     json.dumps({'executor': 'codex', 'summary': 'Original result'})))
            connection.close()
            for _ in range(2):
                legacy = Store(path)
                try:
                    row = legacy.get('p:one')
                    self.assertEqual(row.get('source_status'), 2)
                    self.assertIsNone(row.get('source_checked_at'))
                    self.assertEqual(row['state'], 'done')
                    self.assertEqual(row['revision'], 0)
                    self.assertEqual(row['result']['summary'], 'Original result')
                    self.assertEqual(len(legacy.list_runs()), 1)
                finally:
                    legacy.close()


if __name__ == '__main__':
    unittest.main()
