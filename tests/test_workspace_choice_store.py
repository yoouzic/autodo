"""Workspace choices and immutable run bindings, without accessing real workspaces."""
import inspect
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from dispatcher import control_store
from dispatcher.core import StateConflict, Store, manifest_digest


def source(**changes):
    value = {'id': 'one', 'projectId': 'p', 'title': 'Draft notes', 'tags': [], 'status': 0}
    value.update(changes)
    return value


class WorkspaceChoiceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'queue.sqlite3'
        self.store = Store(self.path)
        self.store.sync([source()], {})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def assign(self, **kwargs):
        if 'workspace_choice' in kwargs:
            self.assertIn('workspace_choice', inspect.signature(self.store.assign).parameters,
                          'Assignments must accept an explicit workspace choice')
        row = self.store.get('p:one')
        return self.store.assign('p:one', 'codex', row['revision'], source(), {}, **kwargs)

    def claim(self):
        self.assign()
        return self.store.claim()['current_run_id']

    def test_unconfigured_and_legacy_assignments_use_default_choice(self):
        self.assertEqual(self.store.get('p:one').get('workspace_choice'), {'mode': 'default'})
        self.assertEqual(self.assign().get('workspace_choice'), {'mode': 'default'})

    def test_choices_persist_and_reassignment_replaces_or_resets_them(self):
        choice = {'mode': 'existing', 'workspace_id': 'ws_' + 'a1' * 12}
        self.assertEqual(self.assign(workspace_choice=choice).get('workspace_choice'), choice)
        choice['workspace_id'] = 'outside-mutation'
        self.store.sync([source(etag='changed transport')], {})
        self.store.close()
        self.store = Store(self.path)
        expected = {'mode': 'existing', 'workspace_id': 'ws_' + 'a1' * 12}
        self.assertEqual(self.store.list_tasks()[0]['workspace_choice'], expected)
        claimed = self.store.claim()
        self.assertEqual(claimed['workspace_choice'], expected)
        self.store.mark('p:one', 'failed', expected_run_id=claimed['current_run_id'])
        self.assertEqual(self.assign(workspace_choice={'mode': 'new', 'name': '研究 notes'})['workspace_choice'],
                         {'mode': 'new', 'name': '研究 notes'})
        self.assertEqual(self.assign()['workspace_choice'], {'mode': 'default'})
        self.assertEqual(self.assign(workspace_choice=None)['workspace_choice'], {'mode': 'default'})

    def test_validator_accepts_only_the_three_supported_shapes(self):
        validate = getattr(control_store, 'validate_workspace_choice', None)
        self.assertTrue(callable(validate), 'Workspace choice validation must be reusable by the controller')
        for choice in ({'mode': 'default'}, {'mode': 'new', 'name': '目录' * 32},
                       {'mode': 'existing', 'workspace_id': 'ws_' + '0123456789abcdef01234567'}):
            with self.subTest(choice=choice):
                self.assertEqual(validate(choice), choice)
                self.assertIsNot(validate(choice), choice)

    def test_invalid_choices_are_rejected_without_mutating_the_task(self):
        self.assign(workspace_choice={'mode': 'new', 'name': 'keep'})
        before = self.store.get('p:one')
        choices = [[], 'default', True, {}, {'mode': 'unknown'}, {'mode': None},
                   {'mode': 'default', 'path': 'D:/elsewhere'},
                   {'mode': 'default', 'name': 'extra'}, {'mode': 'new'},
                   {'mode': 'new', 'name': 'ok', 'path': 'extra'},
                   {'mode': 'existing'}, {'mode': 'existing', 'workspace_id': None},
                   {'mode': 'existing', 'workspace_id': 'ws_' + 'a' * 23},
                   {'mode': 'existing', 'workspace_id': 'ws_' + 'g' * 24},
                   {'mode': 'existing', 'workspace_id': 'D:/existing'},
                   {'mode': 'existing', 'workspace_id': 'ws_' + 'a' * 24, 'name': 'extra'}]
        names = ['', ' ', '.', '..', 'a' * 65, '../outside', '..\\outside', 'a/b', 'a\\b',
                 '/absolute', 'C:drive', 'a:stream', '\\\\server', 'bad<name', 'bad>name',
                 'bad"name', 'bad|name', 'bad?name', 'bad*name', 'trailing.', 'trailing ',
                 ' leading', 'line\nbreak', 'null\x00byte', 'format\u202ename', '\ud800',
                 'CON', 'con.txt', 'NUL', 'AUX.log', 'PRN', 'COM1', 'lpt9.txt', 'CONIN$',
                 'CONOUT$.log', 'COM¹', 'ＣＯＮ', '．．']
        choices.extend({'mode': 'new', 'name': name} for name in names)
        choices.extend({'mode': 'new', 'name': value} for value in (None, True, 12, []))
        for choice in choices:
            with self.subTest(choice=choice), self.assertRaises(ValueError):
                self.assign(workspace_choice=choice)
            self.assertEqual(self.store.get('p:one'), before)

    def test_stale_assignment_cannot_replace_the_choice(self):
        old = self.store.get('p:one')
        assigned = self.assign(workspace_choice={'mode': 'new', 'name': 'keep'})
        with self.assertRaises(StateConflict):
            self.store.assign('p:one', 'codex', old['revision'], source(), {},
                              workspace_choice={'mode': 'default'})
        self.assertEqual(self.store.get('p:one'), assigned)

    def test_run_binding_freezes_and_missing_results_do_not_erase_it(self):
        run_id = self.claim()
        binding = {'mode': 'existing', 'workspace_id': 'ws_' + 'a' * 24,
                   'workspace': 'D:/managed/project', 'version': 1}
        self.store.mark('p:one', 'running', expected_run_id=run_id, result={'workspace_binding': binding})
        self.assertEqual(self.store.get_run(run_id).get('workspace_binding'), binding)
        self.store.mark('p:one', 'verifying', expected_run_id=run_id,
                        result={'execution': {'workspace_binding': dict(binding)}})
        before = self.store.get('p:one')
        with self.assertRaises(StateConflict):
            self.store.mark('p:one', 'failed', expected_run_id=run_id,
                            result={'workspace_binding': dict(binding, workspace='D:/changed')})
        self.assertEqual(self.store.get('p:one'), before)
        self.store.mark('p:one', 'failed', expected_run_id=run_id, result={'status': 'failed'})
        self.assertEqual(self.store.get_run(run_id)['workspace_binding'], binding)
        self.assign()
        self.assertEqual(self.store.list_runs('p:one')[0]['workspace_binding'], binding)
        self.assertIsNone(self.store.get_run(self.store.claim()['current_run_id'])['workspace_binding'])

    def test_first_binding_can_be_nested_alongside_execution_settings(self):
        run_id = self.claim()
        binding = {'mode': 'default', 'workspace': 'D:/managed/run/workspace'}
        settings = {'executor': 'codex', 'model': 'example', 'effort': 'high'}
        self.store.mark('p:one', 'awaiting_review', expected_run_id=run_id,
                        result={'execution': {'workspace_binding': binding, 'execution_settings': settings}})
        run = self.store.get_run(run_id)
        self.assertEqual(run.get('workspace_binding'), binding)
        self.assertEqual(run['execution_settings'], settings)

    def test_binding_freeze_distinguishes_json_types_but_ignores_key_order(self):
        run_id = self.claim()
        binding = {'version': 1, 'mode': 'default'}
        self.store.mark('p:one', 'running', expected_run_id=run_id,
                        result={'workspace_binding': binding})
        self.assertEqual(self.store.get_run(run_id).get('workspace_binding'), binding)
        self.store.mark('p:one', 'verifying', expected_run_id=run_id,
                        result={'workspace_binding': {'mode': 'default', 'version': 1}})
        for result in ({'workspace_binding': {'version': True, 'mode': 'default'}},
                       {'workspace_binding': binding,
                        'execution': {'workspace_binding': {'version': True, 'mode': 'default'}}}):
            with self.subTest(result=result), self.assertRaises(StateConflict):
                self.store.mark('p:one', 'verifying', expected_run_id=run_id, result=result)

    def test_invalid_or_conflicting_bindings_rollback_the_state_change(self):
        run_id = self.claim()
        before = self.store.get('p:one')
        for result in ({'workspace_binding': None}, {'workspace_binding': []},
                       {'workspace_binding': {'workspace': 'a'},
                        'execution': {'workspace_binding': {'workspace': 'b'}}}):
            with self.subTest(result=result), self.assertRaises((ValueError, StateConflict)):
                self.store.mark('p:one', 'verifying', expected_run_id=run_id, result=result)
            self.assertEqual(self.store.get('p:one'), before)
        self.assertIsNone(self.store.get_run(run_id).get('workspace_binding'))

    def test_binding_does_not_weaken_approval_or_source_cancellation(self):
        run_id = self.claim()
        binding = {'mode': 'default', 'workspace': 'D:/managed/run/workspace'}
        result = {'status': 'complete', 'sha256': {'answer.md': 'a' * 64}, 'workspace_binding': binding}
        row = self.store.mark('p:one', 'awaiting_review', expected_run_id=run_id, result=result)
        self.assertEqual(self.store.get_run(run_id).get('workspace_binding'), binding)
        self.store.decide_review('p:one', row['revision'], run_id, row['fingerprint'],
                                 manifest_digest(result), True)
        for changed in (dict(result, sha256={'answer.md': 'b' * 64}),
                        dict(result, workspace_binding=dict(binding, workspace='D:/changed'))):
            with self.assertRaises(StateConflict):
                self.store.mark('p:one', 'writeback_pending', expected_run_id=run_id, result=changed)
        self.store.sync([source(status=2)], {})
        self.assertEqual(self.store.get('p:one')['state'], 'cancelled')
        self.assertEqual(self.store.get_run(run_id)['review_status'], 'stale')
        self.assertEqual(self.store.get_run(run_id)['workspace_binding'], binding)
        with self.assertRaises(StateConflict):
            self.store.mark('p:one', 'done', expected_run_id=run_id, result=result)

    def test_migration_defaults_old_choices_and_keeps_history_unbound(self):
        self.assign()
        run_id = self.store.claim()['current_run_id']
        self.store.mark('p:one', 'failed', expected_run_id=run_id, result={'status': 'failed'})
        self.store.close()
        with closing(sqlite3.connect(self.path)) as database:
            if 'workspace_choice' in {r[1] for r in database.execute('PRAGMA table_info(task_controls)')}:
                database.execute('ALTER TABLE task_controls DROP COLUMN workspace_choice')
            if 'workspace_binding' in {r[1] for r in database.execute('PRAGMA table_info(runs)')}:
                database.execute('ALTER TABLE runs DROP COLUMN workspace_binding')
            database.commit()
        self.store = Store(self.path)
        self.assertEqual(self.store.get('p:one').get('workspace_choice'), {'mode': 'default'})
        run = self.store.get_run(run_id)
        self.assertIn('workspace_binding', run)
        self.assertIsNone(run['workspace_binding'])
        self.assertEqual(run['state'], 'failed')


if __name__ == '__main__':
    unittest.main()
