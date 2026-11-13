import json,tempfile,unittest
from pathlib import Path
from dispatcher import execution_timeouts as timeouts
from dispatcher.external import invocation
from dispatcher.runner import codex_arguments,CodexRunner

POLICY={'agent_timeouts':{
 'codebuddy':{'total_seconds':14400,'first_token_seconds':3600,'stream_idle_seconds':3600,'recovery_first_token_seconds':3600},
 'codex':{'total_seconds':14400,'stream_idle_seconds':3600},
 'dsh':{'total_seconds':14400,'stream_idle_seconds':3600},
 'zcode':{'total_seconds':14400,'http_request_seconds':3600,'stream_idle_seconds':3600},
 'antigravity':{'total_seconds':14400},'qoder':{'total_seconds':14400},'workbuddy':{'total_seconds':3600}},
 'workbuddy_transport':'mcp','doubao_transport':'disabled','task_timeout_seconds':600,'codebuddy_model_timeouts':{'hy4free':1800}}

class TimeoutPolicyTests(unittest.TestCase):
 def test_explicit_agent_policy_overrides_old_hy4_cap_without_changing_models(self):
  p=timeouts.resolve(POLICY,'codebuddy',{'model':'hy4free','effort':'high'})
  self.assertEqual(p['total_seconds'],14400);self.assertEqual(p['recovery_first_token_seconds'],3600)
  self.assertEqual(timeouts.resolve({},'codebuddy')['first_token_seconds'],180)
  self.assertEqual(timeouts.resolve({},'codex')['total_seconds'],1800)
 def test_reject_false_maximum_and_inconsistent_or_unsupported_knobs(self):
  for executor,fields in [('codex',{'total_seconds':False}),('qoder',{'first_token_seconds':3600}),
    ('antigravity',{'stream_idle_seconds':3600}),('codebuddy',{'first_token_seconds':999999999999}),
    ('codebuddy',{'total_seconds':60,'stream_idle_seconds':120}),('workbuddy',{'total_seconds':14400})]:
   with self.subTest(executor=executor,fields=fields),self.assertRaises(ValueError):
    timeouts.resolve(dict(POLICY,agent_timeouts={executor:fields}),executor)
 def test_external_native_and_bridge_limits_match(self):
  with tempfile.TemporaryDirectory() as d:
   run=Path(d);ws=run/'workspace';ws.mkdir()
   args,env,_,_=invocation('codebuddy',['node','cb'],ws,run,'test',POLICY,settings={'model':'hy4free','effort':'high'})
   self.assertEqual(env['CODEBUDDY_FIRST_TOKEN_TIMEOUT_MS'],'3600000');self.assertEqual(env['CODEBUDDY_STREAM_TIMEOUT_MS'],'3600000')
   self.assertEqual(env['CODEBUDDY_RECOVERY_FIRST_TOKEN_TIMEOUT_MS'],'3600000');self.assertEqual(env['CODEBUDDY_MAX_RETRIES'],'1')
   args,env,_,_=invocation('antigravity',['agy'],ws,run,'test',POLICY)
   self.assertEqual(args[args.index('--print-timeout')+1],'14400s')
   args,env,_,_=invocation('zcode',['node',str(run/'glm/zcode.cjs')],ws,run,'test',POLICY,settings={'model':'glm-5.3-flash'})
   self.assertEqual(json.loads((run/'zcode-request.json').read_text())['timeout_seconds'],14400)
   self.assertEqual(env['ZCODE_HTTP_TIMEOUT'],'3600000')
   args,env,_,_=invocation('dsh',['node','dsh.js'],ws,run,'test',POLICY)
   patch=json.loads((run/'dsh.patch.json').read_text())
   self.assertEqual(next(p for p in patch if p['id']=='llm-deepseek')['config']['streamIdleTimeoutMs'],3600000)
   self.assertIn('--require',args);self.assertEqual(env['DISPATCHER_STREAM_IDLE_MS'],'3600000')
 def test_timeout_snapshot_cannot_be_silently_changed_after_prepare(self):
  with tempfile.TemporaryDirectory() as d:
   runner=CodexRunner(d,dict(POLICY,codex_command=['codex']))
   row={'payload':{'id':'t','projectId':'p','title':'write notes'},'difficulty':'easy','kind':'document'}
   run=runner.prepare(row)
   try:
    self.assertEqual(row['_execution_limits']['total_seconds'],14400)
    (run/'execution-limits.json').write_text('{}')
    with self.assertRaises(ValueError):timeouts.snapshot(row,run,'codex',POLICY,{})
   finally:runner.release_workspace(row)

if __name__=='__main__':unittest.main()
