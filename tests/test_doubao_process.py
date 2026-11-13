"""Real owned dummy subprocesses, never Doubao, accounts or model requests."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from dispatcher.doubao_process import run_doubao_process


class DoubaoProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.workspace=self.root/'workspace';self.workspace.mkdir()
        self.run=self.root/'run';self.run.mkdir()

    def child(self,code):
        script=self.root/('child-'+str(time.time_ns())+'.py')
        script.write_text(code,encoding='utf-8')
        return [sys.executable,'-I','-B',str(script)]

    def invoke(self,code,**kwargs):
        return run_doubao_process(self.child(code),cwd=self.workspace,run_dir=self.run,
                                  timeout=kwargs.pop('timeout',5),**kwargs)

    def test_success_captures_bounded_logs_but_does_not_claim_remote_stop(self):
        result=self.invoke("import sys\nprint('ok',flush=True)\nprint('note',file=sys.stderr,flush=True)\n")
        self.assertEqual(result['exit_code'],0)
        self.assertEqual(result['stdout'].strip(),'ok')
        self.assertEqual(result['stderr'].strip(),'note')
        self.assertTrue(result['client_exited'])
        self.assertFalse(result['requires_quarantine'])
        self.assertFalse(result['remote_stop_confirmed'])
        self.assertFalse(result['signal_attempted'])
        stored=json.loads(Path(result['receipt_path']).read_text(encoding='utf-8'))
        self.assertEqual(stored['body']['invocation_id'],result['invocation_id'])
        self.assertEqual(len(stored['hmac_sha256']),64)
        self.assertNotIn('secret',stored['body'])

    def test_nonzero_is_quarantined_even_after_client_exits(self):
        result=self.invoke('raise SystemExit(4)\n')
        self.assertEqual(result['exit_code'],4)
        self.assertTrue(result['client_exited'])
        self.assertTrue(result['requires_quarantine'])
        self.assertFalse(result['remote_stop_confirmed'])

    def test_preexisting_cancellation_never_starts_a_client(self):
        cancelled=threading.Event();cancelled.set()
        result=self.invoke("from pathlib import Path\nPath('must-not-exist').write_text('bad')\n",cancel_event=cancelled)
        self.assertFalse(result['started'])
        self.assertIsNone(result['helper_pid'])
        self.assertTrue(result['cancelled'])
        self.assertFalse(result['requires_quarantine'])
        self.assertFalse((self.workspace/'must-not-exist').exists())

    def test_timeout_attempts_real_sigint_and_retains_quarantine(self):
        result=self.invoke("""import signal,sys,time
from pathlib import Path
def stop(*args):
    Path('interrupted.txt').write_text('SIGINT')
    raise SystemExit(130)
signal.signal(signal.SIGINT,stop)
print('ready',flush=True)
while True:time.sleep(.05)
""",timeout=.6,grace_seconds=1)
        self.assertTrue(result['timed_out'])
        self.assertTrue(result['signal_attempted'])
        self.assertTrue(result['signal_sent'],result)
        self.assertEqual((self.workspace/'interrupted.txt').read_text(),'SIGINT')
        self.assertEqual(result['exit_code'],130)
        self.assertFalse(result['force_terminated'])
        self.assertTrue(result['requires_quarantine'])
        self.assertFalse(result['remote_stop_confirmed'])

    def test_cancel_event_attempts_sigint_without_claiming_task_stopped(self):
        cancel=threading.Event()
        def request():
            deadline=time.monotonic()+3
            while not (self.workspace/'ready').exists() and time.monotonic()<deadline:time.sleep(.01)
            cancel.set()
        request_thread=threading.Thread(target=request,daemon=True);request_thread.start()
        result=self.invoke("""import signal,time
from pathlib import Path
signal.signal(signal.SIGINT,lambda *args:exit(130))
Path('ready').write_text('ready')
while True:time.sleep(.05)
""",cancel_event=cancel,grace_seconds=1)
        request_thread.join(timeout=1)
        self.assertTrue(result['cancelled'])
        self.assertFalse(result['timed_out'])
        self.assertTrue(result['signal_sent'])
        self.assertTrue(result['requires_quarantine'])
        self.assertFalse(result['remote_stop_confirmed'])

    def test_nonresponsive_client_is_terminated_without_killing_unrelated_process(self):
        unrelated=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(30)'],
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        try:
            result=self.invoke("""import signal,time
