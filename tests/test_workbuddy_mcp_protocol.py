"""Standard MCP stdio control tests with owned broker and dummy client only."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from dispatcher.workbuddy_mcp import WorkBuddyBroker


class WorkBuddyMCPProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='todo-mcp-protocol-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.workspace=self.root/'workspaces/task/workspace'
        self.workspace.mkdir(parents=True)
        self.broker=WorkBuddyBroker(self.root).start()
        self.addCleanup(self.broker.close)
        self.process=subprocess.Popen([sys.executable,'-B','-u','-m','dispatcher.workbuddy_mcp',
            '--root',str(self.root),'--idle-seconds','1','--progress-seconds','0.05'],
            cwd=Path(__file__).resolve().parents[1],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,text=True,encoding='utf-8',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        self.addCleanup(self.close_process)
        self.messages=queue.Queue()
        def read_messages():
            try:
                for line in self.process.stdout:
                    self.messages.put(json.loads(line))
            finally:self.messages.put(None)
        self.reader=threading.Thread(target=read_messages,daemon=True);self.reader.start()
        self.send({'jsonrpc':'2.0','id':1,'method':'initialize','params':{
            'protocolVersion':'2025-03-26','capabilities':{},
            'clientInfo':{'name':'isolated-protocol-test','version':'1'}}})
        response,_=self.until_response(1)
        self.assertEqual(response['result']['protocolVersion'],'2025-03-26')
        self.send({'jsonrpc':'2.0','method':'notifications/initialized'})

    def close_process(self):
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        try:self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill();self.process.wait(timeout=3)
        self.reader.join(timeout=2)
        self.process.stdout.close();self.process.stderr.close()

    def send(self,value):
        self.process.stdin.write(json.dumps(value)+'\n');self.process.stdin.flush()

    def wait_call(self,request_id=2,token=None):
        params={'name':'wait_for_task','arguments':{}}
        if token is not None:params['_meta']={'progressToken':token}
        self.send({'jsonrpc':'2.0','id':request_id,'method':'tools/call','params':params})

    def until_response(self,request_id,timeout=4):
        deadline=time.monotonic()+timeout;notices=[]
        while time.monotonic()<deadline:
            try:value=self.messages.get(timeout=max(.01,deadline-time.monotonic()))
            except queue.Empty:self.fail('MCP response timed out')
            self.assertIsNotNone(value,'MCP subprocess exited before its response')
            if value.get('id')==request_id:return value,notices
            notices.append(value)
        self.fail('MCP response timed out')

    def wait_ready(self):
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            if self.broker.status()['ready']:return
            time.sleep(.01)
        self.fail('Worker did not enter its bounded wait')

    def test_standard_cancellation_revokes_wait_and_keeps_protocol_alive(self):
        self.wait_call(token='cancel-2');self.wait_ready()
        self.send({'jsonrpc':'2.0','method':'notifications/cancelled',
                   'params':{'requestId':2,'reason':'owned test cancellation'}})
        response,_=self.until_response(2)
        self.assertTrue(response['result']['isError'])
        self.assertIn('request_cancelled',response['result']['content'][0]['text'])
        self.assertFalse(self.broker.status()['ready'])
        self.broker.submit('after-cancel',self.workspace,'Do not execute.',timeout=5)
        self.send({'jsonrpc':'2.0','id':3,'method':'ping','params':{}})
        response,_=self.until_response(3)
        self.assertEqual(response['result'],{})
        self.assertEqual(self.broker.result('after-cancel')['state'],'queued')
        self.broker.cancel('after-cancel')

    def test_without_progress_token_emits_no_progress_notifications(self):
        self.wait_call()
        response,notices=self.until_response(2)
        self.assertFalse(response['result']['isError'])
        self.assertEqual(json.loads(response['result']['content'][0]['text']),{'state':'idle_timeout'})
        self.assertFalse(any(value.get('method')=='notifications/progress' for value in notices))

    def test_idle_timeout_returns_terminal_wait_and_clears_ready(self):
        self.wait_call(token='idle-2');self.wait_ready()
        response,notices=self.until_response(2)
        self.assertEqual(json.loads(response['result']['content'][0]['text']),{'state':'idle_timeout'})
        status=self.broker.status()
        self.assertTrue(status['online'])
        self.assertFalse(status['ready'])
        progress=[value for value in notices if value.get('method')=='notifications/progress']
        self.assertTrue(progress)
        self.assertTrue(all(value['params']['progressToken']=='idle-2' for value in progress))


if __name__=='__main__':unittest.main()
