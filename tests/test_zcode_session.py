import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from dispatcher import zcode_session as bridge


FAKE_SERVER = r'''
import json,sys
scenario=sys.argv[1]
sid='sess_dispatcher_test'
selection={'providerId':'local-provider','modelId':'glm-test'}
effort='high'
started=False
def snap():
    current=dict(selection,options={'reasoningLevel':effort})
    return {'session':{'sessionId':sid,'status':'completed' if started else 'idle'},
      'settings':{'model':{'current':current,'available':[
        {'ref':{'providerId':'local-provider','modelId':'glm-test'},'label':'Test',
         'reasoning':{'levels':[{'value':'low'},{'value':'high'}],'defaultLevel':'high'}}]},
        'thoughtLevel':{'current':effort,'available':[{'value':'low'},{'value':'high'}]}},
      'projection':{'status':'completed' if started else 'idle','pendingPermissions':[],
        'activeToolCalls':[],'backgroundJobs':[]},
      'runtime':{'stateRevision':2,'eventSeq':4 if started else 0,'pendingRequestIds':[]},
      'messages':[{'info':{'role':'assistant','finish':'stop','model':current,'time':{'completed':1}},
        'parts':[{'type':'text','text':'{"status":"complete","summary":"ok","artifacts":["probe.txt"],"checks":["read"],"needs_input":""}'}]}] if started else []}
for line in sys.stdin:
    req=json.loads(line)
    method=req.get('method');p=req.get('params',{})
    if method=='session/create':
        assert p['mode']=='edit'
        assert p['toolAllowlist']==['Read','Write','Edit','Glob','Grep']
        assert p['dynamicWorkflowMode']=='disabled'
        assert p['titleGenerationEnabled'] is False
        if scenario=='disconnect':sys.exit(0)
        if scenario=='timeout':
            import time;time.sleep(60)
        result=snap()
        if scenario=='catalog_private':
            result['settings']['model']['current']['apiKey']='secret-key-example'
            result['settings']['model']['current']['options']['headers']={'Authorization':'secret-token-example'}
        if scenario=='ambiguous':
            result['settings']['model']['available'].append({'ref':{'providerId':'second','modelId':'glm-test'},'label':'Other'})
    elif method=='session/setModel':
        if scenario=='rpc_error':
            print(json.dumps({'id':req['id'],'error':{'code':-32603,'message':'secret-token-example'}}),flush=True);continue
        if scenario=='requires_reasoning' and not p['model'].get('options',{}).get('reasoningLevel'):
            print(json.dumps({'id':req['id'],'error':{'code':-32603,'message':'Reasoning level is required'}}),flush=True);continue
        selection=p['model']
        if scenario!='ignored_effort':effort=selection.get('options',{}).get('reasoningLevel',effort)
        if scenario=='ignored_inherited_effort':effort='low'
        result=snap()
        if scenario=='mismatch':result['settings']['model']['current']['modelId']='wrong-model'
    elif method=='session/setThoughtLevel':
        if scenario!='ignored_effort':effort=p['thoughtLevel']
        result=snap()
    elif method=='session/send':
        if scenario=='permission':
            print(json.dumps({'id':'server-1','method':'interaction/requestPermission','params':{'toolName':'Bash'}}),flush=True)
        started=True;result={'sessionId':sid,'accepted':True,'stateRevision':3}
    elif method=='session/events':
        events=[{'seq':1,'sessionId':sid,'turnId':'turn-one','type':'turn.started','payload':{}},
                {'seq':2,'sessionId':sid,'turnId':'turn-one','type':'turn.completed','payload':{'resultType':'success'}}]
        if scenario.startswith('terminal_'):
            terminal=scenario.removeprefix('terminal_')
            events[-1]['payload']={} if terminal=='missing' else {'resultType':terminal}
        if scenario=='failed':events[-1]['type']='turn.failed'
        if scenario=='denied_tool':
            events[-1]['seq']=3
            events.insert(1,{'seq':2,'sessionId':sid,'turnId':'turn-one','type':'tool.updated','payload':{'toolName':'Bash','kind':'started'}})
        if scenario=='nested_tool_failure':
            events[-1]['seq']=3
            events.insert(1,{'seq':2,'sessionId':sid,'turnId':'turn-one','type':'tool.updated','payload':{'toolName':'Read','kind':'result','result':{'success':False}}})
        result={'events':events}
    elif method=='session/read':
        result=snap()
        if scenario=='nonterminal':result['runtime']['activeTurnId']='turn-one'
        if scenario=='failed_tool':result['messages'][0]['parts'].insert(0,{'type':'tool','tool':'Read','state':{'status':'error'}})
        if scenario=='truncated':result['messages'][0]['info']['finish']='length'
    elif method=='session/close':result={'closed':True}
    else:result={}
    if 'id' in req: print(json.dumps({'id':req['id'],'result':result}),flush=True)
'''


class ZCodeSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.server=self.root/'server.py'
        self.server.write_text(FAKE_SERVER,encoding='utf-8')
        self.request={'workspace':str(self.root),'prompt':'Create probe.txt',
                      'model':'glm-test','effort':'low','timeout_seconds':3}

    def tearDown(self):
        self.temp.cleanup()

    def call(self, scenario='ok', **request):
        return bridge.run_session([sys.executable,str(self.server),scenario],dict(self.request,**request),append_server_args=False)

    def test_real_ndjson_process_applies_model_effort_and_returns_compatible_report(self):
        result=self.call()
        self.assertEqual(result['sessionId'],'sess_dispatcher_test')
        self.assertEqual(json.loads(result['response'])['status'],'complete')
        self.assertEqual(result['effective_configuration'],{'provider':'local-provider','model':'glm-test','effort':'low'})

    def test_catalog_filters_to_nonsecret_model_metadata(self):
        result=self.call(catalog_only=True)
        self.assertEqual(result['models'][0]['id'],'local-provider/glm-test')
        self.assertEqual(result['models'][0]['efforts'],['low','high'])
        self.assertNotIn('messages',result)

    def test_catalog_current_selection_drops_unknown_potential_secret_fields(self):
        result=self.call('catalog_private',catalog_only=True)
        self.assertEqual(result['current'],{'providerId':'local-provider','modelId':'glm-test','options':{'reasoningLevel':'high'}})
        self.assertNotIn('secret-',json.dumps(result))

    def test_unknown_model_rejected_before_prompt(self):
        with self.assertRaisesRegex(bridge.ProtocolError,'model_unavailable'):
            self.call(model='unknown')

    def test_ambiguous_model_rejected(self):
        with self.assertRaisesRegex(bridge.ProtocolError,'model_ambiguous'):
            self.call('ambiguous')

    def test_qualified_model_selects_exact_provider(self):
        self.assertEqual(self.call('ambiguous',model='local-provider/glm-test')['effective_configuration']['provider'],'local-provider')

    def test_model_selection_carries_required_reasoning_option(self):
        self.assertEqual(self.call('requires_reasoning')['effective_configuration']['effort'],'low')

    def test_inherited_effort_still_must_match_selected_configuration(self):
        with self.assertRaisesRegex(bridge.ProtocolError,'configuration_mismatch'):
            self.call('ignored_inherited_effort',effort=None)

    def test_unsupported_effort_rejected(self):
        with self.assertRaisesRegex(bridge.ProtocolError,'effort_unavailable'):
            self.call(effort='max')

    def test_silently_ignored_effort_rejected(self):
        with self.assertRaisesRegex(bridge.ProtocolError,'configuration_mismatch'):
            self.call('ignored_effort')

    def test_model_mismatch_rejected(self):
        with self.assertRaisesRegex(bridge.ProtocolError,'configuration_mismatch'):
            self.call('mismatch')

    def test_rpc_errors_name_phase_without_exposing_vendor_text(self):
        with self.assertRaisesRegex(bridge.ProtocolError,'vendor_rpc_error_session_setModel_-32603') as caught:
            self.call('rpc_error')
        self.assertNotIn('secret-token-example',str(caught.exception))

    def test_permission_tool_error_failure_disconnect_and_incomplete_rejected(self):
        for scenario in ['permission','denied_tool','failed','disconnect','nonterminal','failed_tool','truncated']:
            with self.subTest(scenario=scenario), self.assertRaises(bridge.ProtocolError):
                self.call(scenario)

    def test_disallowed_tool_with_valid_event_sequence_is_rejected(self):
        with self.assertRaisesRegex(bridge.ProtocolError,'tool_failed_or_disallowed'):
            self.call('denied_tool')

    def test_nested_unsuccessful_tool_result_is_rejected(self):
        with self.assertRaisesRegex(bridge.ProtocolError,'tool_failed_or_disallowed'):
            self.call('nested_tool_failure')

    def test_timeout_terminates_owned_process(self):
        with self.assertRaisesRegex(bridge.ProtocolError,'timeout'):
            self.call('timeout',timeout_seconds=.2)

    def test_completed_event_requires_explicit_success_result_type(self):
        for status in ['cancelled','error_max_turns','error_max_budget','error_during_execution','error_max_tool_calls','missing']:
            with self.subTest(status=status),self.assertRaisesRegex(bridge.ProtocolError,'unsuccessful_terminal_result'):
                self.call('terminal_'+status)

    def test_metadata_never_invents_unknown_reasoning_levels(self):
        self.assertEqual(bridge.model_catalog({'settings':{'model':{'available':[
            {'ref':{'providerId':'p','modelId':'m'},'label':'M'}]}}})[0]['efforts'],[])


if __name__=='__main__':
    unittest.main()
