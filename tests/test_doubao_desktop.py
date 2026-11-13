"""Desktop provider separation and scheduler tests; no native models or apps."""
import contextlib
import importlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock,patch

from dispatcher.control import Controller
from dispatcher.core import Store
from dispatcher.execution_settings import agent_capabilities,resolve_execution_settings
from dispatcher.runner import MultiAgentRunner
from dispatcher.service import Service
from dispatcher.workspace import WorkspaceManager
from dispatcher import workbuddy_runner as worker
from tests.test_service import FakeDida
from tests.test_workbuddy_runner import FakeBroker,report


class DoubaoDesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.config={'codex_command':['never-call-codex'],'task_timeout_seconds':1,
            'enabled_executors':['codex','workbuddy','doubao'],
            'workbuddy_transport':'mcp','doubao_transport':'mcp',
            'require_human_review':True,'auto_complete':True}
        self.registry={name:{'name':name,'enabled':False,'detected':False,'command':[]}
                       for name in ('workbuddy','doubao')}
        with patch('dispatcher.runner.discover_executors',return_value=self.registry):
            self.runner=MultiAgentRunner(self.root,self.config)
        self.doubao=FakeBroker();self.doubao.provider='doubao';self.doubao.on_submit=self.doubao.complete
        self.workbuddy=FakeBroker();self.workbuddy.provider='workbuddy';self.workbuddy.ready=False
        self.runner.doubao_broker=self.doubao;self.runner.workbuddy_broker=self.workbuddy
        self.store=Store(self.root/'tasks.db');self.addCleanup(self.store.close)
        self.task={'id':'t','projectId':'p','title':'编写架构方案','status':0,'priority':5,
                   'tags':['AI','agent:doubao']}
        self.dida=FakeDida(self.task)
        self.service=Service(self.root,self.config,self.store,self.dida,self.runner)
        self.service.sync()

    def execute(self):
        with (patch('dispatcher.runner.run_process',side_effect=AssertionError('No generic process')),
             patch('dispatcher.runner.doubao_process.run_doubao_process',side_effect=AssertionError('No Doubao CLI')),
             patch.object(self.runner,'_invoke',return_value=report()) as verify):
            self.service.execute(self.store.claim())
        return self.store.get('p:t'),verify

    def test_doubao_desktop_without_cli_reaches_independent_and_human_review(self):
        row,verify=self.execute()
        self.assertEqual(row['state'],'awaiting_review',row.get('error'))
        self.assertEqual((self.dida.completions,self.dida.comments),(0,0))
        verify.assert_called_once();self.assertTrue(verify.call_args.args[4])
        self.assertEqual(len(self.doubao.submissions),1);self.assertEqual(self.workbuddy.submissions,[])
        execution=row['result']['execution']
        self.assertEqual(execution['executor'],'doubao')
        self.assertFalse(execution['resume_available'])
        self.assertEqual(execution['execution_settings']['sources']['model'],'native_session')
        self.assertEqual(execution['doubao_receipt']['provider'],'doubao')
        self.assertNotIn('workbuddy_receipt',execution)
        run=Path(row['run_dir'])
        self.assertTrue((run/'doubao-receipt.json').is_file())
        self.assertFalse((run/'workbuddy-receipt.json').exists())
        self.assertNotIn('WorkBuddy',(run/'execution-resume.txt').read_text(encoding='utf-8'))

    def test_readiness_is_separate_and_cannot_borrow_another_provider_broker(self):
        registry=worker.refresh_worker_registry(self.registry,self.config,self.runner)
        self.assertTrue(registry['doubao']['enabled']);self.assertFalse(registry['workbuddy']['enabled'])
        self.doubao.ready=False;self.workbuddy.ready=True
        registry=worker.refresh_worker_registry(registry,self.config,self.runner)
        self.assertFalse(registry['doubao']['enabled']);self.assertTrue(registry['workbuddy']['enabled'])
        self.runner.doubao_broker=self.workbuddy
        self.assertFalse(worker.worker_status(self.runner,'doubao')['ready'])
        row,verify=self.execute()
        self.assertEqual(row['state'],'needs_input');verify.assert_not_called()
        self.assertEqual(self.workbuddy.submissions,[])

    def test_cancellation_of_doubao_does_not_cancel_workbuddy(self):
        self.doubao.on_submit=None
        def source_done(_):
            self.dida.task['status']=2;self.service.sync()
        self.doubao.on_result=source_done
        with patch.object(worker.time,'sleep'):
            row,verify=self.execute()
        self.assertEqual(row['state'],'cancelled')
        self.assertEqual(len(self.doubao.cancelled),1)
        self.assertEqual(self.workbuddy.cancelled,[]);verify.assert_not_called()

    def test_wait_segment_ends_between_readiness_and_submit_needs_input(self):
        from dispatcher.workbuddy_mcp import BridgeError
        with patch.object(self.doubao,'submit',side_effect=BridgeError('worker_not_waiting')) as submit:
            row,verify=self.execute()
        self.assertEqual(row['state'],'needs_input',row.get('error'))
        self.assertEqual(submit.call_count,1)
        self.assertEqual(self.doubao.submissions,[])
        self.assertEqual(self.doubao.cancelled,[])
        self.assertEqual((self.dida.completions,self.dida.comments),(0,0))
        verify.assert_not_called()

    def test_desktop_model_overrides_are_rejected_for_every_layer(self):
        capabilities=agent_capabilities('doubao',self.config)
        self.assertFalse(capabilities['model']);self.assertEqual(capabilities['efforts'],[])
        self.assertIn('豆包',capabilities['note']);self.assertNotIn('WorkBuddy',capabilities['note'])
        for settings in ({'model':'model-id'},{'effort':'high'}):
            for layer in ('project','agent','session'):
                with self.subTest(settings=settings,layer=layer):
                    config=dict(self.config)
                    if layer=='project':config['agent_settings']={'doubao':settings}
                    with self.assertRaisesRegex(ValueError,'豆包'):
                        resolve_execution_settings('doubao',config,settings if layer=='agent' else {},
                                                   settings if layer=='session' else {},'easy')
        value=resolve_execution_settings('doubao',self.config,{}, {},'easy')
        self.assertEqual(value['sources'],{'model':'native_session','effort':'native_session'})
        self.assertTrue(agent_capabilities('doubao',{'doubao_transport':'cli'})['model'])
        self.assertEqual(resolve_execution_settings('doubao',{'doubao_transport':'cli'}, {},
                         {'model':'my-model'},'easy')['cli_model'],'my-model')

    def test_controller_refreshes_ready_state_and_refuses_existing_workspace(self):
        controller=Controller(self.service,WorkspaceManager(self.root,self.store),list(self.registry.values()))
        self.doubao.ready=False
        with self.assertRaises(ValueError):controller._validate_assignment_route('doubao',self.task)
        self.doubao.ready=True
        row=self.store.get('p:t')
        response=controller.submit({'action':'assign','request_id':'desktop-doubao-test',
            'task_id':row['id'],'revision':row['revision'],'executor':'doubao'})
        self.assertIsNotNone(response)
        with self.assertRaisesRegex(ValueError,'豆包'):
            controller._validate_workspace_choice({'mode':'existing','workspace_id':'ws_'+'a'*24},
                                                  self.task,executor='doubao')
        agents={item['id']:item for item in controller.state()['agents']}
        self.assertTrue(agents['doubao']['enabled'])
        self.assertFalse(agents['doubao']['capabilities']['model'])
        self.assertFalse(agents['workbuddy']['enabled'])

    def test_cli_branch_still_uses_native_doubao_invocation(self):
        self.runner.config=dict(self.config,doubao_transport='cli')
        run=self.root/'manual-run';(run/'workspace').mkdir(parents=True)
        row={'payload':self.task,'difficulty':'easy','kind':'document'}
        with (patch.object(self.runner,'_invoke_doubao',return_value={'status':'needs_input'}) as native,
             patch.object(worker,'execute_desktop',side_effect=AssertionError('Must not use desktop MCP'))):
            result=self.runner._invoke_external('doubao',row,run)
        native.assert_called_once();self.assertEqual(result['status'],'needs_input')