from pathlib import Path
signal.signal(signal.SIGINT,signal.SIG_IGN)
while True:time.sleep(.05)
""",timeout=.4,grace_seconds=.3)
            self.assertTrue(result['timed_out'])
            self.assertTrue(result['force_terminated'])
            self.assertTrue(result['client_exited'])
            self.assertTrue(result['requires_quarantine'])
            self.assertIsNone(unrelated.poll())
        finally:
            if unrelated.poll() is None:unrelated.terminate()
            unrelated.wait(timeout=3)

    def test_spawn_failure_does_not_claim_an_executed_task(self):
        result=run_doubao_process([str(self.root/'not-an-executable')],cwd=self.workspace,
                                 run_dir=self.run,timeout=1,grace_seconds=.2)
        self.assertFalse(result['started'])
        self.assertTrue(result['supervisor_error'])
        self.assertFalse(result['requires_quarantine'])
        self.assertEqual(result['stdout'],'')

    def test_output_limit_triggers_quarantine_and_bounds_retained_log(self):
        from dispatcher.doubao_process import MAX_LOG_BYTES
        result=self.invoke('import os,time\n'
            +f'os.write(1,b"x"*({MAX_LOG_BYTES}+65536))\n'
            +'while True:time.sleep(.1)\n',timeout=5,grace_seconds=.5)
        self.assertTrue(result['output_limit'])
        self.assertTrue(result['requires_quarantine'])
        self.assertLessEqual(len(result['stdout'].encode('utf-8')),MAX_LOG_BYTES)
        self.assertFalse(result['remote_stop_confirmed'])

    def test_tampered_lifecycle_receipt_is_rejected(self):
        from dispatcher import doubao_process
        original=doubao_process._read
        def tampered(path,maximum):
            raw=original(path,maximum)
            if path.suffix=='.json':
                value=json.loads(raw);value['body']['requires_quarantine']=False
                return json.dumps(value).encode()
            return raw
        with patch.object(doubao_process,'_read',side_effect=tampered):
            with self.assertRaisesRegex(ValueError,'integrity failed'):
                self.invoke('raise SystemExit(4)\n')

    @unittest.skipUnless(os.name=='nt','Windows private-console membership')
    def test_extra_console_member_blocks_broadcast_and_is_not_killed(self):
        service_script=self.root/'fake-shared-service.py'
        service_script.write_text("""import signal,time
from pathlib import Path
def interrupted(*args):Path('shared-interrupted').write_text('bad')
signal.signal(signal.SIGINT,interrupted)
Path('shared-ready').write_text('yes')
while True:time.sleep(.05)
""",encoding='utf-8')
        pid=None
        try:
            result=self.invoke('import subprocess,sys,time\nfrom pathlib import Path\n'
                +f'p=subprocess.Popen([sys.executable,"-I","-B",{str(service_script)!r}],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n'
                +'Path("shared.pid").write_text(str(p.pid))\n'
                +'while True:time.sleep(.05)\n',timeout=.6,grace_seconds=.2)
            pid=int((self.workspace/'shared.pid').read_text())
            self.assertTrue((self.workspace/'shared-ready').exists())
            self.assertTrue(result['timed_out'])
            self.assertFalse(result['signal_sent'])
            self.assertEqual(result['signal_error'],'unexpected_console_members')
            self.assertTrue(result['force_terminated'])
            self.assertTrue(result['requires_quarantine'])
            self.assertFalse((self.workspace/'shared-interrupted').exists())
            import ctypes
            from ctypes import wintypes
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
            kernel.OpenProcess.restype=wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
            kernel.WaitForSingleObject.restype=wintypes.DWORD
            kernel.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT]
            kernel.CloseHandle.argtypes=[wintypes.HANDLE]
            # Open only the dummy service created by this test, preserving its
            # handle identity while checking and cleaning up that exact process.
            handle=kernel.OpenProcess(0x00100001,False,pid)
            self.assertTrue(handle)
            try:
                self.assertEqual(kernel.WaitForSingleObject(handle,0),0x102)
                kernel.TerminateProcess(handle,0);kernel.WaitForSingleObject(handle,2000)
            finally:kernel.CloseHandle(handle)
            pid=None
        finally:
            if pid:
                # Known, test-created PID; this never enumerates/kills Doubao.
                try:os.kill(pid,signal.SIGTERM)
                except OSError:pass

    def test_parent_disappearance_attempts_stop_and_keeps_uncertain_receipt(self):
        controller=self.root/'controller.py'
        child_script=self.child("""import signal,time
from pathlib import Path
def stop(*args):
    Path('parent-lost-sigint').write_text('yes')
    raise SystemExit(130)
signal.signal(signal.SIGINT,stop)
Path('ready-parent-loss').write_text('yes')
while True:time.sleep(.05)
""")
        repo=Path(__file__).resolve().parents[1]
        controller.write_text('import sys\n'
            +f'sys.path.insert(0,{str(repo)!r})\n'
            +'from dispatcher.doubao_process import run_doubao_process\n'
            +f'run_doubao_process({child_script!r},cwd={str(self.workspace)!r},run_dir={str(self.run)!r},timeout=15,grace_seconds=1)\n',encoding='utf-8')
        parent=subprocess.Popen([sys.executable,'-I','-B',str(controller)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        try:
            deadline=time.monotonic()+4
            while not (self.workspace/'ready-parent-loss').exists() and time.monotonic()<deadline:time.sleep(.02)
            self.assertTrue((self.workspace/'ready-parent-loss').exists())
            parent.terminate();parent.wait(timeout=3)
            deadline=time.monotonic()+4
            while not list(self.run.glob('doubao-process-*.json')) and time.monotonic()<deadline:time.sleep(.02)
            paths=list(self.run.glob('doubao-process-*.json'));self.assertEqual(len(paths),1)
            receipt=json.loads(paths[0].read_text(encoding='utf-8'))['body']
            self.assertTrue(receipt['parent_lost'])
            self.assertTrue(receipt['requires_quarantine'])
            self.assertFalse(receipt['remote_stop_confirmed'])
            self.assertTrue((self.workspace/'parent-lost-sigint').exists())
        finally:
            if parent.poll() is None:parent.terminate();parent.wait(timeout=3)

    def test_rejects_unbounded_or_ambiguous_inputs(self):
        for kwargs in ({'timeout':float('inf')},{'timeout':True},{'grace_seconds':90},{'input_text':'not the CLI contract'}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                run_doubao_process([sys.executable],cwd=self.workspace,run_dir=self.run,**kwargs)
        with self.assertRaises(ValueError):run_doubao_process([],cwd=self.workspace,run_dir=self.run)


if __name__=='__main__':unittest.main()
