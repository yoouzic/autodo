"""Durable model defaults, per-dispatch overrides and immutable run snapshots."""
import concurrent.futures
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing

from dispatcher.core import StateConflict, Store


def source():
    return {'id': 'one', 'projectId': 'p', 'title': 'Draft notes', 'tags': [], 'status': 0}


class AgentSettingsStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'queue.sqlite3'
        self.store = Store(self.path)
        self.store.sync([source()], {})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def assign(self, executor='codex', **kwargs):
        row = self.store.get('p:one')
        return self.store.assign('p:one', executor, row['revision'], source(), {}, **kwargs)

    def test_defaults_are_versioned_persisted_and_resettable(self):
        self.assertTrue(callable(getattr(self.store, 'get_agent_settings', None)),
                        'Store must expose durable agent defaults')
        self.assertEqual(self.store.get_agent_settings('codex'),
                         {'executor': 'codex', 'settings': {}, 'revision': 0})
        changed = self.store.set_agent_settings('codex', {'model': 'gpt-6.1-sol', 'effort': 'high'}, 0)
        self.assertEqual(changed['revision'], 1)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.get_agent_settings('codex'), changed)
        reset = self.store.set_agent_settings('codex', {}, changed['revision'])
        self.assertEqual(reset['settings'], {})
        self.assertEqual(reset['revision'], 2)
        self.assertEqual(self.store.get_agent_settings('dsh')['revision'], 0)

    def test_default_conflicts_do_not_replace_saved_values(self):
        saved = self.store.set_agent_settings('codex', {'model': 'model-a'}, 0)
        for revision in (0, True, None, '1', -1):
            with self.subTest(revision=revision), self.assertRaises((ValueError, StateConflict)):
                self.store.set_agent_settings('codex', {'model': 'model-b'}, revision)
        self.assertEqual(self.store.get_agent_settings('codex'), saved)

    def test_default_validation_is_shared_with_assignments(self):
        for settings in ([], 'model', {'extra': 'x'}, {'model': ''}, {'effort': ' '},
                         {'model': True}, {'effort': 3}, {'model': None}):
            with self.subTest(settings=settings):
                with self.assertRaises(ValueError):
                    self.store.set_agent_settings('codex', settings, 0)
                with self.assertRaises(ValueError):
                    self.assign(execution_overrides=settings)
        for name in ('unknown', '', None, True):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.store.get_agent_settings(name)
                with self.assertRaises(ValueError):
                    self.store.set_agent_settings(name, {}, 0)

    def test_only_one_competing_default_write_can_commit(self):
        def change(model):
            store = Store(self.path)
            try:
                return store.set_agent_settings('qoder', {'model': model}, 0)
            except StateConflict:
                return None
            finally:
                store.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(change, ['model-a', 'model-b']))
        winners = [r for r in results if r is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.store.get_agent_settings('qoder'), winners[0])

    def test_session_overrides_survive_sync_and_claim_but_new_assignment_resets(self):
        settings = {'model': 'gpt-6.1-sol', 'effort': 'high'}
        assigned = self.assign(execution_overrides=settings)
        self.assertEqual(assigned['execution_overrides'], settings)
        settings['model'] = 'outside-mutation'
        self.store.sync([source()], {})
        self.assertEqual(self.store.get('p:one')['execution_overrides']['model'], 'gpt-6.1-sol')
        claimed = self.store.claim()
        self.assertEqual(claimed['execution_overrides'], {'model': 'gpt-6.1-sol', 'effort': 'high'})
        self.store.mark('p:one', 'failed', expected_run_id=claimed['current_run_id'])
        reassigned = self.assign()
        self.assertEqual(reassigned['execution_overrides'], {})
        self.assertEqual(self.store.claim()['execution_overrides'], {})

    def test_auto_routing_rejects_explicit_settings_without_mutation(self):
        original = self.store.get('p:one')
        with self.assertRaises(ValueError):
            self.assign('auto', execution_overrides={'model': 'gpt-6.1-sol'})
        self.assertEqual(self.store.get('p:one'), original)
        self.assertEqual(self.assign('auto', execution_overrides={})['execution_overrides'], {})

    def test_run_settings_are_immutable_and_independent_from_later_defaults(self):
        self.assign(execution_overrides={'model': 'model-a'})
        claimed = self.store.claim()
        run_id = claimed['current_run_id']
        frozen = {'executor': 'codex', 'model': 'model-a', 'effort': 'high',
                  'sources': {'model': 'session', 'effort': 'difficulty'}}
        self.store.mark('p:one', 'running', expected_run_id=run_id,
                        result={'executor': 'codex', 'execution_settings': frozen})
        self.store.set_agent_settings('codex', {'model': 'model-b'}, 0)
        self.store.mark('p:one', 'verifying', expected_run_id=run_id,
                        result={'status': 'complete', 'execution': {'execution_settings': frozen}})
        before = self.store.get('p:one')
        with self.assertRaises(StateConflict):
            self.store.mark('p:one', 'failed', expected_run_id=run_id,
                            result={'execution_settings': dict(frozen, model='model-b')})
        self.assertEqual(self.store.get('p:one'), before)
        self.store.mark('p:one', 'failed', expected_run_id=run_id, result={'status': 'failed'})
        self.assertEqual(self.store.get_run(run_id)['execution_settings'], frozen)
        self.assign()
        self.assertEqual(self.store.get_run(run_id)['execution_settings'], frozen)
        self.assertEqual(self.store.list_runs('p:one')[0]['execution_settings'], frozen)
        self.assertIsNone(self.store.get_run(self.store.claim()['current_run_id'])['execution_settings'])

    def test_first_snapshot_can_come_from_nested_execution(self):
        self.assign()
        run_id = self.store.claim()['current_run_id']
        frozen = {'executor': 'dsh', 'model': 'deepseek-chat', 'effort': None}
        self.store.mark('p:one', 'awaiting_review', expected_run_id=run_id,
                        result={'execution': {'execution_settings': frozen}})
        self.assertEqual(self.store.get_run(run_id)['execution_settings'], frozen)

    def test_invalid_or_conflicting_snapshots_are_not_persisted(self):
        self.assign()
        run_id = self.store.claim()['current_run_id']
        for result in ({'execution_settings': None}, {'execution_settings': []},
                       {'execution_settings': {'model': 'a'},
                        'execution': {'execution_settings': {'model': 'b'}}}):
            with self.subTest(result=result), self.assertRaises((ValueError, StateConflict)):
                self.store.mark('p:one', 'verifying', expected_run_id=run_id, result=result)
        self.assertIsNone(self.store.get_run(run_id)['execution_settings'])

    def test_migration_preserves_old_controls_and_leaves_history_unknown(self):
        self.assign('zcode')
        run_id = self.store.claim()['current_run_id']
        self.store.mark('p:one', 'failed', expected_run_id=run_id, result={'status': 'failed'})
        self.store.close()
        with closing(sqlite3.connect(self.path)) as db:
            task_columns = {r[1] for r in db.execute('PRAGMA table_info(task_controls)')}
            if 'execution_overrides' in task_columns:
                db.execute('ALTER TABLE task_controls DROP COLUMN execution_overrides')
            run_columns = {r[1] for r in db.execute('PRAGMA table_info(runs)')}
            if 'execution_settings' in run_columns:
                db.execute('ALTER TABLE runs DROP COLUMN execution_settings')
            db.execute('DROP TABLE IF EXISTS agent_settings')
            db.commit()
        self.store = Store(self.path)
        task = self.store.get('p:one')
        self.assertEqual(task['manual_executor'], 'zcode')
        self.assertEqual(task['execution_overrides'], {})
        self.assertIsNone(self.store.get_run(run_id)['execution_settings'])
        self.assertEqual(self.store.get_agent_settings('zcode')['revision'], 0)


if __name__ == '__main__':
    unittest.main()
