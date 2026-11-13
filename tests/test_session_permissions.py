import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dispatcher import permissions
from dispatcher.core import Store
from dispatcher.runner import CodexRunner, codex_arguments
from tests.test_model_settings_api import ModelSettingsAPITests


class PermissionPolicyTests(unittest.TestCase):
    def test_only_supported_profiles_and_explicit_full_ack(self):
        for name in ('codex', 'codebuddy'):
            self.assertEqual(permissions.validate(name, 'full', True), 'full')
            for ack in (False, 'true', 1, None):
                with self.assertRaises(ValueError): permissions.validate(name, 'full', ack)
        for name in ('auto', 'qoder', 'workbuddy', 'doubao'):
            with self.assertRaises(ValueError): permissions.validate(name, 'full', True)
        for value in ('--unsafe', {}, None, 'unknown'):
            with self.assertRaises(ValueError): permissions.validate('codex', value)
        for mode in ('auto', 'full'):
            with self.assertRaises(ValueError): permissions.resolve(mode, 'codebuddy', answer=True)

    def test_codex_modes_and_readonly_verifier_are_separate(self):
        auto=codex_arguments('ws','schema','out','low',None,False,permissions=permissions.resolve('auto','codex'))
        self.assertIn('--approve-for-me',auto)
        self.assertNotIn('approval_policy="never"',auto)
        full=codex_arguments('ws','schema','out','low',None,False,permissions=permissions.resolve('full','codex'))
        self.assertEqual(full[full.index('--sandbox')+1],'danger-full-access')
        safe=codex_arguments('ws','schema','out','low',None,True)
        self.assertEqual(safe[safe.index('--sandbox')+1],'read-only')

    def test_snapshot_cannot_be_changed_after_prepare(self):
        with tempfile.TemporaryDirectory() as directory:
            runner=CodexRunner(directory, {'codex_command':['codex']})
            row={'payload':{'id':'t','projectId':'p','title':'write a document'},'difficulty':'easy','kind':'document','permission_profile':'auto'}
            run=runner.prepare(row)
            try:
                (run/'permissions.json').write_text('{}')
                with patch.object(runner,'_invoke') as invoke:
                    with self.assertRaises(ValueError): runner.execute(row,run)
                    invoke.assert_not_called()
            finally: runner.release_workspace(row)

    def test_codebuddy_native_modes_preserve_vendor_critical_guard(self):
        from dispatcher.codebuddy import build_invocation
        from dispatcher.runner import RESULT_SCHEMA
        with tempfile.TemporaryDirectory() as d:
            run=Path(d);ws=run/'workspace';ws.mkdir();runtime=ws/'.runtime'
            for profile,mode in [('default','dontAsk'),('auto','auto'),('full','bypassPermissions')]:
                argv,env,_,_=build_invocation(['node','entry'],ws,run,'test',{},runtime,
                    schema=RESULT_SCHEMA,permissions=permissions.resolve(profile,'codebuddy'),home=run)
                self.assertEqual(argv[argv.index('--permission-mode')+1],mode)
                self.assertEqual(env['CODEBUDDY_IS_SANDBOX'],'')
                self.assertIn('--strict-mcp-config',argv)
                self.assertEqual('Bash' in argv[argv.index('--tools')+1],profile!='default')

    def test_external_full_is_only_profile_that_removes_outer_sandbox(self):
        from dispatcher.runner import MultiAgentRunner, RESULT_SCHEMA
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);config={'executor_commands':{'codex':['codex']},'enabled_executors':['codebuddy']}
            with patch('dispatcher.runner.discover_executors',return_value={'codebuddy':{'enabled':True,'command':['node','entry']}}):
                runner=MultiAgentRunner(root,config)
            for profile in ('default','auto','full'):
                row={'payload':{'id':profile,'projectId':'p','title':'write file'},'kind':'document','difficulty':'easy','permission_profile':profile,'manual_executor':'codebuddy'}
                run=runner.prepare(row)
                result={'status':'needs_input','summary':'need input','artifacts':[],'checks':[],'needs_input':'more details'}
                try:
                    with patch('dispatcher.runner.run_process',return_value=(json.dumps({'type':'result','subtype':'success','is_error':False,'structured_output':result}),'')) as process:
                        runner.execute(row,run)
                        self.assertEqual('sandbox' in process.call_args.args[0],profile!='full')
                finally:runner.release_workspace(row)


