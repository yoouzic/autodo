"""Projected and executed routes use the same per-task capabilities."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dispatcher.control import Controller
from dispatcher.core import Store
from dispatcher.runner import CodexRunner, MultiAgentRunner
from dispatcher.service import Service
from dispatcher.workspace import WorkspaceManager
from tests.test_service import FakeDida, FakeRunner


class RoutingProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'queue.sqlite3')
        self.config = {'repositories': {'demo': str(self.root / 'demo')}, 'codex_command': ['codex']}
        self.source = {'id': 't', 'projectId': 'p', 'title': 'Fix a Python bug', 'tags': ['repo:demo'], 'status': 0}
        self.dida = FakeDida(self.source.copy())
        self.service = Service(self.root, self.config, self.store, self.dida, FakeRunner(self.root))
        self.registry = {'qoder': {'enabled': True}, 'codex': {'enabled': True}}
        self.control = Controller(self.service, WorkspaceManager(self.root, self.store),
                                  [dict(spec, name=name) for name, spec in self.registry.items()])
        self.service.sync()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_unassigned_backlog_only_has_recommendation(self):
        task = self.control.task(self.store.get('p:t'))
        self.assertEqual(task['stage'], 'backlog')
        self.assertIsNone(task['executor'])
        self.assertEqual(task['recommended_executor'], 'codex')

    def test_queued_projection_matches_real_prepare_capability_filter(self):
        row = self.store.get('p:t')
        row = self.store.assign(row['id'], 'auto', row['revision'], self.source, self.config)
        projected = self.control.task(row)
        with patch('dispatcher.runner.discover_executors', return_value=self.registry):
            runner = MultiAgentRunner(self.root, self.config)
        run = self.root / 'mock-run'
        run.mkdir()
        with patch.object(CodexRunner, 'prepare', return_value=run):
            runner.prepare(row)
        actual = json.loads((run / 'routing.json').read_text(encoding='utf-8'))
        self.assertEqual(projected['executor'], 'codex')
        self.assertEqual(projected['executor'], actual['executor'])
        self.assertEqual(self.control.state()['agents'][0]['queued'], 1)

    def test_manual_qoder_without_repository_checks_fails_at_submission(self):
        row = self.store.get('p:t')
        with self.assertRaisesRegex(ValueError, 'repository checks'):
            self.control.submit({'action': 'assign', 'request_id': 'qoder-no-checks', 'task_id': row['id'],
                                 'revision': row['revision'], 'executor': 'qoder'})
        self.assertIsNone(self.store.next_command())

    def test_checked_repo_alias_allows_manual_qoder(self):
        self.config['repository_checks'] = {'demo': {'commands': [['python', '-m', 'unittest']]}}
        row = self.store.get('p:t')
        submitted = self.control.submit({'action': 'assign', 'request_id': 'qoder-checked', 'task_id': row['id'],
                                        'revision': row['revision'], 'executor': 'qoder', 'repo_alias': 'demo'})
        self.control.process_pending()
        self.assertEqual(self.store.get_command(submitted['id'])['status'], 'done')
        self.assertEqual(self.control.task(self.store.get(row['id']))['executor'], 'qoder')

    def test_fresh_source_capability_is_rechecked_before_assignment(self):
        self.source['title'] = 'Write notes'
        self.source['tags'] = []
        self.dida.task = self.source.copy()
        self.service.sync()
        row = self.store.get('p:t')
        submitted = self.control.submit({'action': 'assign', 'request_id': 'qoder-source-change', 'task_id': row['id'],
                                        'revision': row['revision'], 'executor': 'qoder'})
        self.dida.task['title'] = 'Fix a Python bug'
        self.control.process_pending()
        self.assertEqual(self.store.get_command(submitted['id'])['status'], 'failed')
        self.assertEqual(self.store.get(row['id'])['state'], 'ignored')


if __name__ == '__main__':
    unittest.main()
