"""Persistent guard tests use owned temporary files and dummy subprocesses only."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from dispatcher.workspace_guard import (
    WorkspaceGuardError, active_guards, quarantine_guard, release_guard, start_guard,
)
import dispatcher.workspace_guard as guard


class WorkspaceGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='todo-guard-')
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve()
        self.root=self.base/'project';self.root.mkdir()
        self.workspace=self.base/'workspace';self.workspace.mkdir()
        self.directory=self.root/'data/workspace-guards'

    def start(self,run='run-1',workspace=None):
        return start_guard(self.root,run,workspace or self.workspace)

    def record_path(self,run='run-1'):
        return self.directory/(hashlib.sha256(run.encode('utf-8')).hexdigest()+'.json')

    def read_record(self,run='run-1'):
        return json.loads(self.record_path(run).read_text(encoding='utf-8'))

    def write_record(self,value,run='run-1'):
        self.record_path(run).write_text(json.dumps(value),encoding='utf-8')

    def subprocess(self,code,*args):
        options={}
        if os.name=='nt':options['creationflags']=subprocess.CREATE_NO_WINDOW
        return subprocess.Popen([sys.executable,'-B','-c',code,*map(str,args)],
                                cwd=Path(__file__).resolve().parents[1],
                                stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,**options)

    def test_empty_registry_is_read_only(self):
        self.assertEqual(active_guards(self.root),[])
        self.assertFalse((self.root/'data').exists())

    def test_start_persists_identity_and_redacts_public_metadata(self):
        frozen=self.start()
        self.assertEqual(frozen,self.read_record())
        self.assertEqual(frozen['status'],'running')
        self.assertRegex(frozen['guard_id'],r'^[0-9a-f]{64}$')
        self.assertEqual(active_guards(self.root),[{
            'workspace':str(self.workspace),'run_id':'run-1','executor':'doubao',
            'status':'running','reason':None}])
        self.assertNotIn(frozen['guard_id'],json.dumps(active_guards(self.root)))

    def test_release_is_idempotent_and_allows_a_new_run(self):
        frozen=self.start()
        released=release_guard(self.root,frozen)
        self.assertEqual(release_guard(self.root,frozen),released)
        self.assertEqual(release_guard(self.root,released),released)
        self.assertEqual(active_guards(self.root),[])
        self.assertTrue(self.record_path().is_file())
        next_run=self.start('run-2')
        self.assertNotEqual(next_run['guard_id'],frozen['guard_id'])
        with self.assertRaises(WorkspaceGuardError):self.start()

    def test_not_started_release_has_a_distinct_reason(self):
        frozen=self.start()
        release_guard(self.root,frozen,reason='not_started')
        self.assertEqual(active_guards(self.root),[])
        with self.assertRaises(WorkspaceGuardError):release_guard(self.root,frozen)

    def test_quarantine_cannot_be_cleared_by_late_completion_or_approval(self):
        frozen=self.start()
        quarantined=quarantine_guard(self.root,frozen,'timeout')
        self.assertEqual(quarantine_guard(self.root,frozen,'timeout'),quarantined)
        self.assertEqual(active_guards(self.root)[0]['status'],'quarantined')
        for reason in ('verified_completion','not_started'):
            with self.assertRaises(WorkspaceGuardError):release_guard(self.root,frozen,reason)
        with self.assertRaises(WorkspaceGuardError):quarantine_guard(self.root,frozen,'cancelled')
        with self.assertRaises(WorkspaceGuardError):self.start('new')
        self.assertEqual(self.read_record(),quarantined)

    def test_released_record_cannot_be_quarantined_by_stale_callback(self):
        frozen=self.start();released=release_guard(self.root,frozen)
        with self.assertRaises(WorkspaceGuardError):quarantine_guard(self.root,frozen,'timeout')
        self.assertEqual(self.read_record(),released)

    def test_frozen_identity_must_match_all_immutable_fields(self):
        frozen=self.start()
        for key,value in {'guard_id':'0'*64,'run_id':'different','executor':'codex',
                          'workspace':str(self.base/'other'),
                          'created_at':'2000-01-01T00:00:00.000000Z'}.items():
            with self.subTest(key=key),self.assertRaises(WorkspaceGuardError):
                release_guard(self.root,dict(frozen,**{key:value}))
        self.assertEqual(self.read_record(),frozen)

    def test_overlap_is_blocked_but_siblings_are_allowed(self):
        self.start()
        for path in (self.workspace,self.workspace/'sub',self.base):
            with self.subTest(path=path),self.assertRaises(WorkspaceGuardError):
                self.start(str(path.name),path)
        sibling=self.base/'workspace-neighbour';sibling.mkdir()
        self.start('sibling',sibling)
        self.assertEqual(len(active_guards(self.root)),2)

    def test_dead_owner_and_old_timestamps_do_not_release(self):
        child=self.subprocess("""import sys,time