class PermissionAPITests(ModelSettingsAPITests):
    def test_assignment_full_is_one_shot_and_history_is_frozen(self):
        command=self.assignment(permission_profile='full',full_access_ack=True)
        self.control.process_pending()
        self.assertEqual(self.store.get_command(command['id'])['status'],'done')
        row=self.store.claim()
        self.assertEqual(row['permission_profile'],'full')
        self.assertEqual(self.store.get(row['id'])['permission_profile'],'default')
        snapshot=permissions.resolve('full','codex')
        self.store.mark(row['id'],'running',result={'executor':'codex','permissions':snapshot})
        self.assertEqual(self.store.get_run(row['current_run_id'])['permissions'],snapshot)
        with self.assertRaises(Exception):
            self.store.mark(row['id'],'running',result={'executor':'codex','permissions':permissions.resolve('default','codex')})
        self.assertEqual(self.control.state()['system']['review_policy'],'human_required')
        self.assertEqual((self.dida.comments,self.dida.completions),(0,0))

    def test_full_ack_and_text_conflicts_rejected_before_queue(self):
        for options in ({'permission_profile':'full'}, {'permission_profile':'full','full_access_ack':'true'},
                        {'permission_profile':'full','full_access_ack':True,'delivery_mode':'answer'},
                        {'permission_profile':'auto','delivery_mode':'answer'}):
            with self.subTest(options=options),self.assertRaises(ValueError):self.assignment(**options)
        self.assertIsNone(self.store.next_command())

    def test_changed_source_clears_queued_elevated_choice(self):
        queued=self.assignment(permission_profile='full',full_access_ack=True);self.control.process_pending()
        self.dida.task['title']='Different work'
        self.service.sync(force=True)
        self.assertEqual(self.store.get('p:t')['permission_profile'],'default')

    def test_unseen_source_edit_cannot_reuse_full_access_consent(self):
        queued=self.assignment(permission_profile='full',full_access_ack=True)
        self.dida.task['title']='Unseen different task'
        self.control.process_pending()
        self.assertEqual(self.store.get_command(queued['id'])['http_status'],409)
        self.assertEqual(self.store.get('p:t')['state'],'ignored')


class TimeoutDiagnosticsTests(unittest.TestCase):
    def test_active_reasoning_budget_does_not_expand_other_models_or_disable_idle_timeout(self):
        from dispatcher.codebuddy import runtime_limits
        config={'task_timeout_seconds':600,'codebuddy_model_timeouts':{'hy4free':1800}}
        self.assertEqual(runtime_limits(config,{'model':'hy4free'}),{'total_seconds':1800,'first_token_seconds':180,'stream_idle_seconds':180,'request_retries':1})
        self.assertEqual(runtime_limits(config,{'model':'glm-5.3-flash'})['total_seconds'],600)
        for seconds in (True,0,99999,'1800'):
            with self.assertRaises(ValueError):runtime_limits({'codebuddy_model_timeouts':{'hy4free':seconds}}, {})

    def test_incremental_thinking_is_replaced_with_bounded_metadata(self):
        from dispatcher.codebuddy import ProgressFilter,progress
        f=ProgressFilter()
        event={'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'thinking_delta','thinking':'PRIVATE_REASONING'}}}
        first=f(json.dumps(event))
        self.assertIn('thinking',first);self.assertNotIn('PRIVATE_REASONING',first)
        self.assertIsNone(f(json.dumps(event)))
        assistant={'type':'assistant','message':{'content':[{'type':'thinking','thinking':'PRIVATE_REASONING'},{'type':'tool_use','name':'Write','input':{'content':'report'}}]}}
        result=f(json.dumps(assistant));self.assertNotIn('PRIVATE_REASONING',result);self.assertIn('Write',result)
        with tempfile.TemporaryDirectory() as d:
            (Path(d)/'execution.events.jsonl').write_text(first+result,encoding='utf-8')
            self.assertEqual(progress(d)['phase'],'thinking')

    def test_filtered_process_captures_terminal_and_stops_on_timeout(self):
        import sys
        from dispatcher.adapters import run_process
        from dispatcher.codebuddy import ProgressFilter
        with tempfile.TemporaryDirectory() as d:
            script='import json;print(json.dumps({"type":"stream_event","event":{"delta":{"type":"thinking_delta","thinking":"PRIVATE_REASONING"}}}));print(json.dumps({"type":"result","subtype":"success"}))'
            out,_=run_process([sys.executable,'-c',script],log_prefix=Path(d)/'execution',line_filter=ProgressFilter())
            self.assertNotIn('PRIVATE_REASONING',out);self.assertEqual(json.loads(out.splitlines()[-1])['type'],'result')
            with self.assertRaises(TimeoutError):
                run_process([sys.executable,'-c','import time;time.sleep(10)'],timeout=.2,log_prefix=Path(d)/'timeout',line_filter=ProgressFilter())

    def test_only_metadata_is_retained_and_absence_is_not_permission_denial(self):
        from dispatcher.codebuddy import timeout_diagnostic
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            events=[{'type':'system','subtype':'init','model':'hy4free','token':'SECRET'},
                    {'type':'system','subtype':'status','status':None,'_meta':{'private':'SECRET'}}]
            (p/'execution.events.jsonl').write_text('\n'.join(map(json.dumps,events)))
            message=timeout_diagnostic(p,600)
            data=json.loads((p/'execution-diagnostics.json').read_text())
            self.assertEqual(data['stage'],'waiting_first_response')
            self.assertNotIn('SECRET',json.dumps(data)+message)
            self.assertIn('未观察到',message)
            events.append({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Write','input':{'secret':'SECRET'}}]}})
            (p/'execution.events.jsonl').write_text('\n'.join(map(json.dumps,events)))
            timeout_diagnostic(p,600)
            self.assertEqual(json.loads((p/'execution-diagnostics.json').read_text())['stage'],'tool_activity')

if __name__=='__main__':unittest.main()
