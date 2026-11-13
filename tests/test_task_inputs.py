import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from dispatcher.core import Store, StateConflict, manifest_digest
from dispatcher.control import Controller
from dispatcher.runner import CodexRunner
from dispatcher.service import Service
from dispatcher.workspace import WorkspaceManager
from tests.test_service import FakeDida
from tests.test_answer_results import ANSWER


class TaskInputTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=Store(self.root/'data/tasks.db')
        self.source={'id':'t','projectId':'p','title':'请回答预算问题，不需要改文件','content':'预算300元，费用200元。','status':0,'tags':['AI']}
        self.dida=FakeDida(copy.deepcopy(self.source))
        self.config={'model':'gpt-6.1-sol','require_human_review':True,'write_comments':True,'agent_settings':{'codex':{'model':'gpt-6.1-sol','effort':'low'}}}
        self.runner=CodexRunner(self.root,self.config)
        self.service=Service(self.root,self.config,self.store,self.dida,self.runner)
        self.controller=Controller(self.service,WorkspaceManager(self.root,self.store),[{'name':'codex','enabled':True},{'name':'codebuddy','enabled':True}])
        self.store.sync([self.source],{})
        self.prompts=[]

    def run_answer(self, report=None):
        def invoke(run,prompt,phase,*args,**kwargs):
            self.prompts.append((phase,prompt))
            if phase=='execution':return copy.deepcopy(report or ANSWER)
            return {'status':'complete','summary':'checked','checks':['核对全部输入'],'needs_input':'','artifacts':[]}
        with patch.object(self.runner,'_invoke',side_effect=invoke):self.service.execute(self.store.claim())
        return self.store.get('p:t')

    def submit(self,row,action='follow_up',key='next',**changes):
        body={'action':action,'request_id':key,'task_id':row['id'],'revision':row['revision'],
              'run_id':row['current_run_id'],'fingerprint':row['fingerprint'],'message':'追加要求：预算改为350元，请重新计算。','answers':{}}
        body.update(changes)
        return self.controller.submit(body)

    def execute_command(self,command):
        self.controller.process_pending()
        result=self.store.get_command(command['id'])
        self.assertEqual(result['status'],'done',result.get('error'))
        return self.store.get('p:t')

    def test_followup_preserves_history_settings_and_binds_new_answer(self):
        first=self.run_answer();old=copy.deepcopy(self.store.get_run(first['current_run_id']))
        command=self.submit(first)
        self.assertEqual(self.submit(first)['id'],command['id'])
        queued=self.execute_command(command)
        self.assertEqual(queued['state'],'queued');self.assertEqual(queued['input_version'],2)
        self.assertEqual(self.store.get_run(old['id']),old)
        self.store.set_agent_settings('codex',{'model':'gpt-6-astra','effort':'high'},0)
        second=self.run_answer(dict(ANSWER,answer_markdown='350-200=150元'))
        self.assertEqual(second['state'],'awaiting_review',second.get('error'))
        run=self.store.get_run(second['current_run_id'])
        self.assertEqual(run['parent_run_id'],old['id']);self.assertEqual(run['input_version'],2)
        self.assertEqual(run['execution_settings']['model'],'gpt-6.1-sol')
        self.assertEqual(run['execution_settings']['effort'],'low')
        self.assertEqual(self.store.get_run(old['id']),old)
        for phase in ('execution','verification'):
            self.assertIn('350',next(prompt for p,prompt in reversed(self.prompts) if p==phase))
        from dispatcher.result_receipt import check_answer_receipt
        self.assertTrue(check_answer_receipt(self.root,second))
        bad=copy.deepcopy(second);bad['input_context']['messages'][-1]['message']='changed'
        with self.assertRaises(ValueError):check_answer_receipt(self.root,bad)
        with self.assertRaises(StateConflict):
            self.store.decide_review('p:t',second['revision'],old['id'],first['fingerprint'],manifest_digest(first['result']),True)
        self.assertEqual(self.dida.completions,0)

    def test_concurrent_messages_accept_only_one_and_stale_source_rejected(self):
        first=self.run_answer()
        commands=[self.submit(first,key='one'),self.submit(first,key='two',message='另一个要求')]
        self.controller.process_pending()
        self.assertEqual(sorted(self.store.get_command(c['id'])['status'] for c in commands),['done','failed'])
        self.assertEqual(len(self.store.list_task_inputs('p:t')),1)
        second=self.run_answer();self.dida.task['content']='upstream changed'
        c=self.submit(second,key='stale');self.controller.process_pending()
        self.assertEqual(self.store.get_command(c['id'])['http_status'],409)
        self.assertEqual(len(self.store.list_task_inputs('p:t')),1)

    def test_questions_require_matching_ids_and_required_answers(self):
        report=dict(ANSWER,status='needs_input',needs_input='请给出预算',input_requests=[{'id':'budget','prompt':'新的预算是多少？','options':['300','350'],'required':True}])
        row=self.run_answer(report)
        self.assertEqual(row['state'],'needs_input')
        for answers in ({},{'unknown':'350'}):
            with self.assertRaises(ValueError):self.submit(row,action='respond_input',answers=answers,message='')
        c=self.submit(row,action='respond_input',answers={'budget':'350'},message='用于活动甲')
        self.execute_command(c)
        second=self.run_answer();self.assertEqual(second['state'],'awaiting_review')
        self.assertIn('350',self.prompts[-1][1])

    def test_drafts_persist_without_dispatch_and_reject_stale_overwrite(self):
        row=self.run_answer();run_id=row['current_run_id']
        c=self.submit(row,action='save_input_draft',draft_revision=0)
        saved=self.execute_command(c)
        self.assertEqual(saved['state'],'awaiting_review');self.assertEqual(saved['revision'],row['revision'])
        draft=self.controller.detail('p:t')['input_draft'];self.assertEqual(draft['revision'],1)
        self.assertIn('350',draft['message']);self.assertEqual(len(self.store.list_runs('p:t')),1)
        c=self.submit(row,action='save_input_draft',key='stale-draft',draft_revision=0,message='overwrite')
        self.controller.process_pending();self.assertEqual(self.store.get_command(c['id'])['http_status'],409)
        self.assertEqual(self.controller.detail('p:t')['input_draft']['message'],draft['message'])

    def test_submit_cannot_discard_a_newer_draft_from_another_device(self):
        row=self.run_answer()
        self.execute_command(self.submit(row,action='save_input_draft',key='draft',draft_revision=0))
        stale=self.submit(row,key='stale-submit',draft_revision=0)
        self.controller.process_pending()
        self.assertEqual(self.store.get_command(stale['id'])['http_status'],409)
        self.assertEqual(self.store.get('p:t')['state'],'awaiting_review')
        self.assertEqual(self.controller.detail('p:t')['input_draft']['revision'],1)
        fresh=self.submit(row,key='fresh-submit',draft_revision=1)
        self.execute_command(fresh)
        self.assertEqual(len(self.store.list_task_inputs('p:t')),1)

    def test_completed_task_followup_never_comments_or_completes_source(self):
        first=self.run_answer()
        self.store.decide_review('p:t',first['revision'],first['current_run_id'],first['fingerprint'],manifest_digest(first['result']),True)
        self.service.process_writebacks();done=self.store.get('p:t')
        self.assertEqual(done['state'],'done');calls=(self.dida.comments,self.dida.completions)
        old=copy.deepcopy(self.store.get_run(done['current_run_id']))
        c=self.submit(done,message='只解释为什么要先扣除已支出金额。')
        queued=self.execute_command(c)
        self.assertFalse(queued['source_writeback_allowed'])
        self.store.sync([self.dida.task],self.config)
        self.assertEqual(self.store.get('p:t')['state'],'queued')
        reply=self.run_answer();self.assertEqual(reply['state'],'awaiting_review',reply.get('error'))
        public=self.controller.detail('p:t')['task']
        self.assertTrue(public['can_approve']);self.assertFalse(public['source_writeback_allowed'])
        self.store.decide_review('p:t',reply['revision'],reply['current_run_id'],reply['fingerprint'],manifest_digest(reply['result']),True)
        self.service.process_writebacks()
        self.assertEqual(self.store.get('p:t')['state'],'done')
        self.assertEqual((self.dida.comments,self.dida.completions),calls)
        self.assertEqual(self.store.get_run(old['id']),old)

    def test_sensitive_input_and_running_submission_are_rejected(self):
        row=self.run_answer()
        with self.assertRaises(ValueError):self.submit(row,message='api_key=sk-test-secret-value-123456789')
        self.execute_command(self.submit(row));running=self.store.claim()
        with self.assertRaises((ValueError,StateConflict)):self.submit(running,key='busy')

    def test_ack_replay_and_rejected_review_are_retained(self):
        first=self.run_answer()
        rejected=self.store.decide_review('p:t',first['revision'],first['current_run_id'],first['fingerprint'],manifest_digest(first['result']),False,note='请增加新预算的算式')
        old=copy.deepcopy(self.store.get_run(first['current_run_id']))
        command=self.submit(rejected);self.execute_command(command)
        replay=self.submit(rejected)
        self.assertEqual(replay['id'],command['id']);self.assertEqual(replay['status'],'done')
        self.assertEqual(len(self.store.list_task_inputs('p:t')),1)
        self.run_answer()
        self.assertEqual(self.store.get_run(old['id']),old)
        self.assertIn('请增加新预算的算式',self.prompts[-1][1])

    def test_source_completion_cancels_queued_supplement_without_changing_parent(self):
        first=self.run_answer();old=copy.deepcopy(self.store.get_run(first['current_run_id']))
        self.execute_command(self.submit(first));self.dida.task['status']=2
        self.store.sync([self.dida.task],self.config)
        self.assertEqual(self.store.get('p:t')['state'],'cancelled')
        self.assertIsNone(self.store.claim());self.assertEqual(self.store.get_run(old['id']),old)

    def test_completed_reply_reopened_upstream_requires_explicit_assignment(self):
        first=self.run_answer()
        self.store.decide_review('p:t',first['revision'],first['current_run_id'],first['fingerprint'],manifest_digest(first['result']),True)
        self.service.process_writebacks();done=self.store.get('p:t')
        self.execute_command(self.submit(done));reply=self.run_answer()
        self.store.decide_review('p:t',reply['revision'],reply['current_run_id'],reply['fingerprint'],manifest_digest(reply['result']),True)
        self.service.process_writebacks();old=copy.deepcopy(self.store.get_run(reply['current_run_id']))
        self.dida.task['status']=0;self.store.sync([self.dida.task],self.config)
        current=self.store.get('p:t')
        self.assertEqual(current['state'],'cancelled');self.assertTrue(current['paused'])
        self.assertEqual(self.store.get_run(old['id']),old)
        with self.assertRaises(ValueError):self.submit(current,key='invalid-local-reply')
        self.assertTrue(self.controller.task(current)['can_assign'])

    def test_input_prose_stays_out_of_list_and_selected_history_has_its_input(self):
        first=self.run_answer();self.execute_command(self.submit(first));second=self.run_answer()
        listing=json.dumps(self.controller.state(),ensure_ascii=False)
        self.assertNotIn('追加要求：预算改为350元',listing)
        detail=self.controller.detail('p:t')
        current=next(run for run in detail['runs'] if run['id']==second['current_run_id'])
        self.assertEqual(current['round_number'],2)
        self.assertIn('350',current['round_input']['message'])
        older=self.controller.detail('p:t',run_id=first['current_run_id'])
        old=next(run for run in older['runs'] if run['id']==first['current_run_id'])
        self.assertEqual(old['delivery']['answer_markdown'],ANSWER['answer_markdown'])
        self.assertIsNone(old['round_input'])

    def test_rejected_completed_source_reply_can_be_revised_without_source_writes(self):
        first=self.run_answer()
        self.store.decide_review('p:t',first['revision'],first['current_run_id'],first['fingerprint'],manifest_digest(first['result']),True)
        self.service.process_writebacks();done=self.store.get('p:t')
        self.execute_command(self.submit(done));reply=self.run_answer()
        rejected=self.store.decide_review('p:t',reply['revision'],reply['current_run_id'],reply['fingerprint'],manifest_digest(reply['result']),False,note='再解释清楚一点')
        old=copy.deepcopy(self.store.get_run(reply['current_run_id']))
        self.execute_command(self.submit(rejected,key='revise'))
        revised=self.run_answer()
        self.assertEqual(revised['state'],'awaiting_review',revised.get('error'))
        self.assertFalse(revised['source_writeback_allowed'])
        self.assertEqual(self.store.get_run(old['id']),old)
        self.assertEqual(self.dida.completions,1)


if __name__=='__main__':unittest.main()
