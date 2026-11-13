import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from dispatcher.runner import CodexRunner, MultiAgentRunner


class MultiRunnerTests(unittest.TestCase):
    def test_verifier_gets_actual_artifact_bytes_not_just_worker_claims(self):
        from dispatcher.runner import CodexRunner
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'workspace').mkdir()
            (root/'workspace/result.md').write_text('ACTUAL_FILE_CONTENT',encoding='utf-8')
            r=CodexRunner(root,{'codex_command':['codex']})
            report={'status':'complete','summary':'worker claim','artifacts':['result.md'],'checks':['written'],'needs_input':''}
            with patch.object(r,'_invoke',return_value=report.copy()) as invoke:
                r.verify({'payload':{'title':'read document'}},root,report)
            self.assertIn('ACTUAL_FILE_CONTENT',invoke.call_args.args[1])
    def test_codex_command_override_is_used_for_all_invocations(self):
        with tempfile.TemporaryDirectory() as d:
            r=MultiAgentRunner(d,{'codex_command':['old-codex'],'executor_commands':{'codex':['new-codex']}})
            self.assertEqual(r.command,['new-codex'])
    def test_selected_vendor_is_recorded_and_not_mislabelled_codex(self):
        with tempfile.TemporaryDirectory() as d, ExitStack() as leases:
            root=Path(d)
            registry={name:{'enabled':True,'command':[name]} for name in ['codex','dsh','zcode']}
            with patch('dispatcher.runner.discover_executors',return_value=registry):
                r=MultiAgentRunner(root,{'codex_command':['codex']})
            row={'payload':{'id':'t','projectId':'p','tags':['AI'],'title':'Write a summary'},
                 'fingerprint':'abcdef00','difficulty':'easy','kind':'document','repo':None}
            run=r.prepare(row)
            leases.callback(r.release_workspace,row)
            self.assertEqual(run.parents[2], root.resolve()/'workspaces'/'dsh')
            self.assertTrue(run.parent.parent.name.startswith('Write a summary--'))
            self.assertEqual(json.loads((run/'routing.json').read_text(encoding='utf-8'))['executor'],'dsh')
            report={'status':'complete','summary':'done','artifacts':['r.md'],'checks':['exists'],'needs_input':'','session_id':'dsh-1','executor':'dsh'}
            with patch.object(r,'_invoke_external',return_value=report):
                result=r.execute(row,run)
            self.assertEqual(result['executor'],'dsh')
            self.assertEqual(result['route']['executor'],'dsh')
            self.assertNotIn('thread_id',result)

    def test_explicit_disabled_vendor_does_not_create_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with patch('dispatcher.runner.discover_executors',return_value={'codex':{'enabled':True,'command':['c']},'dsh':{'enabled':False,'command':[]}}):
                r=MultiAgentRunner(root,{'codex_command':['c']})
            row={'payload':{'tags':['agent:dsh']},'difficulty':'easy','kind':'document'}
            with self.assertRaises(ValueError): r.prepare(row)
            self.assertFalse((root/'workspaces').exists())

    def test_codex_prepare_records_layout_and_keeps_original_payload(self):
        with tempfile.TemporaryDirectory() as d, ExitStack() as leases:
            root = Path(d)
            runner = CodexRunner(root, {'codex_command': ['codex']})
            task = {'projectId': 'p', 'id': 'one', 'title': '借阅提醒', 'content': '原始内容'}
            row = {'payload': task, 'fingerprint': 'abcdef00'}
            run = runner.prepare(row)
            leases.callback(runner.release_workspace,row)
            task_hash = hashlib.sha256(b'p:one').hexdigest()[:10]
            self.assertEqual(run.parents[2], root.resolve()/'workspaces'/'codex')
            self.assertEqual(run.parent.parent.name, f'借阅提醒--{task_hash}')
            self.assertTrue((run/'workspace').is_dir())
            self.assertTrue((run/'result.schema.json').is_file())
            self.assertEqual(json.loads((run/'task.json').read_text(encoding='utf-8')), task)
            self.assertEqual(json.loads((run/'workspace-layout.json').read_text(encoding='utf-8')), {
                'version': 2, 'executor': 'codex', 'summary': '借阅提醒',
                'task_key_hash': task_hash, 'task_directory': f'codex/借阅提醒--{task_hash}',
            })

    def test_task_reruns_reuse_task_directory_when_fingerprint_changes(self):
        with tempfile.TemporaryDirectory() as d, ExitStack() as leases:
            runner = CodexRunner(d, {'codex_command': ['codex']})
            task = {'projectId': 'p', 'id': 'one', 'title': '借阅提醒'}
            times = [datetime(2026, 11, 7, 1, 2, 3, microsecond, timezone.utc)
                     for microsecond in (1, 2)]
            with patch('dispatcher.runner.datetime') as clock:
                clock.now.side_effect = times
                first_row = {'payload': task, 'fingerprint': 'aaaaaaaa'}
                second_row = {'payload': task, 'fingerprint': 'bbbbbbbb'}
                first = runner.prepare(first_row)
                leases.callback(runner.release_workspace,first_row)
                second = runner.prepare(second_row)
                leases.callback(runner.release_workspace,second_row)
            self.assertEqual(first.parent, second.parent)
            self.assertNotEqual(first, second)
            self.assertEqual(first.name, '20261107T010203000001Z')
            self.assertEqual(second.name, '20261107T010203000002Z')

    def test_run_directory_collision_preserves_existing_files(self):
        with tempfile.TemporaryDirectory() as d, ExitStack() as leases:
            runner = CodexRunner(d, {'codex_command': ['codex']})
            row = {'payload': {'projectId': 'p', 'id': 'one', 'title': '借阅提醒'},
                   'fingerprint': 'aaaaaaaa'}
            with patch('dispatcher.runner.datetime') as clock:
                clock.now.return_value = datetime(2026, 11, 7, 1, 2, 3, 1, timezone.utc)
                first = runner.prepare(row)
                leases.callback(runner.release_workspace,row)
                (first/'workspace'/'keep.txt').write_bytes(b'keep')
                runner.release_workspace(row)
                with self.assertRaises(FileExistsError):
                    runner.prepare(row)
            self.assertEqual((first/'workspace'/'keep.txt').read_bytes(), b'keep')


if __name__=='__main__': unittest.main()
