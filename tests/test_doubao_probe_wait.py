"""Segmented Doubao probe evidence; fake broker time, no model or production data."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dispatcher import workbuddy_probe as probe
from tests.test_workbuddy_probe import FakeBroker


class SegmentedBroker(FakeBroker):
    provider='doubao'

    def __init__(self,epoch):
        super().__init__();self.epoch=epoch
        self.value.update(provider='doubao',wait_window={'window_id':'a'*32,
            'window_started':100.0,'deadline':1000.0,'segment_count':1,'active_seconds':0.0,
            'phase':'active','gap_deadline':None,'close_reason':None})

    def status(self):
        value=copy.deepcopy(self.value);window=value.pop('wait_window')
        names={'window_id':'wait_window_id','window_started':'wait_window_started',
               'deadline':'wait_window_deadline','segment_count':'wait_segment_count',
               'active_seconds':'wait_active_seconds','phase':'wait_phase',
               'gap_deadline':'wait_gap_deadline','close_reason':'wait_end_reason'}
        value.update({names[key]:item for key,item in window.items()})
        value.update(wait_policy='segmented',epoch=self.epoch,
                     wait_gap_seconds=max(0,15-(window['gap_deadline']-self.clock()))
                         if window['phase']=='gap' and window['gap_deadline'] is not None else 0)
        return value

    def _bind_probe_wait(self,run_id,wait_calls,connection_generation,*,window_id=None):
        super()._bind_probe_wait(run_id,wait_calls,connection_generation)
        self.bindings[run_id]['window_id']=window_id


class DoubaoProbeWaitTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve();self.counter=0
        self.reset()

    def reset(self,*,start=True):
        self.counter+=1;self.root=self.base/str(self.counter)
        (self.root/'workspaces').mkdir(parents=True)
        self.now=[100.0];self.epoch=('%064x'%self.counter)
        self.broker=SegmentedBroker(self.epoch)
        self.broker.clock=lambda:self.now[0]
        self.inbox=probe.ProbeInbox(self.root,self.epoch,self.broker,provider='doubao',clock=lambda:self.now[0])
        self.request=probe.request_probe(self.root,provider='doubao')
        if start:self.tick()

    def result(self):return probe.get_probe_result(self.root,self.request,provider='doubao')

    def tick(self):self.inbox.tick();return self.result()

    def progress(self,seconds,active):
        self.now[0]+=seconds
        self.broker.value['wait_window']['active_seconds']=active
        return self.tick()

    def gap(self):
        self.broker.value.update(ready=False,wait_active=False)
        self.broker.value['wait_window'].update(phase='gap',gap_deadline=self.now[0]+15,close_reason='idle_continue')
        return self.tick()

    def resume(self,gap=5):
        self.now[0]+=gap
        self.broker.value.update(ready=True,wait_active=True,wait_calls=self.broker.value['wait_calls']+1)
        self.broker.value['wait_window'].update(phase='active',
            segment_count=self.broker.value['wait_window']['segment_count']+1,gap_deadline=None,close_reason=None)
        return self.tick()

    def reach_second(self):
        self.progress(40,40);self.gap();self.resume()

    def complete(self,body=b'READY\n',claim_changes=None):
        run_id,workspace,_,_=self.broker.submissions[0]
        (workspace/'probe.txt').write_bytes(body)
        expected=copy.deepcopy(self.broker.bindings[run_id])
        actual=dict(expected,**(claim_changes or {}))
        self.broker.results[run_id]={'run_id':run_id,'state':'submitted',
            'submission':{'status':'complete','summary':'fixed','artifacts':['probe.txt'],
                          'checks':['model claim only'],'needs_input':''},
            'artifact_sha256':{'probe.txt':hashlib.sha256(body).hexdigest()},
            'claim_evidence':{'expected':expected,'actual':actual,'matched':True,
                              'preempted':False,'wait_active':True}}
        return self.tick()

    def test_two_segments_require_seventy_actual_active_seconds_and_bound_claim(self):
        self.progress(40,40)
        self.assertEqual(self.gap()['state'],'waiting')
        self.assertEqual(self.resume()['state'],'waiting')
        self.assertEqual(self.progress(29,69)['state'],'waiting')
        self.assertFalse(self.broker.submissions)
        self.assertEqual(self.progress(1,70)['state'],'running')
        binding=next(iter(self.broker.bindings.values()))
        self.assertEqual(binding,{'wait_calls':2,'connection_generation':1,'window_id':'a'*32})
        result=self.complete()
        self.assertEqual(result['state'],'passed')
        evidence=result['wait_evidence']
        self.assertEqual(evidence['wait_policy'],'segmented')
        self.assertEqual(evidence['active_seconds'],70)
        self.assertEqual(evidence['segment_count'],2)
        self.assertNotIn('idle_wait_seconds',evidence)
        self.assertNotIn('host_token',json.dumps(result))

    def test_gap_does_not_count_toward_active_time(self):
        self.progress(40,40);self.gap();self.resume(gap=14)
        self.progress(16,56)
        self.assertEqual(self.now[0],170)
        self.assertFalse(self.broker.submissions)
        self.assertEqual(self.progress(14,70)['state'],'running')

    def test_previous_window_progress_is_not_reused_for_a_new_probe(self):
        # Request first observed in segment 3 must still observe its own 70s.
        self.reset(start=False)
        self.now[0]=190
        self.broker.value.update(wait_calls=3)
        self.broker.value['wait_window'].update(segment_count=3,active_seconds=80)
        self.tick()
        self.progress(40,120);self.gap();self.resume()
        self.progress(29,149)
        self.assertFalse(self.broker.submissions)
        result=self.progress(1,150)
        self.assertEqual(result['state'],'running')
        self.assertEqual(result['wait_evidence']['active_seconds'],70)
        self.assertEqual(result['wait_evidence']['segment_count'],2)

    def test_probe_armed_in_gap_starts_its_own_baseline_in_next_active_segment(self):
        self.reset(start=False);self.now[0]=145
        self.broker.value.update(ready=False,wait_active=False)
        self.broker.value['wait_window'].update(phase='gap',active_seconds=40,
            gap_deadline=155,close_reason='idle_continue')
        self.assertEqual(self.tick()['state'],'waiting')
        self.resume()
        self.progress(40,80);self.gap();self.resume()
        self.progress(29,109)
        self.assertFalse(self.broker.submissions)
        self.assertEqual(self.progress(1,110)['wait_evidence']['active_seconds'],70)

    def test_ended_segment_before_stdio_ack_is_not_a_false_cancellation(self):
        self.progress(40,40)
        self.broker.value['ready']=False
        self.assertEqual(self.tick()['state'],'waiting')
        self.assertEqual(self.gap()['state'],'waiting')
        self.resume()
        self.assertEqual(self.progress(30,70)['state'],'running')

    def test_wall_time_or_single_segment_cannot_satisfy_probe(self):
        self.now[0]+=70
        self.tick()
        self.assertFalse(self.broker.submissions)
        self.broker.value['wait_window']['active_seconds']=70
        self.tick()
        self.assertFalse(self.broker.submissions)

    def test_cancel_disconnect_generation_window_and_provider_changes_fail_closed(self):
        variants=[{'online':False},{'connection_generation':2},{'provider':'workbuddy'},
                  {'active_run_id':'production'},{'queued_count':1}]
        for changes in variants:
            with self.subTest(changes=changes):
                self.reset();self.progress(10,10);self.broker.value.update(changes)
                self.assertEqual(self.tick()['state'],'failed')
                self.assertFalse(self.broker.submissions)
        for changes in ({'window_id':'b'*32},{'window_started':101.0},{'deadline':1001.0},
                        {'phase':'closed','close_reason':'cancelled'},
                        {'phase':'closed','close_reason':'gap_timeout'},
                        {'phase':'closed','close_reason':'idle_timeout'}):
            with self.subTest(window=changes):
                self.reset();self.progress(10,10);self.broker.value['wait_window'].update(changes)
                self.assertEqual(self.tick()['state'],'failed')
                self.assertFalse(self.broker.submissions)

    def test_gap_expiry_and_resume_without_a_normal_gap_fail_closed(self):
        self.progress(40,40);self.gap();self.now[0]+=15
        self.assertEqual(self.tick()['state'],'failed')

    def test_gap_requires_completed_forty_second_segment_and_frozen_active_counter(self):
        self.progress(10,10)
        self.assertEqual(self.gap()['state'],'failed')
        self.reset();self.progress(40,40);self.gap();self.now[0]+=1
        self.broker.value['wait_window']['active_seconds']=41
        self.assertEqual(self.tick()['state'],'failed')
        self.reset();self.progress(10,10)
        self.broker.value['wait_window'].update(phase='gap',gap_deadline=None)
        self.broker.value.update(ready=False,wait_active=False)
        self.assertEqual(self.tick()['state'],'failed')

    def test_invalid_or_regressing_active_metrics_do_not_dispatch(self):
        for changes in ({'active_seconds':float('nan')},{'active_seconds':-1},
                        {'segment_count':0},{'segment_count':25},{'phase':'other'}):
            with self.subTest(changes=changes):
                self.reset();self.broker.value['wait_window'].update(changes)
                self.assertEqual(self.tick()['state'],'failed')
        self.reset();self.progress(10,10)
        self.broker.value['wait_window']['active_seconds']=9
        self.assertEqual(self.tick()['state'],'failed')

    def test_final_submission_rechecks_active_segment_under_broker_lock(self):
        self.reach_second();self.now[0]+=30
        self.broker.value['wait_window']['active_seconds']=70
        original=self.broker.status;calls=[]
        def raced():
            calls.append(True)
            if len(calls)==2:
                self.assertTrue(self.broker._lock._is_owned())
                self.broker.value['wait_window'].update(phase='gap',gap_deadline=self.now[0]+15)
                self.broker.value.update(ready=False,wait_active=False)
            return original()
        self.broker.status=raced
        self.assertEqual(self.tick()['state'],'failed')
        self.assertFalse(self.broker.submissions)

    def test_claimed_other_window_or_other_segment_is_not_accepted(self):
        for changes in ({'window_id':'b'*32},{'wait_calls':3},{'connection_generation':2}):
            with self.subTest(changes=changes):
                self.reset();self.reach_second();self.progress(30,70)
                self.assertEqual(self.complete(claim_changes=changes)['state'],'failed')

    def test_exact_newline_requirement_remains_unchanged(self):
        self.reach_second();self.progress(30,70)
        self.assertEqual(self.complete(body=b'READY')['state'],'failed')

    def test_failed_receipt_has_only_sanitized_end_status(self):
        self.broker.value.update(token='PRIVATE_TOKEN',secret='PRIVATE_TOKEN',prompt='PRIVATE_TOKEN')
        self.broker.value['wait_window'].update(phase='closed',close_reason='cancelled')
        result=self.tick()
        self.assertEqual(result['state'],'failed')
        self.assertEqual(result['end_status']['wait_end_reason'],'cancelled')
        self.assertNotIn('PRIVATE_TOKEN',json.dumps(result))

    def test_failure_reason_is_fixed_and_existing_job_is_cancelled(self):
        self.reach_second();self.progress(30,70)
        run_id=self.broker.submissions[0][0]
        with patch.object(self.broker,'result',side_effect=probe.ProbeError('claim_wait_mismatch')):
            result=self.tick()
        self.assertEqual(result['reason'],'claim_wait_mismatch')
        self.assertIn(run_id,self.broker.cancelled)
        self.assertIn('end_status',result)
        self.reset()
        with patch.object(self.inbox,'_tick',side_effect=probe.ProbeError('PRIVATE_TOKEN')):
            result=self.tick()
        self.assertEqual(result['reason'],'probe_validation_failed')
        self.assertNotIn('PRIVATE_TOKEN',json.dumps(result))

    def test_window_and_gap_failures_retain_specific_safe_reason(self):
        self.broker.value['wait_window'].update(phase='closed',close_reason='cancelled')
        self.assertEqual(self.tick()['reason'],'wait_window_closed')
        self.reset();self.progress(40,40);self.gap();self.now[0]+=15
        self.assertEqual(self.tick()['reason'],'wait_gap_invalid')

    def test_real_broker_prearmed_probe_crosses_two_active_segments(self):
        from dispatcher.workbuddy_mcp import WorkBuddyBroker,BridgeClient,TEXT_FRAME_PREFIX,TEXT_FRAME_SUFFIX
        root=self.base/'real-broker';(root/'workspaces').mkdir(parents=True)
        clock=[100.0]
        # Avoid a concurrent watcher tick; all real protocol and locks remain.
        with patch.object(WorkBuddyBroker,'_watch',lambda broker:broker._stopped.wait()):
            broker=WorkBuddyBroker(root,provider='doubao',clock=lambda:clock[0],heartbeat_timeout=120).start()
        self.addCleanup(broker.close)
        client=BridgeClient(root,provider='doubao');self.addCleanup(client.close)
        identifier=probe.request_probe(root,provider='doubao')
        inbox=broker._probe_inbox
        inbox.tick()
        self.assertEqual(probe.get_probe_result(root,identifier,provider='doubao')['state'],'waiting')
        self.assertEqual(broker.status()['wait_end_reason'],'not_started')
        client.request('wait_started',progress_token_seen=True);client.request('poll');inbox.tick()
        clock[0]+=40
        self.assertFalse(broker.status()['ready'])
        inbox.tick()
        self.assertEqual(client.request('wait_segment_end')['state'],'idle_continue')
        inbox.tick();clock[0]+=5
        client.request('wait_started',progress_token_seen=True);client.request('poll');inbox.tick()
        clock[0]+=30;inbox.tick()
        task=client.request('poll')['task']
        self.assertEqual(task['run_id'],'probe-'+identifier)
        nonce=task['lease_nonce']
        client.request('write_file',lease_nonce=nonce,path='probe.txt',text_format='framed',
            text=TEXT_FRAME_PREFIX+'READY\n'+TEXT_FRAME_SUFFIX)
        client.request('publish_result',lease_nonce=nonce,status='complete',summary='test',
            artifacts=['probe.txt'],checks=['fixture bytes'],needs_input='')
        inbox.tick()
        result=probe.get_probe_result(root,identifier,provider='doubao')
        self.assertEqual(result['state'],'passed')
        self.assertEqual(result['wait_evidence']['active_seconds'],70)
        self.assertEqual(result['wait_evidence']['segment_count'],2)


if __name__=='__main__':unittest.main()
