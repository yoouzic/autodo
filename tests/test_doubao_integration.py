"""Scheduler integration with simulated native receipts, never a real provider."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from dispatcher import doubao, workspace_guard
from dispatcher.core import Store
from dispatcher.runner import MultiAgentRunner
from dispatcher.service import Service
from dispatcher.workspace_lock import WorkspaceLease


class FakeDida:
    def __init__(self,task):
        self.task=task.copy()
        self.completions=0
    def get(self,task):return self.task.copy()
    def fetch_tasks(self):return [self.task.copy()],[]
    def complete(self,task):self.completions+=1
    def ensure_comment(self,*args):raise AssertionError('Human review is required')


class DoubaoIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.exe=self.root/'doubao.exe';self.exe.write_bytes(b'never executed')
        self.config={'codex_command':['codex-never-run'], 'task_timeout_seconds':60,
                     'require_human_review':True, 'auto_complete':True, 'daily_max_runs':10}
        registry={'doubao':{'enabled':True,'command':[str(self.exe)]}}
        with patch('dispatcher.runner.discover_executors',return_value=registry):
            self.runner=MultiAgentRunner(self.root,self.config)
        self.store=Store(self.root/'test.db')
        self.task={'id':'t','projectId':'p','title':'编写借阅提醒方案',
                   'tags':['AI','agent:doubao'],'priority':5,'status':0}
        self.dida=FakeDida(self.task)
        self.service=Service(self.root,self.config,self.store,self.dida,self.runner)
        self.store.sync([self.task],self.config)
        self.process=Mock(side_effect=self.success)
        self.verification=Mock(side_effect=self.verify)
        for target,value in (
                ('dispatcher.runner.doubao_process.run_doubao_process',self.process),
                ('dispatcher.runner.run_process',Mock(side_effect=AssertionError('Generic runner forbidden'))),
                ('dispatcher.runner.sandbox_command',Mock(side_effect=AssertionError('Wrapper forbidden')))):
            patcher=patch(target,value);patcher.start();self.addCleanup(patcher.stop)
        patcher=patch.object(self.runner,'_invoke',self.verification)
        patcher.start();self.addCleanup(patcher.stop)

    def receipt(self,**changes):
        result={'version':1,'started':True,'client_pid':123,'client_exited':True,'exit_code':0,
                'timed_out':False,'cancelled':False,'parent_lost':False,'output_limit':False,
                'supervisor_error':False,'force_terminated':False,'requires_quarantine':False,
                'remote_stop_confirmed':False,'stdout':'private vendor text','stderr':''}
        result.update(changes)
        return result

    def manifest(self,run_dir,cwd,**changes):
        binding=doubao.read_binding(run_dir)
        (cwd/'result.md').write_text('借阅到期提醒架构和验证步骤。',encoding='utf-8')
        result={'run_id':binding['run_id'],'nonce':binding['nonce'],'status':'complete',
                'summary':'written','artifacts':['result.md'],'checks':['read back result.md'],
                'needs_input':''}
        result.update(changes)
        (cwd/binding['manifest_name']).write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')

    def success(self,argv,*,cwd,run_dir,**kwargs):
        self.assertEqual(argv[0],str(self.exe))
        self.assertEqual(argv[argv.index('--permission-mode')+1],'ask_always')
        guards=workspace_guard.active_guards(self.root)
        self.assertEqual(len(guards),1)
        self.assertEqual(guards[0]['status'],'running')
        self.assertFalse(kwargs['cancel_event'].is_set())
        self.manifest(run_dir,cwd)
        return self.receipt()

    def verify(self,run_dir,prompt,phase,effort,readonly=False,**kwargs):
        self.assertEqual(phase,'verification')
        self.assertTrue(readonly)
        self.assertIn('借阅到期提醒架构和验证步骤',prompt)
        return {'status':'complete','summary':'independently checked','artifacts':['result.md'],
                'checks':['checked actual contents'],'needs_input':''}

    def execute(self):
        self.service.execute(self.store.claim())
        return self.store.get('p:t')

    def assert_quarantined(self,row,reason):
        self.assertEqual(row['state'],'needs_input')
        guards=workspace_guard.active_guards(self.root)
        self.assertEqual(len(guards),1)
        self.assertEqual(guards[0]['status'],'quarantined')
        self.assertEqual(guards[0]['reason'],reason)
        self.verification.assert_not_called()
        self.assertEqual(self.dida.completions,0)
        # The process-owned lease is released even though durable quarantine stays.
        with WorkspaceLease(self.root,Path(guards[0]['workspace']),'test-lease'):
            pass

    def test_success_uses_native_receipt_then_independent_and_human_review(self):
        row=self.execute()
        self.assertEqual(row['state'],'awaiting_review')
        self.assertEqual(self.dida.completions,0)
        self.assertEqual(workspace_guard.active_guards(self.root),[])
        self.verification.assert_called_once()
        execution=row['result']['execution']
        self.assertEqual(execution['executor'],'doubao')
        self.assertFalse(execution['resume_available'])
        self.assertNotIn('stdout',execution['process_receipt'])
        self.assertEqual(execution['doubao_receipt']['file_count'],1)
        self.assertEqual(json.loads((Path(row['run_dir'])/'execution-command.json').read_text(encoding='utf-8'))['argv'][0],str(self.exe))

    def test_missing_manifest_quarantines(self):
        self.process.side_effect=lambda *args,**kwargs:self.receipt()
        self.assert_quarantined(self.execute(),'manifest_invalid')

    def test_wrong_nonce_quarantines(self):
        def run(*args,cwd,run_dir,**kwargs):
            self.manifest(run_dir,cwd,nonce='0'*64)
            return self.receipt()
        self.process.side_effect=run
        self.assert_quarantined(self.execute(),'manifest_invalid')

    def test_modified_host_binding_is_not_trusted(self):
        def run(*args,cwd,run_dir,**kwargs):
            self.manifest(run_dir,cwd)
            path=run_dir/doubao.BINDING_NAME
            binding=json.loads(path.read_text(encoding='utf-8'));binding['nonce']='0'*64
            path.write_text(json.dumps(binding),encoding='utf-8')
            return self.receipt()
        self.process.side_effect=run
        self.assert_quarantined(self.execute(),'manifest_invalid')

    def test_timeout_cannot_accept_existing_complete_manifest(self):
        def run(*args,cwd,run_dir,**kwargs):
            self.manifest(run_dir,cwd)
            return self.receipt(timed_out=True,requires_quarantine=True)
        self.process.side_effect=run
        self.assert_quarantined(self.execute(),'timeout')

    def test_cancel_cannot_accept_existing_complete_manifest(self):
        def run(*args,cwd,run_dir,**kwargs):
            self.manifest(run_dir,cwd)
            return self.receipt(cancelled=True,requires_quarantine=True)
        self.process.side_effect=run
        self.assert_quarantined(self.execute(),'cancelled')

    def test_supervisor_failure_is_safe_diagnostic_and_quarantine(self):
        self.process.side_effect=RuntimeError('private vendor output must not leak')
        row=self.execute()
        self.assert_quarantined(row,'execution_exception')
        self.assertNotIn('private vendor',row['error'])

    def test_bad_receipt_cannot_accept_valid_manifest(self):
        def run(*args,cwd,run_dir,**kwargs):
            self.manifest(run_dir,cwd)
            return self.receipt(client_exited=False)
        self.process.side_effect=run
        self.assert_quarantined(self.execute(),'client_failed')

    def test_unconfirmed_login_failure_retains_quarantine(self):
        self.process.side_effect=lambda *args,**kwargs:self.receipt(exit_code=4,stderr='未登录',requires_quarantine=True)
        self.assert_quarantined(self.execute(),'client_failed')

    def test_never_started_client_releases_guard_but_never_verifies(self):
        self.process.side_effect=lambda *args,**kwargs:self.receipt(started=False,client_pid=None,
            client_exited=False,exit_code=None,supervisor_error=True)
        row=self.execute()
        self.assertEqual(row['state'],'needs_input')
        self.assertEqual(workspace_guard.active_guards(self.root),[])
        self.verification.assert_not_called()
        records=list((self.root/'data/workspace-guards').glob('*.json'))
        self.assertEqual(json.loads(records[0].read_text(encoding='utf-8'))['reason'],'not_started')

    def test_manifest_needs_input_does_not_release_or_verify(self):
        def run(*args,cwd,run_dir,**kwargs):
            self.manifest(run_dir,cwd,status='needs_input',needs_input='Need material')
            return self.receipt()
        self.process.side_effect=run
        self.assert_quarantined(self.execute(),'client_failed')

    def test_source_completion_cancels_live_process_without_reviving_run(self):
        def run(*args,cwd,run_dir,cancel_event,**kwargs):
            self.dida.task['status']=2
            self.service.sync()
            self.assertTrue(cancel_event.is_set())
            return self.receipt(exit_code=130,cancelled=True,requires_quarantine=True)
        self.process.side_effect=run
        row=self.execute()
        self.assertEqual(row['state'],'cancelled')
        self.assertTrue(row['paused'])
        self.assertEqual(workspace_guard.active_guards(self.root)[0]['reason'],'cancelled')
        self.verification.assert_not_called()

    def test_unsynced_source_completion_before_verification_is_rechecked(self):
        def run(*args,**kwargs):
            receipt=self.success(*args,**kwargs)
            self.dida.task['status']=2
            return receipt
        self.process.side_effect=run
        row=self.execute()
        self.assertEqual(row['state'],'cancelled')
        self.verification.assert_not_called()
        self.assertEqual(self.dida.completions,0)

    def test_source_completion_during_verification_does_not_offer_review(self):
        def verify(*args,**kwargs):
            result=self.verify(*args,**kwargs)
            self.dida.task['status']=2
            return result
        self.verification.side_effect=verify
        row=self.execute()
        self.assertEqual(row['state'],'cancelled')
        self.assertEqual(self.dida.completions,0)

    def test_invalid_command_never_starts_client_or_creates_guard(self):
        self.runner.registry['doubao']['command'].append('--permission-mode=always_allow')
        row=self.execute()
        self.assertEqual(row['state'],'failed')
        self.process.assert_not_called()
        self.assertEqual(workspace_guard.active_guards(self.root),[])

    def test_new_original_workspace_uses_separate_runtime_and_review_snapshot(self):
        row=self.store.claim()
        row['workspace_choice']={'mode':'new','name':'借阅项目'}
        self.service.execute(row)
        saved=self.store.get(row['id'])
        self.assertEqual(saved['state'],'awaiting_review')
        run=Path(saved['run_dir']);binding=doubao.read_binding(run)
        self.assertEqual(binding['runtime'],str(run/'runtime'))
        self.assertNotEqual(Path(binding['workspace']),run/'workspace')
        self.assertTrue((run/'workspace/result.md').is_file())
        self.assertFalse((run/'workspace'/binding['manifest_name']).exists())
        self.assertEqual(workspace_guard.active_guards(self.root),[])

    def test_stop_before_start_releases_without_running_or_verifying(self):
        self.service.stop.set()
        def run(*args,cancel_event,**kwargs):
            self.assertTrue(cancel_event.is_set())
            return self.receipt(started=False,client_pid=None,client_exited=False,
                                exit_code=None,cancelled=True)
        self.process.side_effect=run
        row=self.execute()
        self.assertEqual(row['state'],'needs_input')
        self.verification.assert_not_called()
        self.assertEqual(workspace_guard.active_guards(self.root),[])


if __name__=='__main__':unittest.main()
