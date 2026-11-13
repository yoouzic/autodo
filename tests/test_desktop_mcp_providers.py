"""Provider namespace isolation; local fixtures and stdio, no desktop models."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from dispatcher.workbuddy_mcp import WorkBuddyBroker, BridgeClient, BridgeError
from dispatcher.workbuddy_probe import request_probe, get_probe_result, ProbeError


class DesktopMCPProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'dispatcher'
        self.a=self.root/'workspaces/one/workspace'
        self.b=self.root/'workspaces/two/workspace'
        self.a.mkdir(parents=True);self.b.mkdir(parents=True)

    def brokers(self):
        first=WorkBuddyBroker(self.root).start();self.addCleanup(first.close)
        second=WorkBuddyBroker(self.root,provider='doubao').start();self.addCleanup(second.close)
        return first,second

    def clients(self):
        first=BridgeClient(self.root);self.addCleanup(first.close)
        second=BridgeClient(self.root,provider='doubao');self.addCleanup(second.close)
        return first,second

    def test_two_providers_have_independent_credentials_jobs_leases_and_shutdown(self):
        first,second=self.brokers();a,b=self.clients()
        self.assertNotEqual(a.token,b.token);self.assertNotEqual(a.epoch,b.epoch)
        for provider in ('workbuddy','doubao'):
            descriptor=json.loads((self.root/f'data/{provider}-bridge.json').read_text())
            self.assertEqual(descriptor['provider'],provider)
            self.assertTrue((self.root/f'data/{provider}-bridge.lock').is_file())
        first.submit('same-id',self.a,'first task')
        b.request('wait_started',progress_token_seen=False);b.request('poll')
        second.submit('same-id',self.b,'second task')
        one=a.request('poll')['task'];two=b.request('poll')['task']
        self.assertEqual(one['prompt'],'first task');self.assertEqual(two['prompt'],'second task')
        self.assertNotEqual(one['lease_nonce'],two['lease_nonce'])
        with self.assertRaises(BridgeError):
            b.request('write_file',lease_nonce=one['lease_nonce'],path='bad.txt',text='no')
        first.cancel('same-id');first.close()
        self.assertEqual(second.result('same-id')['state'],'leased')
        b.request('write_file',lease_nonce=two['lease_nonce'],path='ok.txt',text='yes')
        self.assertEqual((self.b/'ok.txt').read_text(),'yes')
        self.assertFalse((self.a/'ok.txt').exists())
        self.assertEqual(b.request('heartbeat'),{'state':'alive'})

    def test_provider_header_cannot_be_changed_even_with_correct_other_credentials(self):
        self.brokers();a,b=self.clients()
        b.provider='workbuddy'
        with self.assertRaises(BridgeError):b.request('poll')
        b.provider='doubao'
        b.request('wait_started',progress_token_seen=False)
        self.assertIsNone(b.request('poll')['task'])

    def test_copied_or_legacy_descriptor_cannot_connect_to_the_other_provider(self):
        first,second=self.brokers()
        origin=self.root/'data/workbuddy-bridge.json'
        destination=self.root/'data/doubao-bridge.json'
        value=json.loads(origin.read_text())
        destination.write_text(json.dumps(value))
        with self.assertRaises(BridgeError):BridgeClient(self.root,provider='doubao')
        legacy={key:item for key,item in value.items() if key!='provider'};legacy['version']=1
        destination.write_text(json.dumps(legacy))
        with self.assertRaises(BridgeError):BridgeClient(self.root,provider='doubao')
        self.assertFalse(first.status()['online']);self.assertFalse(second.status()['online'])
        origin.write_text(json.dumps(legacy))
        old=BridgeClient(self.root);self.addCleanup(old.close)
        self.assertEqual(old.request('heartbeat'),{'state':'alive'})

    def test_probe_controls_requests_and_receipts_do_not_cross_namespaces(self):
        first,second=self.brokers();self.clients()
        a=request_probe(self.root);b=request_probe(self.root,provider='doubao')
        self.assertNotEqual(a,b)
        one=json.loads((self.root/'data/workbuddy-probe/control.json').read_text())
        two=json.loads((self.root/'data/doubao-probe/control.json').read_text())
        self.assertNotEqual(one['host_token'],two['host_token'])
        self.assertEqual(one['provider'],'workbuddy');self.assertEqual(two['provider'],'doubao')
        first._probe_inbox.tick();second._probe_inbox.tick()
        self.assertEqual(get_probe_result(self.root,a)['state'],'waiting')
        self.assertEqual(get_probe_result(self.root,b,provider='doubao')['state'],'waiting')
        copied=json.loads((self.root/'data/workbuddy-probe/receipt.json').read_text())
        # Even a matching injected epoch does not make a foreign provider valid.
        copied['epoch']=two['epoch']
        (self.root/'data/doubao-probe/receipt.json').write_text(json.dumps(copied))
        with self.assertRaises(ProbeError):get_probe_result(self.root,a,provider='doubao')
        (self.root/'data/doubao-probe/control.json').write_text(json.dumps(one))
        with self.assertRaises(ProbeError):request_probe(self.root,provider='doubao')

    def test_invalid_provider_never_creates_any_namespace(self):
        for provider in ('', '../other','WorkBuddy','antigravity',None):
            with self.subTest(provider=provider),self.assertRaises(ValueError):
                WorkBuddyBroker(self.root,provider=provider)
            with self.subTest(client=provider),self.assertRaises(ValueError):
                BridgeClient(self.root,provider=provider)
        self.assertFalse((self.root/'data').exists())

    def test_both_provider_fixed_probes_use_their_own_workspaces_and_claim_proofs(self):
        now=[100.0]
        first=WorkBuddyBroker(self.root,clock=lambda:now[0],heartbeat_timeout=120)
        # This fixture drives ProbeInbox.tick and the shared clock explicitly.
        # Production has one watcher; running it as a second tick caller here
        # races a stale observation against the fixture's own compare-and-submit.
        first._watch=lambda:None
        first.start()
        self.addCleanup(first.close)
        second=WorkBuddyBroker(self.root,provider='doubao',clock=lambda:now[0],heartbeat_timeout=120)
        second._watch=lambda:None
        second.start()
        self.addCleanup(second.close)
        a,b=self.clients()
        pending=[]
        for provider,broker,client in [('workbuddy',first,a),('doubao',second,b)]:
            client.request('wait_started',progress_token_seen=True)
            client.request('poll')
            request_id=request_probe(self.root,provider=provider)
            broker._probe_inbox.tick()
            pending.append((provider,broker,client,request_id))
        now[0]+=40
        b.request('wait_segment_end');second._probe_inbox.tick()
        b.request('wait_started',progress_token_seen=True);b.request('poll')
        second._probe_inbox.tick()
        now[0]+=30
        for provider,broker,client,request_id in pending:
            broker._probe_inbox.tick()
            task=client.request('poll')['task']
            self.assertIsNotNone(task,get_probe_result(self.root,request_id,provider=provider))
            self.assertEqual(task['run_id'],'probe-'+request_id)
            expected_name='todo-dispatcher-doubao' if provider=='doubao' else 'todo-dispatcher'
            self.assertIn(expected_name+' 连接器',task['prompt'])
            nonce=task['lease_nonce']
            self.assertEqual(client.request('read_file',lease_nonce=nonce,path='seed.txt')['text'],'KEEP\n')
            client.request('write_file',lease_nonce=nonce,path='probe.txt',
                text='<<<TODO_TEXT_V1_BEGIN>>>READY\n<<<TODO_TEXT_V1_END>>>',text_format='framed')
            client.request('publish_result',lease_nonce=nonce,status='complete',summary='fixed test',
                artifacts=['probe.txt'],checks=['read back'],needs_input='')
            broker._probe_inbox.tick()
            receipt=get_probe_result(self.root,request_id,provider=provider)
            self.assertEqual(receipt['state'],'passed')
            self.assertEqual(receipt['provider'],provider)
            self.assertEqual(Path(receipt['workspace']),
                self.root/f'workspaces/{provider}-probes'/request_id/'workspace')
            self.assertTrue(receipt['result']['claim_evidence']['matched'])

    def test_default_workbuddy_can_read_an_existing_v1_signed_receipt(self):
        from dispatcher.workbuddy_probe import _sign
        folder=self.root/'data/workbuddy-probe';folder.mkdir(parents=True)
        control={'version':1,'epoch':'1'*64,'host_token':'2'*64}
        receipt={'version':1,'epoch':control['epoch'],'request_id':'a'*32,'state':'passed'}
        (folder/'control.json').write_text(json.dumps(control))
        (folder/'receipt.json').write_text(json.dumps(_sign(receipt,control['host_token'])))
        self.assertEqual(get_probe_result(self.root,'a'*32),receipt)

    def test_absolute_doubao_launcher_handshakes_without_cwd_or_global_pythonpath(self):
        first,second=self.brokers()
        launcher=Path(__file__).resolve().parents[1]/'scripts/doubao-mcp-worker.py'
        self.assertTrue(launcher.is_file())
        payload=''.join(json.dumps(item)+'\n' for item in [
            {'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26',
                'capabilities':{},'clientInfo':{'name':'isolated-test','version':'1'}}},
            {'jsonrpc':'2.0','method':'notifications/initialized'},
            {'jsonrpc':'2.0','id':2,'method':'tools/list'}])
        env=dict(os.environ,PYTHONIOENCODING='gbk',PYTHONUTF8='0');env.pop('PYTHONPATH',None)
        result=subprocess.run([sys.executable,'-B',str(launcher),'--root',str(self.root)],
            input=payload,cwd=self.temp.name,env=env,encoding='utf-8',stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,timeout=8,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        self.assertEqual(result.returncode,0,result.stderr)
        rows=[json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(rows[0]['result']['serverInfo']['name'],'todo-dispatcher-doubao')
        self.assertEqual(len(rows[1]['result']['tools']),5)
        self.assertFalse(first.status()['online']);self.assertFalse(second.status()['online'])
        self.assertEqual(first.status()['connection_generation'],0)
        # Tool discovery is passive for both desktop providers.
        self.assertEqual(second.status()['connection_generation'],0)

    def test_fixed_doubao_launcher_rejects_abbreviated_provider_overrides_before_connecting(self):
        first,second=self.brokers()
        launcher=Path(__file__).resolve().parents[1]/'scripts/doubao-mcp-worker.py'
        for extra in (['--provid','workbuddy'],['--provid=workbuddy']):
            with self.subTest(extra=extra):
                result=subprocess.run([sys.executable,'-B',str(launcher),'--root',str(self.root),*extra],
                    input='',cwd=self.temp.name,encoding='utf-8',stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,timeout=8,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                self.assertEqual(result.returncode,2)
                self.assertEqual(first.status()['connection_generation'],0)
                self.assertEqual(second.status()['connection_generation'],0)


if __name__=='__main__':unittest.main()
