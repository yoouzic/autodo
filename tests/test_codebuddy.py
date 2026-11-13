import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dispatcher.execution_settings import agent_capabilities, resolve_execution_settings
from dispatcher.executors import discover_executors, normalize_result
from dispatcher.external import invocation
from dispatcher.routing import EXECUTORS, choose_executor, task_available_executors
from dispatcher.runner import RESULT_SCHEMA
from dispatcher.runner import MultiAgentRunner
from dispatcher.core import Store
from dispatcher.service import Service
from tests.test_service import FakeDida


REPORT = {'status':'complete','summary':'Created the checklist','artifacts':['checklist.md'],
          'checks':['Read back the file'],'needs_input':''}
CATALOG = [{'id':'glm-5.3-flash','label':'GLM-5.3-Flash','efforts':['low','high','max']},
           {'id':'glm-5.3','label':'GLM-5.3','efforts':[]}]


class CodeBuddyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config = {'_codebuddy_model_catalog':CATALOG,
            'agent_settings':{'codebuddy':{'model':'glm-5.3-flash','effort':'low'}}}

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('dispatcher.codebuddy'), 'CodeBuddy adapter missing')
        return importlib.import_module('dispatcher.codebuddy')

    def test_discovery_uses_independent_npm_package_without_enabling_workbuddy(self):
        self.assertIn('codebuddy', EXECUTORS)
        entry = self.root/'npm/node_modules/@tencent-ai/codebuddy-code/bin/codebuddy'
        entry.parent.mkdir(parents=True); entry.write_text('',encoding='utf-8')
        with patch.dict(os.environ, {'APPDATA':str(self.root)}), patch('dispatcher.executors.shutil.which',return_value='node.exe'):
            agents=discover_executors(self.root, {'enabled_executors':['codebuddy']})
        self.assertEqual(agents['codebuddy']['command'], ['node.exe',str(entry)])
        self.assertTrue(agents['codebuddy']['enabled'])
        self.assertFalse(agents['workbuddy']['enabled'])

    def test_agent_defaults_and_session_effort_override_are_independent(self):
        self.assertIn('codebuddy', EXECUTORS)
        resolved=resolve_execution_settings('codebuddy',self.config,{'effort':'high'},{'effort':'max'},'easy')
        self.assertEqual((resolved['model'],resolved['effort']),('glm-5.3-flash','max'))
        self.assertEqual(resolved['sources'],{'model':'project','effort':'session'})
        changed=resolve_execution_settings('codebuddy',self.config,{}, {'model':'glm-5.3'},'easy')
        self.assertIsNone(changed['effort'])
        with self.assertRaises(ValueError):
            resolve_execution_settings('codebuddy',self.config,{}, {'effort':'medium'},'easy')
        self.assertEqual(agent_capabilities('codebuddy',self.config)['models'],CATALOG)

    def test_manual_and_tag_route_and_code_check_requirement(self):
        self.assertIn('codebuddy', EXECUTORS)
        task={'title':'Checklist','tags':['agent:codebuddy']}
        self.assertEqual(choose_executor(task,'easy','document',{}, {'codebuddy':True})['executor'],'codebuddy')
        self.assertFalse(task_available_executors({'kind':'code'}, {}, {'codebuddy':{'enabled':True}})['codebuddy'])

    def test_catalog_is_public_metadata_and_validates_model_efforts(self):
        module=self.module(); (self.root/'data').mkdir()
        path=self.root/'data/codebuddy-model-catalog.json'
        self.assertEqual(module.load_catalog(self.root),[])
        path.write_text(json.dumps({'models':CATALOG}),encoding='utf-8')
        self.assertEqual(module.load_catalog(self.root),CATALOG)
        for rows in ([dict(CATALOG[0],efforts=['ultra'])], [dict(CATALOG[0],id='--bad')], CATALOG+[CATALOG[0]]):
            path.write_text(json.dumps({'models':rows}),encoding='utf-8')
            with self.assertRaises(ValueError): module.load_catalog(self.root)

    def test_invocation_has_separate_profile_and_bounded_file_only_tools(self):
        module=self.module(); run=self.root/'run'; workspace=run/'workspace'; workspace.mkdir(parents=True)
        runtime=workspace/'.runtime'
        argv,env,stdin,roots=module.build_invocation(['node.exe','codebuddy'],workspace,run,'Make the checklist',
            {'model':'glm-5.3-flash','effort':'high'},runtime,schema=RESULT_SCHEMA,home=self.root)
        self.assertEqual(env['CODEBUDDY_CONFIG_DIR'],str(self.root/'.codebuddy'))
        self.assertEqual(env['CODEBUDDY_DISABLE_TRACE_COLLECTOR'],'1')
        self.assertNotIn('.workbuddy',json.dumps(env))
        expected_roots=[runtime]+([self.root/'AppData/Local/CodeBuddyExtension/Data/Public/auth'] if os.name=='nt' else [])
        self.assertEqual(roots,expected_roots)
        self.assertNotIn(self.root/'.codebuddy',roots)
        self.assertEqual(argv[argv.index('--model')+1],'glm-5.3-flash')
        self.assertEqual(argv[argv.index('--effort')+1],'high')
        self.assertEqual(argv[argv.index('--tools')+1],'Read,Write,Edit,StructuredOutput')
        self.assertEqual(argv[argv.index('--permission-mode')+1],'dontAsk')
        self.assertEqual(argv[argv.index('--setting-sources')+1],'none')
        self.assertTrue(json.loads(argv[argv.index('--settings')+1])['disableAllHooks'])
        allow=json.loads(argv[argv.index('--settings')+1])['permissions']['allow']
        self.assertEqual(allow,[f'Write({workspace.as_posix()}/**)',f'Edit({workspace.as_posix()}/**)','StructuredOutput'])
        self.assertNotIn('--allowedTools',argv)  # path commas must not become extra permission rules
        self.assertIn('--strict-mcp-config',argv); self.assertIn('--no-session-persistence',argv)
        self.assertFalse(any(flag in argv for flag in ('-y','--dangerously-skip-permissions','--serve','--bg','--swarm')))
        self.assertEqual(stdin,'Make the checklist')
        self.assertFalse((self.root/'.codebuddy').exists())

    def test_external_dispatch_uses_codebuddy_builder(self):
        self.module(); run=self.root/'run'; workspace=run/'workspace'; workspace.mkdir(parents=True)
        argv,env,stdin,roots=invocation('codebuddy',['node.exe','codebuddy'],workspace,run,'Task',{},
                                    settings={'model':'glm-5.3-flash','effort':'low'})
        self.assertEqual(Path(env['CODEBUDDY_CONFIG_DIR']).name,'.codebuddy')
        self.assertEqual(argv[argv.index('--effort')+1],'low')

    def test_protocol_requires_clean_unique_terminal_and_preserves_agent_identity(self):
        self.module()
        event={'type':'result','subtype':'success','is_error':False,'session_id':'cb-1','structured_output':REPORT}
        result=normalize_result('codebuddy',json.dumps(event))
        self.assertEqual(result['executor'],'codebuddy'); self.assertFalse(result['resume_available'])
        for text in ('',json.dumps(event)+'\n'+json.dumps(event),json.dumps(dict(event,is_error=True)),
                     json.dumps(dict(event,permission_denials=[{'tool':'Write'}]))):
            with self.assertRaises(ValueError):normalize_result('codebuddy',text)

    def test_login_error_is_safe_and_distinct_from_workbuddy(self):
        module=self.module()
        with self.assertRaises(module.CodeBuddyLoginRequired) as failure:
            module.normalize_result('',stderr='Authentication required, please use /login SECRET')
        self.assertIn('CodeBuddy',str(failure.exception)); self.assertNotIn('SECRET',str(failure.exception))

    def incomplete_stream(self,status='needs_input',**terminal_changes):
        error={'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'read-1',
            'is_error':True,'content':[{'type':'text','text':'<tool_use_error>File does not exist.</tool_use_error>'}],
            '_meta':{'rawResponse':{'is_error':True,'content':'File does not exist.'}}}]}}
        report=dict(REPORT,status=status,summary='缺少额度数据',artifacts=[],needs_input='请提供可核验的额度数据。')
        terminal=dict(type='result',subtype='success',is_error=False,structured_output=report,**terminal_changes)
        return '\n'.join(map(json.dumps,[error,terminal]))

    def test_valid_incomplete_report_preserves_tool_error_evidence(self):
        for status in ('needs_input','failed'):
            raw=self.incomplete_stream(status)
            result=normalize_result('codebuddy',raw)
            self.assertEqual(result['status'],status)
            self.assertEqual(result['artifacts'],[])
            self.assertEqual(result['tool_error_count'],1)
            self.assertIn('额度数据',result['needs_input'])
        with self.assertRaises(ValueError):normalize_result('codebuddy',self.incomplete_stream('complete'))

    def test_incomplete_report_does_not_bypass_terminal_or_permission_errors(self):
        stream=self.incomplete_stream();terminal=json.loads(stream.splitlines()[-1])
        for end in (dict(terminal,is_error=True),dict(terminal,subtype='error_during_execution'),
                    dict(terminal,permission_denials=[{'tool_name':'Read'}])):
            with self.subTest(end=end),self.assertRaises(ValueError):
                normalize_result('codebuddy',stream.splitlines()[0]+'\n'+json.dumps(end))
        for raw in (stream+'\n'+json.dumps(terminal),stream+'\n'+json.dumps({'type':'system','subtype':'init'}),
                    json.dumps({'type':'error','error':'PRIVATE_TOKEN'})+'\n'+stream):
            with self.assertRaises(ValueError) as failure:normalize_result('codebuddy',raw)
            self.assertNotIn('PRIVATE_TOKEN',str(failure.exception))

    def test_incomplete_tool_result_stops_in_needs_input_without_review_or_source_write(self):
        config=dict(self.config,codex_command=['codex'],enabled_executors=['codebuddy'],require_human_review=True)
        registry={'codebuddy':{'enabled':True,'command':['node','entry']}}
        with patch('dispatcher.runner.discover_executors',return_value=registry):runner=MultiAgentRunner(self.root,config)
        store=Store(self.root/'data/tasks.db');self.addCleanup(store.close)
        task={'id':'t','projectId':'p','title':'查询额度','status':0,'tags':[]}
        dida=FakeDida(task);service=Service(self.root,config,store,dida,runner);service.sync()
        store.assign('p:t','codebuddy',store.get('p:t')['revision'],task,config)
        with patch('dispatcher.runner.run_process',return_value=(self.incomplete_stream(),'')),patch.object(runner,'verify') as verify:
            service.execute(store.claim())
        row=store.get('p:t');self.assertEqual(row['state'],'needs_input',row.get('error'))
        self.assertEqual(row['result']['tool_error_count'],1);verify.assert_not_called()
        self.assertEqual((dida.comments,dida.completions),(0,0));self.assertIsNone(store.claim())

    def test_normal_service_flow_freezes_overrides_and_stops_for_human_review(self):
        self.module()
        config=dict(self.config,codex_command=['codex'],enabled_executors=['codebuddy'],require_human_review=True)
        registry={'codebuddy':{'enabled':True,'command':['node','entry']}}
        with patch('dispatcher.runner.discover_executors',return_value=registry):runner=MultiAgentRunner(self.root,config)
        store=Store(self.root/'data/tasks.db');self.addCleanup(store.close)
        task={'id':'t','projectId':'p','title':'编写验收清单','status':0,'tags':[]}
        dida=FakeDida(task);service=Service(self.root,config,store,dida,runner);service.sync()
        store.assign('p:t','codebuddy',store.get('p:t')['revision'],task,config,
                     execution_overrides={'effort':'high'})
        def process(argv,**kwargs):
            self.assertIn('workspace-write',str(argv));self.assertEqual(argv[argv.index('--effort')+1],'high')
            self.assertEqual(Path(kwargs['env']['CODEBUDDY_CONFIG_DIR']).name,'.codebuddy')
            (Path(kwargs['cwd'])/'checklist.md').write_text('Checklist\n',encoding='utf-8')
            return json.dumps({'type':'result','subtype':'success','is_error':False,'structured_output':REPORT}),''
        with patch('dispatcher.runner.run_process',side_effect=process),patch.object(runner,'_invoke',return_value=REPORT.copy()) as verify:
            service.execute(store.claim())
        row=store.get('p:t');self.assertEqual(row['state'],'awaiting_review',row.get('error'))
        self.assertEqual(row['result']['execution']['execution_settings']['effort'],'high')
        self.assertIn('codebuddy',Path(row['run_dir']).parts)
        self.assertEqual((dida.comments,dida.completions),(0,0));verify.assert_called_once()


if __name__=='__main__':unittest.main()