from dispatcher.workspace_guard import start_guard
start_guard(sys.argv[1],'orphan',sys.argv[2])
print('ready',flush=True)
time.sleep(120)
""",self.root,self.workspace)
        try:
            self.assertEqual(child.stdout.readline().strip(),'ready')
            child.kill();child.wait(timeout=5)
        finally:
            if child.poll() is None:child.kill();child.wait(timeout=5)
            child.stdout.close();child.stderr.close()
        record=self.read_record('orphan')
        record['created_at']=record['updated_at']='2000-01-01T00:00:00.000000Z'
        self.write_record(record,'orphan')
        self.assertEqual(active_guards(self.root)[0]['status'],'running')
        with self.assertRaises(WorkspaceGuardError):self.start('replacement')

    def test_cross_process_competing_start_only_one_claims_workspace(self):
        code="""import sys
from dispatcher.workspace_guard import start_guard,WorkspaceGuardError
try:start_guard(sys.argv[1],sys.argv[3],sys.argv[2])
except WorkspaceGuardError:raise SystemExit(3)
"""
        children=[self.subprocess(code,self.root,self.workspace,'race-'+str(i)) for i in range(2)]
        try:
            output=[child.communicate(timeout=8) for child in children]
            self.assertEqual(sorted(child.returncode for child in children),[0,3],output)
            self.assertEqual(len(active_guards(self.root)),1)
        finally:
            for child in children:
                if child.poll() is None:child.kill();child.wait(timeout=5)

    def test_competing_transitions_choose_one_terminal_state(self):
        frozen=self.start()
        def release_or_quarantine(index):
            try:
                return (release_guard(self.root,frozen) if index else
                        quarantine_guard(self.root,frozen,'timeout'))['status']
            except WorkspaceGuardError:return 'rejected'
        with ThreadPoolExecutor(max_workers=2) as pool:
            result=list(pool.map(release_or_quarantine,[0,1]))
        self.assertEqual(result.count('rejected'),1,result)
        self.assertIn(self.read_record()['status'],{'released','quarantined'})

    def test_malformed_or_unknown_record_fields_fail_closed(self):
        frozen=self.start()
        variants=[None,{},dict(frozen,extra='bad'),dict(frozen,version=True),
                  dict(frozen,status='complete'),dict(frozen,reason='timeout'),
                  dict(frozen,status='quarantined',reason='unknown'),
                  dict(frozen,guard_id='bad'),dict(frozen,root=str(self.base)),
                  dict(frozen,run_id='wrong-filename'),dict(frozen,created_at='old'),
                  dict(frozen,workspace='../escape')]
        for variant in variants:
            with self.subTest(variant=variant):
                self.write_record(variant)
                with self.assertRaises(WorkspaceGuardError):active_guards(self.root)
        for raw in ('{','{"version":1,"version":1}',json.dumps(frozen).replace('null','NaN')):
            self.record_path().write_text(raw,encoding='utf-8')
            with self.assertRaises(WorkspaceGuardError):active_guards(self.root)

    def test_released_records_are_still_validated(self):
        frozen=self.start();released=release_guard(self.root,frozen)
        self.write_record(dict(released,guard_id='corrupt'))
        with self.assertRaises(WorkspaceGuardError):active_guards(self.root)
        with self.assertRaises(WorkspaceGuardError):self.start('new')

    def test_unknown_file_or_interrupted_atomic_update_fails_closed(self):
        frozen=self.start()
        with patch.object(Path,'replace',side_effect=OSError('simulated failure')):
            with self.assertRaises(WorkspaceGuardError):quarantine_guard(self.root,frozen,'timeout')
        self.assertEqual(self.read_record(),frozen)
        self.assertTrue(list(self.directory.glob('*.tmp')))
        with self.assertRaises(WorkspaceGuardError):active_guards(self.root)

    def test_bounded_record_count_and_bytes_fail_before_new_write(self):
        self.start()
        with patch.object(guard,'MAX_RECORDS',1):
            with self.assertRaises(WorkspaceGuardError):self.start('other',self.base/'sibling')
        self.assertFalse(self.record_path('other').exists())
        current=self.record_path().stat().st_size
        with patch.object(guard,'MAX_TOTAL_BYTES',current):
            with self.assertRaises(WorkspaceGuardError):self.start('other',self.base/'sibling')
        self.assertFalse(self.record_path('other').exists())
        with patch.object(guard,'MAX_TOTAL_BYTES',current-1):
            with self.assertRaises(WorkspaceGuardError):active_guards(self.root)
        with patch.object(guard,'MAX_RECORD_BYTES',current-1):
            with self.assertRaises(WorkspaceGuardError):active_guards(self.root)

    def test_invalid_inputs_and_private_or_metadata_paths_are_rejected(self):
        for run in ('','a\nb',' a','x'*257):
            with self.subTest(run=run),self.assertRaises(WorkspaceGuardError):self.start(run)
        for executor in ('','../other','UPPER','doubao\x00'):
            with self.subTest(executor=executor),self.assertRaises(WorkspaceGuardError):
                start_guard(self.root,'x',self.workspace,executor)
        invalid=['relative/path',Path(self.workspace.anchor),Path.home(),
                 Path.home()/'.ssh',self.root,self.root/'data',self.directory,
                 self.directory/'child','\\\\server\\share\\workspace']
        if os.name=='nt':invalid.append(str(self.workspace)+':alternate')
        for path in invalid:
            with self.subTest(path=str(path)),self.assertRaises(WorkspaceGuardError):self.start('x',path)
        frozen=self.start()
        with self.assertRaises(WorkspaceGuardError):quarantine_guard(self.root,frozen,'anything')
        with self.assertRaises(WorkspaceGuardError):release_guard(self.root,frozen,'approved')

    def test_record_hardlinks_are_rejected(self):
        self.start()
        link=self.base/'record-copy.json'
        os.link(self.record_path(),link)
        with self.assertRaises(WorkspaceGuardError):active_guards(self.root)

    @unittest.skipUnless(os.name=='nt','Windows junction boundary')
    def test_junctions_in_registry_workspace_and_record_are_rejected(self):
        def junction(path,target):
            # Paths are owned temporary fixtures, never interpolated into shell code.
            command="$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path $env:TODO_GUARD_LINK -Target $env:TODO_GUARD_TARGET | Out-Null"
            result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
                                   command],capture_output=True,text=True,
                                  env=dict(os.environ,TODO_GUARD_LINK=str(path),TODO_GUARD_TARGET=str(target)),
                                  creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:self.fail(result.stderr)
            self.addCleanup(lambda: path.rmdir() if path.is_junction() else None)
        redirected=self.base/'redirect';redirected.mkdir()
        junction(self.root/'data',redirected)
        with self.assertRaises(WorkspaceGuardError):active_guards(self.root)
        with self.assertRaises(WorkspaceGuardError):self.start()
        (self.root/'data').rmdir()
        linked_workspace=self.base/'linked-workspace'
        junction(linked_workspace,self.workspace)
        with self.assertRaises(WorkspaceGuardError):self.start('linked',linked_workspace)
        self.start()
        # Replace only this fixture's record with a junction, not its target.
        self.record_path().unlink()
        junction(self.record_path(),redirected)
        with self.assertRaises(WorkspaceGuardError):active_guards(self.root)


if __name__=='__main__':unittest.main()
