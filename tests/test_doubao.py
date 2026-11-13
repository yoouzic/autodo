"""Isolated Doubao invocation/manifest checks; no CLI or model is started."""
import copy
import hashlib
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


class DoubaoAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve()
        self.workspace=self.base/'original-project';self.workspace.mkdir()
        self.run=self.base/'run';self.run.mkdir()
        self.runtime=self.run/'runtime'
        self.exe=self.base/'doubao.exe';self.exe.write_bytes(b'not executed')
        self.run_id='test-run-1'
        self.nonce='9a'*32

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('dispatcher.doubao'))
        from dispatcher import doubao
        return doubao

    def build(self,**changes):
        values=dict(command=[str(self.exe)],workspace=self.workspace,run_dir=self.run,
                    prompt='Create result.md and read it back.',settings={},runtime=self.runtime,
                    run_id=self.run_id,nonce=self.nonce)
        values.update(changes)
        return self.module().build_invocation(**values)

    def prepared(self):
        invocation=self.build()
        return invocation,self.module().read_binding(self.run)

    def manifest(self,binding,**changes):
        value={'run_id':binding['run_id'],'nonce':binding['nonce'],'status':'complete',
               'summary':'done','artifacts':['result.md'],'checks':['read back result.md'],'needs_input':''}
        value.update(changes)
        (self.workspace/'result.md').write_text('ACTUAL RESULT',encoding='utf-8')
        path=self.workspace/binding['manifest_name']
        path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
        return path,value

    def test_invocation_only_uses_fresh_scoped_ask_always_and_bound_manifest(self):
        (argv,env,stdin,roots),binding=self.prepared()
        self.assertEqual(argv[0],str(self.exe))
        self.assertEqual(argv[argv.index('-C')+1],str(self.workspace))
        self.assertEqual(argv[argv.index('--permission-mode')+1],'ask_always')
        delegated_prompt=argv[argv.index('-p')+1]
        self.assertIn('Create result.md',delegated_prompt)
        self.assertIn(self.nonce,delegated_prompt)
        self.assertIn(self.run_id,delegated_prompt)
        self.assertIn(binding['manifest_name'],delegated_prompt)
        self.assertIsNone(stdin)
        self.assertEqual(env,{'CI':'1'})
        self.assertEqual(roots,[])  # Does not pretend to sandbox a shared service.
        self.assertFalse((self.workspace/binding['manifest_name']).exists())
        self.assertFalse(any(flag in argv for flag in ('--resume','--service-start','--list',
            '--dangerously-bypass-approvals-and-sandbox','always_allow','ask_risky')))

    def test_default_workspace_runtime_and_model_effort_are_explicit_arguments(self):
        workspace=self.run/'workspace';workspace.mkdir()
        argv,_,_,_=self.build(workspace=workspace,runtime=workspace/'.runtime',
            settings={'cli_model':'model-1','effort':'low'})
        self.assertEqual(argv[argv.index('--model')+1],'model-1')
        self.assertEqual(argv[argv.index('--effort')+1],'low')

    def test_bad_commands_paths_and_parameters_rejected_before_metadata_write(self):
        cases=[{'command':[str(self.exe),'--permission-mode','always_allow']},
               {'command':['relative.exe']},{'settings':{'model':'bad --flag'}},
               {'settings':{'effort':'ultra'}},{'nonce':'not-a-random-nonce'},
               {'run_id':'../escape'},{'runtime':self.base/'arbitrary-runtime'},
               {'max_age_seconds':0},{'prompt':'x'*30000}]
        for case in cases:
            with self.subTest(case=case),self.assertRaises(ValueError):self.build(**case)
            self.assertFalse((self.run/'doubao-manifest-binding.json').exists())

    def test_binding_is_exclusive_and_existing_manifest_never_reused(self):
        module=self.module()
        stale=self.workspace/f'.dispatcher-doubao-{self.nonce}.json'
        stale.write_text('{}')
        with self.assertRaises(ValueError):self.build()
        self.assertEqual(stale.read_text(),'{}')
        other='7b'*32
        self.build(nonce=other)
        before=(self.run/'doubao-manifest-binding.json').read_bytes()
        with self.assertRaises((ValueError,FileExistsError)):self.build(nonce='8c'*32)
        self.assertEqual((self.run/'doubao-manifest-binding.json').read_bytes(),before)

    def test_valid_exit_and_manifest_return_execution_evidence_not_review_approval(self):
        _,binding=self.prepared();path,_=self.manifest(binding)
        result=self.module().normalize_result(binding,exit_code=0,session_id='cli-session-test')
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['executor'],'doubao')
        self.assertEqual(result['session_id'],'cli-session-test')
        self.assertFalse(result['resume_available'])
        self.assertFalse(result['requires_quarantine'])
        self.assertEqual(result['doubao_receipt']['manifest_sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(result['doubao_receipt']['artifact_sha256']['result.md'],
                         hashlib.sha256((self.workspace/'result.md').read_bytes()).hexdigest())
        self.assertNotIn('review_status',result)

    def test_nonzero_exit_timeout_and_cancel_never_accept_even_valid_manifest(self):
        _,binding=self.prepared();self.manifest(binding)
        module=self.module()
        for fields in ({'exit_code':1},{'exit_code':0,'timed_out':True},
                       {'exit_code':0,'cancelled':True},{'exit_code':130},{'exit_code':None}):
            with self.subTest(fields=fields),self.assertRaises(module.DoubaoRunIncomplete) as error:
                module.normalize_result(binding,**fields)
            self.assertTrue(error.exception.requires_quarantine)
        with self.assertRaises(module.DoubaoRunIncomplete) as error:
            module.normalize_result(binding,exit_code=143,cancelled=True,remote_stop_confirmed=True)
        self.assertFalse(error.exception.requires_quarantine)

    def test_known_login_failure_requires_explicit_no_started_task_evidence(self):
        _,binding=self.prepared();module=self.module()
        with self.assertRaises(module.DoubaoLoginRequired) as error:
            module.normalize_result(binding,exit_code=4,task_started=False,stderr='未登录，请先运行 doubao login。')
        self.assertFalse(error.exception.requires_quarantine)
        with self.assertRaises(module.DoubaoRunIncomplete) as error:
            module.normalize_result(binding,exit_code=4,stderr='未登录，请先运行 doubao login。')
        self.assertTrue(error.exception.requires_quarantine)

    def test_old_nonce_wrong_run_extra_keys_duplicate_json_and_stale_mtime_rejected(self):
        _,binding=self.prepared();module=self.module()
        for changes in ({'nonce':'8b'*32},{'run_id':'old-run'},{'unexpected':'ignored?'},
                        {'artifacts':[]},{'status':'passed'},{'checks':'claimed'}):
            path,_=self.manifest(binding,**changes)
            with self.subTest(changes=changes),self.assertRaises(module.DoubaoRunIncomplete):
                module.normalize_result(binding,exit_code=0)
        path,value=self.manifest(binding)
        text=json.dumps(value)
        path.write_text(text[:-1]+',"status":"complete"}',encoding='utf-8')
        with self.assertRaises(module.DoubaoRunIncomplete):module.normalize_result(binding,exit_code=0)
        path,_=self.manifest(binding)
        os.utime(path,ns=(binding['created_ns']-10_000_000_000,)*2)
        with self.assertRaises(module.DoubaoRunIncomplete):module.normalize_result(binding,exit_code=0)

    def test_expired_or_tampered_host_binding_rejected(self):
        _,binding=self.prepared();self.manifest(binding);module=self.module()
        with self.assertRaises(module.DoubaoRunIncomplete):
            module.normalize_result(binding,exit_code=0,now_ns=binding['expires_ns']+1)
        changed=copy.deepcopy(binding);changed['workspace']=str(self.base/'elsewhere')
        with self.assertRaises(module.DoubaoRunIncomplete):module.normalize_result(changed,exit_code=0)
        saved=self.run/'doubao-manifest-binding.json'
        data=json.loads(saved.read_text());data['nonce']='0'*64;saved.write_text(json.dumps(data))
        with self.assertRaises(module.DoubaoRunIncomplete):module.normalize_result(binding,exit_code=0)

    def test_manifest_and_private_or_escaping_files_cannot_be_artifacts(self):
        _,binding=self.prepared();module=self.module()
        private=self.workspace/'.env';private.write_text('do not read')
        for artifact in (binding['manifest_name'],binding['manifest_name'].upper(),'../outside.txt','C:/outside.txt','.env',
                         'result.md:stream','result.md.','result.md ','/absolute',
                         '.dispatcher-doubao-another.json'):
            self.manifest(binding,artifacts=[artifact])
            with self.subTest(artifact=artifact),self.assertRaises(module.DoubaoRunIncomplete):
                module.normalize_result(binding,exit_code=0)

    def test_missing_empty_duplicate_and_changed_artifacts_fail(self):
        _,binding=self.prepared();module=self.module()
        for names in (['missing.md'],['empty.md'],['result.md','RESULT.md']):
            (self.workspace/'empty.md').write_bytes(b'')
            self.manifest(binding,artifacts=names)
            with self.subTest(names=names),self.assertRaises(module.DoubaoRunIncomplete):
                module.normalize_result(binding,exit_code=0)
        self.manifest(binding)
        with patch('dispatcher.doubao._read_file',side_effect=ValueError('changed bytes')):
            with self.assertRaises(module.DoubaoRunIncomplete):module.normalize_result(binding,exit_code=0)

    def test_needs_input_report_may_have_no_deliverables(self):
        _,binding=self.prepared()
        self.manifest(binding,status='needs_input',artifacts=[],checks=[],needs_input='Missing input')
        result=self.module().normalize_result(binding,exit_code=0)
        self.assertEqual(result['status'],'needs_input')
        self.assertEqual(result['artifacts'],[])

    def test_control_manifest_size_is_bounded(self):
        _,binding=self.prepared();module=self.module()
        path=self.workspace/binding['manifest_name'];path.write_bytes(b'x'*(module.MAX_MANIFEST_BYTES+1))
        with self.assertRaises(module.DoubaoRunIncomplete):module.normalize_result(binding,exit_code=0)

    def test_expiration_during_validation_and_late_artifact_change_fail_closed(self):
        _,binding=self.prepared();self.manifest(binding);module=self.module()
        with patch.object(module.time,'time_ns',side_effect=[binding['created_ns']+1,binding['expires_ns']+1]):
            with self.assertRaises(module.DoubaoRunIncomplete):module.normalize_result(binding,exit_code=0)
        real_read=module._read_file;changed=False
        def mutate_after_first_artifact(path,*args,**kwargs):
            nonlocal changed
            digest=real_read(path,*args,**kwargs)
            if Path(path).name=='result.md' and not changed:
                changed=True;Path(path).write_text('A LATER CHANGE',encoding='utf-8')
            return digest
        with patch.object(module,'_read_file',side_effect=mutate_after_first_artifact):
            with self.assertRaises(module.DoubaoRunIncomplete):module.normalize_result(binding,exit_code=0)

    def test_dot_relative_paths_and_windows_separators_remain_safe(self):
        _,binding=self.prepared();module=self.module()
        self.manifest(binding,artifacts=['./sub\\answer.md'])
        sub=self.workspace/'sub';sub.mkdir();(sub/'answer.md').write_text('answer')
        result=module.normalize_result(binding,exit_code=0)
        self.assertEqual(list(result['doubao_receipt']['artifact_sha256']),['sub/answer.md'])

    def test_utf8_bom_is_accepted_but_non_json_constants_are_not(self):
        _,binding=self.prepared();module=self.module()
        path,value=self.manifest(binding)
        path.write_text(json.dumps(value),encoding='utf-8-sig')
        self.assertEqual(module.normalize_result(binding,exit_code=0)['status'],'complete')
        value['checks']=[float('nan')]
        path.write_text(json.dumps(value),encoding='utf-8')
        with self.assertRaises(module.DoubaoRunIncomplete):module.normalize_result(binding,exit_code=0)

    def test_hardlinked_manifest_and_artifact_are_rejected(self):
        _,binding=self.prepared();path,_=self.manifest(binding);module=self.module()
        os.link(path,self.base/'manifest-copy')
        with self.assertRaises(module.DoubaoRunIncomplete):module.normalize_result(binding,exit_code=0)

    @unittest.skipUnless(os.name=='nt','Windows junction regression')
    def test_replaced_workspace_junction_rejected(self):
        import _winapi
        _,binding=self.prepared();self.manifest(binding);module=self.module()
        moved=self.base/'moved-project';self.workspace.rename(moved)
        _winapi.CreateJunction(str(moved),str(self.workspace))
        try:
            with self.assertRaises(module.DoubaoRunIncomplete):module.normalize_result(binding,exit_code=0)
        finally:self.workspace.rmdir()


if __name__=='__main__':unittest.main()
