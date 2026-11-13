"""Real owned broker mutations followed by a deliberately discarded reply."""
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

from dispatcher.workbuddy_mcp import BridgeClient, BridgeError, StdioWorker, WorkBuddyBroker


class DropAcceptedReply:
    def __init__(self,client,operation):
        self.inner=client;self.provider=client.provider;self.namespace=client.namespace
        self.operation=operation;self.calls=[]

    def request(self,operation,**params):
        self.calls.append(operation)
        value=self.inner.request(operation,**params)
        if operation==self.operation:
            raise BridgeError('connection_lost')  # Server already committed the operation.
        return value

    def close(self):self.inner.close()


class MCPAcceptedReplyLossTests(unittest.TestCase):
    def test_library_main_never_arms_hard_exit_and_fatal_clean_return_stays_nonzero(self):
        from dispatcher import workbuddy_mcp
        with tempfile.TemporaryDirectory() as directory:
            for failed,enabled,expected in ((True,False,1),(True,True,1),(False,False,0)):
                with self.subTest(failed=failed,enabled=enabled):
                    worker=Mock();worker._transport_failed.is_set.return_value=failed
                    with patch.object(workbuddy_mcp,'BridgeClient'), \
                            patch.object(workbuddy_mcp,'StdioWorker',return_value=worker) as factory:
                        result=workbuddy_mcp.main(['--root',directory,'--provider','doubao'],
                            exit_on_transport_failure=enabled)
                    self.assertEqual(result,expected)
                    self.assertIs(factory.call_args.kwargs['transport_exit'],os._exit if enabled else None)

    def test_committed_file_or_submission_is_not_replayed_after_losing_its_reply(self):
        for provider in ('workbuddy','doubao'):
            for operation in ('write_file','publish_result'):
                with self.subTest(provider=provider,operation=operation),tempfile.TemporaryDirectory() as directory:
                    root=Path(directory)/'root';workspace=root/'workspaces/fixed/workspace'
                    workspace.mkdir(parents=True)
                    broker=WorkBuddyBroker(root,provider=provider).start()
                    client=None
                    try:
                        client=BridgeClient(root,provider=provider)
                        client.request('wait_started',progress_token_seen=False)
                        client.request('poll')
                        broker.submit('owned-test',workspace,'Only owned fixture files.')
                        nonce=client.request('poll')['task']['lease_nonce']
                        if operation=='publish_result':
                            client.request('write_file',lease_nonce=nonce,path='result.txt',text='EXACT\n')
                            args={'lease_nonce':nonce,'status':'complete','summary':'submitted once',
                                  'artifacts':['result.txt'],'checks':['fixture'],'needs_input':''}
                        else:args={'lease_nonce':nonce,'path':'result.txt','text':'EXACT\n'}
                        transport=DropAcceptedReply(client,operation)
                        output=io.StringIO()
                        worker=StdioWorker(transport,stdin=io.StringIO(),stdout=output)
                        worker._call(1,{'name':operation,'arguments':args},threading.Event())
                        self.assertTrue(worker._transport_failed.is_set())
                        self.assertTrue(worker._closed.wait(2),'Old connection did not revoke its lease')
                        worker._call(2,{'name':operation,'arguments':args},threading.Event())
                        self.assertEqual(transport.calls,[operation])
                        self.assertEqual((workspace/'result.txt').read_bytes(),b'EXACT\n')
                        state=broker.result('owned-test')
                        self.assertEqual(state['state'],'submitted' if operation=='publish_result' else 'disconnected')
                        self.assertIsNone(broker.status()['active_run_id'])
                        self.assertEqual(broker.status()['connection_generation'],1)
                        first=json.loads(output.getvalue().splitlines()[0])
                        self.assertTrue(first['result']['isError'])
                        self.assertIn('connection_lost',first['result']['content'][0]['text'])
                    finally:
                        if client:client.close()
                        broker.close()


if __name__=='__main__':unittest.main()
