"""MCP runner uses simulated broker state and owned files, never native models."""
import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from dispatcher import workbuddy_runner as worker
from dispatcher.core import Store
from dispatcher.runner import MultiAgentRunner
from dispatcher.service import Service
from tests.test_service import FakeDida


def report(**changes):
    return dict({'status':'complete','summary':'written','artifacts':['result.md'],
                 'checks':['read back'],'needs_input':''},**changes)


class FakeBroker:
    def __init__(self):
        self.ready=True
        self.submissions=[]
        self.cancelled=[]
        self.on_submit=None
        self.on_result=None
        self.state={'state':'queued'}

    def status(self):return {'online':self.ready,'ready':self.ready}

    def submit(self,run_id,workspace,prompt,**kwargs):
        self.submissions.append((run_id,workspace,prompt,kwargs))
        if self.on_submit:self.on_submit(run_id,workspace)

    def result(self,run_id):
        if self.on_result:self.on_result(run_id)
        return copy.deepcopy(self.state)

    def cancel(self,run_id):
        self.cancelled.append(run_id)
        self.state={'state':'cancelled'}
        return True

    def complete(self,run_id,workspace):
        data='实际文档交付物'.encode('utf-8')
        (workspace/'result.md').write_bytes(data)
        # The real broker makes submission and capability revocation atomic.
        self.state={'state':'submitted','submission':report(),
                    'artifact_sha256':{'result.md':hashlib.sha256(data).hexdigest()}}


class WorkBuddyRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.run=self.root/'run';self.run.mkdir()
        self.workspace=self.run/'workspace';self.workspace.mkdir()
        self.broker=FakeBroker();self.broker.on_submit=self.broker.complete
        self.runner=SimpleNamespace(workbuddy_broker=self.broker,config={'task_timeout_seconds':1},
            execution_settings=Mock(return_value={}),workspace_binding=Mock(return_value={'mode':'default'}),
            workspace_path=Mock(return_value=self.workspace),prompt=Mock(return_value='编写文档'))
        self.row={'current_run_id':'run-1','priority':5}

    def execute(self):return worker.execute_workbuddy(self.runner,self.row,self.run)

    def test_submitted_real_bytes_make_review_receipt_without_approval(self):
        result=self.execute()
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['executor'],'workbuddy')
        self.assertFalse(result['resume_available'])
        self.assertNotIn('approved',result)
        self.assertEqual(result['workbuddy_receipt']['artifact_sha256'],self.broker.state['artifact_sha256'])
        self.assertTrue(result['workbuddy_receipt']['scoped_tools_revoked'])
        self.assertEqual(self.broker.cancelled,[])
        self.assertEqual(json.loads((self.run/'workbuddy-receipt.json').read_text(encoding='utf-8')),
                         result['workbuddy_receipt'])

    def test_model_statement_without_submitted_terminal_is_rejected_and_revoked(self):
        self.broker.on_result=lambda _:self.broker.state.update(state='complete')
        with self.assertRaisesRegex(ValueError,'Unknown'):self.execute()
        self.assertEqual(self.broker.cancelled,['run-1'])
        self.assertFalse((self.run/'workbuddy-receipt.json').exists())

    def test_cancellation_revokes_queued_capability(self):
        cancel=threading.Event();self.row['_cancel_event']=cancel
        self.broker.on_submit=None
        self.broker.on_result=lambda _:cancel.set()
        with patch.object(worker.time,'sleep'),self.assertRaisesRegex(worker.WorkBuddyWorkerUnavailable,'取消'):
            self.execute()
        self.assertEqual(self.broker.cancelled,['run-1'])

    def test_cancelled_before_submission_never_reaches_worker(self):
        cancel=threading.Event();cancel.set();self.row['_cancel_event']=cancel
        with self.assertRaisesRegex(worker.WorkBuddyWorkerUnavailable,'未提交'):self.execute()
        self.assertEqual(self.broker.submissions,[])

    def test_timeout_revokes_capability_and_disconnect_is_not_completion(self):
        self.broker.on_submit=None
        with patch.object(worker.time,'monotonic',side_effect=[0,2]):
            with self.assertRaisesRegex(worker.WorkBuddyWorkerUnavailable,'超时'):self.execute()
        self.assertEqual(self.broker.cancelled,['run-1'])
        self.broker.state={'state':'disconnected'}
        with self.assertRaisesRegex(worker.WorkBuddyWorkerUnavailable,'中断'):self.execute()
        self.assertFalse((self.run/'workbuddy-receipt.json').exists())

    def test_file_changed_after_submission_is_not_accepted(self):
        self.broker.on_result=lambda _:(self.workspace/'result.md').write_text('different')
        with self.assertRaisesRegex(ValueError,'changed after submission'):self.execute()
        self.assertFalse((self.run/'workbuddy-receipt.json').exists())

    def test_hardlinked_artifact_never_reaches_reader(self):
        outside=self.root/'outside.txt';outside.write_text('private')
        def replace(_):
            path=self.workspace/'result.md';path.unlink();os.link(outside,path)
        self.broker.on_result=replace
        with patch.object(worker,'_read_file',side_effect=AssertionError('must not read hardlink')):
            with self.assertRaisesRegex(ValueError,'without links'):self.execute()

    def test_replacement_between_validation_and_open_is_rejected(self):
        original=worker._read_file
        seen=[]
        def replace(path,identity,*args):
            if not seen:
                seen.append(True)
                path.unlink();path.write_bytes(b'new file')
            return original(path,identity,*args)
        with patch.object(worker,'_read_file',side_effect=replace):
            with self.assertRaises(ValueError):self.execute()

    def test_metadata_paths_count_and_size_are_bounded(self):
        variants=[report(summary='s'*(worker.MAX_TEXT_CHARS+1)),
                  report(checks=['x']*(worker.MAX_ARTIFACTS+1)),
                  report(artifacts=['result.md','RESULT.md']),
                  report(artifacts=['../outside.txt']),report(artifacts=['result.md.']),
                  report(artifacts=['NUL.txt']),report(artifacts=['.dispatcher-doubao-test.json']),
                  report(status='approved')]
        for bad in variants:
            with self.subTest(bad=str(bad)[:50]):
                self.broker.on_result=lambda _,bad=bad:self.broker.state.update(submission=bad)
                with self.assertRaises(ValueError):self.execute()
        self.broker.on_result=None
        with patch.object(worker,'MAX_ARTIFACT_BYTES',1):
            with self.assertRaisesRegex(ValueError,'bounded'):self.execute()
        with patch.object(worker,'MAX_ARTIFACT_TOTAL_BYTES',1):
            with self.assertRaisesRegex(ValueError,'total byte limit'):self.execute()
        with patch.object(worker,'MAX_REPORT_BYTES',1):
            with self.assertRaisesRegex(ValueError,'metadata limit'):self.execute()

    def test_unready_existing_and_unsupported_model_settings_never_submit(self):
        self.broker.ready=False
        with self.assertRaises(worker.WorkBuddyWorkerUnavailable):self.execute()
        self.broker.ready=True
        self.runner.workspace_binding.return_value={'mode':'existing'}
        with self.assertRaisesRegex(worker.WorkBuddyWorkerUnavailable,'独立目录'):self.execute()
        self.runner.workspace_binding.return_value={'mode':'new'}
        for settings in ({'model':'auto'},{'effort':'high'}):
            self.runner.execution_settings.return_value=settings
            with self.assertRaisesRegex(worker.WorkBuddyWorkerUnavailable,'当前模型'):self.execute()
        self.assertEqual(self.broker.submissions,[])

    def test_service_requires_independent_and_human_review_before_source_completion(self):
        service,store,dida,runner=self.service_fixture()
        with patch.object(runner,'_invoke',return_value=report()) as verifier:
            service.execute(store.claim())
        row=store.get('p:t')
        self.assertEqual(row['state'],'awaiting_review',row.get('error'))
        self.assertEqual((dida.completions,dida.comments),(0,0))
        verifier.assert_called_once()
        self.assertTrue(verifier.call_args.args[4])
        self.assertIn('实际文档交付物',verifier.call_args.args[1])
        self.assertEqual(row['result']['execution']['workbuddy_receipt']['transport'],'desktop_mcp')

    def test_service_source_completion_revokes_pending_worker(self):
        service,store,dida,runner=self.service_fixture()
        self.broker.on_submit=None
        def complete_source(_):
            dida.task['status']=2
            service.sync()
        self.broker.on_result=complete_source
        with patch.object(runner,'_invoke') as verifier,patch.object(worker.time,'sleep'):
            service.execute(store.claim())
        row=store.get('p:t')
        self.assertEqual(row['state'],'cancelled',row.get('error'))
        self.assertEqual(len(self.broker.cancelled),1)
        verifier.assert_not_called()
        self.assertEqual((dida.completions,dida.comments),(0,0))

    def service_fixture(self):
        config={'codex_command':['never-call-codex'],'task_timeout_seconds':1,
                'enabled_executors':['codex','workbuddy'],'workbuddy_transport':'mcp',
                'require_human_review':True,'auto_complete':True}
        registry={'codex':{'enabled':True,'command':['never-call-codex']},
                  'workbuddy':{'enabled':True,'command':[],'transport':'desktop_mcp'}}
        with patch('dispatcher.runner.discover_executors',return_value=registry):
            runner=MultiAgentRunner(self.root,config)
        runner.workbuddy_broker=self.broker
        store=Store(self.root/'tasks.db');self.addCleanup(store.close)
        task={'id':'t','projectId':'p','title':'编写文档','priority':5,'status':0,'tags':['AI','agent:workbuddy']}
        dida=FakeDida(task)
        service=Service(self.root,config,store,dida,runner)
        service.sync()
        return service,store,dida,runner


if __name__=='__main__':unittest.main()
