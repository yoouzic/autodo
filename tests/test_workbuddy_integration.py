import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dispatcher.core import Store
from dispatcher.execution_settings import resolve_execution_settings
from dispatcher.runner import MultiAgentRunner
from dispatcher.service import Service
from dispatcher.routing import task_available_executors
from tests.test_service import FakeDida


class WorkBuddyIntegrationTests(unittest.TestCase):
    def test_desktop_session_settings_are_explicit_and_cannot_be_silently_overridden(self):
        from dispatcher.execution_settings import agent_capabilities
        config={'workbuddy_transport':'mcp'}
        capabilities=agent_capabilities('workbuddy',config)
        self.assertFalse(capabilities['model'])
        self.assertEqual(capabilities['efforts'],[])
        value=resolve_execution_settings('workbuddy',config,{}, {},'easy')
        self.assertEqual(value['sources'],{'model':'native_session','effort':'native_session'})
        for settings in ({'model':'auto'},{'effort':'high'}):
            with self.subTest(settings=settings),self.assertRaises(ValueError):
                resolve_execution_settings('workbuddy',config,{},settings,'easy')

    def test_model_setting_allowed_but_unverified_model_effort_not_advertised(self):
        settings=resolve_execution_settings('workbuddy',{}, {},{'model':'auto'},'easy')
        self.assertEqual(settings['cli_model'],'auto')
        self.assertIsNone(settings['effort'])
        with self.assertRaises(ValueError):
            resolve_execution_settings('workbuddy',{}, {},{'model':'auto','effort':'high'},'easy')

    def test_file_only_code_executor_requires_configured_repository_checks(self):
        registry={'workbuddy':{'enabled':True}}
        available=task_available_executors({'kind':'code','repo':'example'}, {},registry)
        self.assertFalse(available['workbuddy'])
        config={'repositories':{'repo':'example'},'repository_checks':{'repo':{'commands':[['test']]}}}
        self.assertTrue(task_available_executors({'kind':'code','repo':'example'},config,registry)['workbuddy'])

    def test_workbuddy_task_reaches_review_with_diagnostic_only_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            config={'codex_command':['codex'],'enabled_executors':['codex','workbuddy'],'require_human_review':True}
            registry={'codex':{'enabled':True,'command':['codex']},
                      'workbuddy':{'enabled':True,'command':['node','workbuddy-entry']}}
            with patch('dispatcher.runner.discover_executors',return_value=registry):runner=MultiAgentRunner(root,config)
            store=Store(root/'data/tasks.db')
            self.addCleanup(store.close)
            task={'id':'t','projectId':'p','title':'编写文档','status':0,'tags':[]}
            dida=FakeDida(task)
            service=Service(root,config,store,dida,runner);service.sync()
            current=store.get('p:t')
            store.assign('p:t','workbuddy',current['revision'],task,config,execution_overrides={'model':'auto'})
            report={'status':'complete','summary':'done','artifacts':['result.md'],'checks':['read'],'needs_input':''}
            def process(argv,**kwargs):
                self.assertNotIn('--dangerously-skip-permissions',argv)
                self.assertIn('dontAsk',argv)
                self.assertIn('--no-session-persistence',argv)
                self.assertEqual(kwargs['env']['CODEBUDDY_CONFIG_DIR'],str(Path.home()/'.workbuddy'))
                (Path(kwargs['cwd'])/'result.md').write_text('result',encoding='utf-8')
                return json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':'diagnostic-session',
                                   'structured_output':report,'permission_denials':[]}),''
            with patch('dispatcher.runner.run_process',side_effect=process),patch.object(runner,'_invoke',return_value=report.copy()):
                service.execute(store.claim())
            result=store.get('p:t')
            self.assertEqual(result['state'],'awaiting_review',result.get('error'))
            execution=result['result']['execution']
            self.assertEqual(execution['executor'],'workbuddy')
            self.assertFalse(execution['resume_available'])
            self.assertEqual(execution['execution_settings']['model'],'auto')
            self.assertEqual((dida.comments,dida.completions),(0,0))

    def test_login_required_stops_in_needs_input_without_source_writeback(self):
        from dispatcher.workbuddy import WorkBuddyLoginRequired
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config={'codex_command':['codex'],'require_human_review':True}
            registry={'workbuddy':{'enabled':True,'command':['node','entry']}}
            with patch('dispatcher.runner.discover_executors',return_value=registry):runner=MultiAgentRunner(root,config)
            store=Store(root/'data/tasks.db');self.addCleanup(store.close)
            task={'id':'t','projectId':'p','title':'文档','status':0,'tags':[]}
            dida=FakeDida(task);service=Service(root,config,store,dida,runner);service.sync()
            current=store.get('p:t');store.assign('p:t','workbuddy',current['revision'],task,config)
            with patch.object(runner,'execute',side_effect=WorkBuddyLoginRequired()):service.execute(store.claim())
            self.assertEqual(store.get('p:t')['state'],'needs_input')
            self.assertIsNone(store.claim())
            self.assertEqual((dida.comments,dida.completions),(0,0))


if __name__=='__main__':unittest.main()