class DesktopConfigAndStartupTests(unittest.TestCase):
    def test_config_defaults_cli_and_validates_both_transport_choices(self):
        cli=importlib.import_module('dispatcher.__main__')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            self.assertEqual(cli.load_config(root)['doubao_transport'],'cli')
            for key in ('workbuddy_transport','doubao_transport'):
                for value in ('mcp','cli','invalid'):
                    (root/'config.local.json').write_text(json.dumps({key:value}),encoding='utf-8')
                    if value=='invalid':
                        with self.assertRaisesRegex(ValueError,key):cli.load_config(root)
                    else:self.assertEqual(cli.load_config(root)[key],value)

    def test_daemon_starts_attaches_and_closes_two_separate_brokers(self):
        cli=importlib.import_module('dispatcher.__main__')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'config.local.json').write_text(json.dumps({'workbuddy_transport':'mcp','doubao_transport':'mcp'}))
            brokers={}
            def construct(root,*,provider):
                broker=Mock(provider=provider);broker.start.return_value=broker;brokers[provider]=broker
                return broker
            service=SimpleNamespace(store=Mock(),runner=SimpleNamespace(),snapshot=lambda:{})
            def daemon():
                self.assertIs(service.runner.workbuddy_broker,brokers['workbuddy'])
                self.assertIs(service.runner.doubao_broker,brokers['doubao'])
                self.assertIsNot(service.runner.workbuddy_broker,service.runner.doubao_broker)
            service.daemon=daemon
            with (patch.object(cli,'make_service',return_value=service),patch.object(cli,'detect_agents',return_value=[]),
                 patch.object(cli,'start_dashboard',return_value=contextlib.nullcontext()),
                 patch('dispatcher.workbuddy_mcp.WorkBuddyBroker',side_effect=construct),
                 contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(cli.main(['--root',str(root),'daemon']),0)
            self.assertEqual(set(brokers),{'workbuddy','doubao'})
            for broker in brokers.values():broker.start.assert_called_once();broker.close.assert_called_once()


if __name__=='__main__':unittest.main()
