"""Resume targets live paths; index artifact links target frozen snapshots."""
import contextlib
import importlib
import inspect
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dispatcher.core import Store
from dispatcher.index import write_index


class WorkspaceNavigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.live = self.root / 'project'
        self.live.mkdir()
        self.run = self.root / 'workspaces' / 'codex' / 'task' / 'runs' / 'one'
        self.snapshot = self.run / 'workspace'
        self.snapshot.mkdir(parents=True)
        self.store = Store(self.root / 'data' / 'tasks.db')
        self.addCleanup(self.store.close)
        self.task = {'id': 't', 'projectId': 'p', 'title': 'Continue project', 'tags': ['AI'], 'status': 0}
        self.store.sync([self.task])
        self.run_id = self.store.claim()['current_run_id']
        self.binding = {'version': 1, 'mode': 'existing', 'strategy': 'in_place',
                        'workspace_path': str(self.live), 'snapshot_path': str(self.snapshot),
                        'name': 'project', 'source': {}}
        self.cli = importlib.import_module('dispatcher.__main__')

    def finish(self, binding=None):
        result = {'status': 'complete', 'artifacts': ['answer.md'],
                  'execution': {'thread_id': 'live-session', 'executor': 'codex'},
                  'workspace_binding': self.binding if binding is None else binding}
        return self.store.mark('p:t', 'done', expected_run_id=self.run_id,
                               run_dir=str(self.run), result=result)

    def resume(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return self.cli.main(['--root', str(self.root), 'resume', 'p:t'])

    def test_resume_uses_frozen_run_live_binding_not_snapshot(self):
        self.finish()
        (self.run / 'workspace-binding.json').write_text(json.dumps(dict(self.binding, workspace_path='wrong')))
        with patch.object(self.cli, 'codex_command', return_value=['codex']), \
                patch.object(self.cli.subprocess, 'call', return_value=0) as call:
            self.assertEqual(self.resume(), 0)
        call.assert_called_once_with(['codex', 'resume', 'live-session'], cwd=self.live)

    def test_missing_or_nonlocal_live_directory_does_not_fall_back_to_snapshot(self):
        self.finish(dict(self.binding, workspace_path=str(self.root / 'missing')))
        with patch.object(self.cli.subprocess, 'call') as call:
            self.assertEqual(self.resume(), 1)
            call.assert_not_called()

    def test_vendor_resume_displays_frozen_live_directory(self):
        result = {'status': 'complete', 'workspace_binding': self.binding,
                  'execution': {'executor': 'dsh', 'session_id': 'vendor-session'}}
        self.store.mark('p:t', 'done', expected_run_id=self.run_id, run_dir=str(self.run), result=result)
        (self.run / 'execution-resume.txt').write_text('Vendor session instructions', encoding='utf-8')
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(self.cli.subprocess, 'call') as call:
            self.assertEqual(self.cli.main(['--root', str(self.root), 'resume', 'p:t']), 0)
        self.assertIn(str(self.live), output.getvalue())
        call.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows junction behavior')
    def test_resume_rejects_reparse_live_directory(self):
        import _winapi
        junction = self.root / 'linked'
        _winapi.CreateJunction(str(self.live), str(junction))
        self.addCleanup(lambda: junction.rmdir() if junction.exists() else None)
        self.finish(dict(self.binding, workspace_path=str(junction)))
        with patch.object(self.cli.subprocess, 'call') as call:
            self.assertEqual(self.resume(), 1)
            call.assert_not_called()

    def test_index_distinguishes_live_and_effective_snapshot(self):
        row = self.finish()
        archive = self.run / 'verified-artifacts' / 'saved'
        archive.mkdir(parents=True)
        (archive / 'answer.md').write_text('reviewed bytes')
        self.store.freeze_artifact_root(self.run_id, str(archive), str(self.run))
        self.assertIn('store', inspect.signature(write_index).parameters)
        write_index(self.root, [row], store=self.store)
        text = (self.root / 'workspaces' / 'INDEX.md').read_text(encoding='utf-8')
        self.assertIn('执行工作目录：[打开](' + self.live.as_posix() + ')', text)
        self.assertIn('验收快照：[打开](' + archive.as_posix() + ')', text)
        self.assertIn((archive / 'answer.md').as_posix(), text)
        self.assertNotIn((self.live / 'answer.md').as_posix(), text)
        self.assertNotIn('原始项目不会被直接覆盖', text)

    def test_index_never_promotes_result_only_binding_to_trusted_live_path(self):
        row = {'id': 'legacy', 'state': 'done', 'payload': {'title': 'Legacy'},
               'run_dir': str(self.run), 'result': {'workspace_binding': self.binding, 'artifacts': []}}
        write_index(self.root, [row])
        text = (self.root / 'workspaces' / 'INDEX.md').read_text(encoding='utf-8')
        self.assertNotIn(self.live.as_posix(), text)
        self.assertIn(self.snapshot.as_posix(), text)


if __name__ == '__main__':
    unittest.main()
