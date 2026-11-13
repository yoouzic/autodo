import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dispatcher.control import Controller
from dispatcher.core import Store
from dispatcher.runner import CodexRunner
from dispatcher.service import Service
from dispatcher.workspace import WorkspaceManager
from tests.test_service import FakeDida


class WorkspaceSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'original';self.source.mkdir();(self.source/'seed.txt').write_text('keep')
        self.store=Store(self.root/'data/tasks.db')
        self.task={'id':'one','projectId':'p','title':'整理文档','status':0,'tags':[]}
        self.config={'codex_command':['codex'],'enabled_executors':['codex'],'model':'gpt-6.1-sol',
                     'repositories':{'notes':str(self.source)},'require_human_review':True}
        self.runner=CodexRunner(self.root,self.config)
        self.service=Service(self.root,self.config,self.store,FakeDida(self.task.copy()),self.runner)
        self.control=Controller(self.service,WorkspaceManager(self.root,self.store),[{'name':'codex','enabled':True}])
        self.service.sync()
    def tearDown(self):self.store.close();self.temp.cleanup()
    def choice(self):
        return {'mode':'existing','workspace_id':self.control.workspace_catalog(force=True)['items'][0]['id']}
    def assign(self, choice):
        row=self.store.get('p:one')
        command=self.control.submit({'action':'assign','request_id':'workspace-test','task_id':'p:one',
            'revision':row['revision'],'executor':'codex','workspace_choice':choice})
        self.control.process_pending()
        result=self.store.get_command(command['id'])
        self.assertEqual(result['status'],'done',result['error'])

    def test_catalog_and_assignment_do_not_write_selected_directory(self):
        choice=self.choice();self.assign(choice)
        self.assertEqual(self.store.get('p:one')['workspace_choice'],choice)
        self.assertEqual(sorted(p.name for p in self.source.iterdir()),['seed.txt'])
        state=self.control.state()
        self.assertEqual(state['workspace_policy']['reuse_strategy'],'in_place')

    def test_catalog_and_selection_http_keep_same_origin_and_id_boundaries(self):
        import http.client
        from dispatcher.dashboard import start_dashboard
        with start_dashboard(self.service.snapshot,port=0,control=self.control) as server:
            port=server.server_address[1]
            def request(method,path,payload=None,headers=None):
                connection=http.client.HTTPConnection('127.0.0.1',port)
                connection.request(method,path,json.dumps(payload) if payload is not None else None,headers or {})
                response=connection.getresponse();code,body=response.status,response.read();connection.close()
                return code,body
            code,raw=request('GET','/api/workspaces');self.assertEqual(code,200)
            selected=json.loads(raw)['items'][0]
            self.assertEqual(selected['path'],str(self.source))
            self.assertEqual(request('GET','/api/workspaces',headers={'Sec-Fetch-Site':'cross-site'})[0],403)
            state=json.loads(request('GET','/api/state')[1])
            headers={'Content-Type':'application/json','Origin':f'http://127.0.0.1:{port}','X-Dispatcher-Token':state['csrf_token']}
            payload={'action':'assign','request_id':'http-workspace','task_id':'p:one',
                     'revision':state['tasks'][0]['revision'],'executor':'codex',
                     'workspace_choice':{'mode':'existing','workspace_id':selected['id']}}
            self.assertEqual(request('POST','/api/actions',payload)[0],403)
            invalid=dict(payload,workspace_choice={'mode':'existing','path':str(self.source)})
            self.assertEqual(request('POST','/api/actions',invalid,headers)[0],400)
            code,raw=request('POST','/api/actions',payload,headers);self.assertEqual(code,202)
            self.control.process_pending()
            self.assertEqual(self.store.get_command(json.loads(raw)['id'])['status'],'done')
            self.assertEqual(self.store.get('p:one')['workspace_choice'],payload['workspace_choice'])

    def test_in_place_execution_writes_original_and_review_uses_independent_snapshot(self):
        self.assign(self.choice())
        report={'status':'complete','summary':'done','artifacts':['answer.md'],'checks':['read'],'needs_input':''}
        observed=[]
        def invoke(run,prompt,phase,effort,readonly=False,**kwargs):
            directory=Path(kwargs.get('workspace') or Path(run)/'workspace')
            observed.append((phase,directory))
            if phase=='execution':
                self.assertEqual(directory,self.source)
                (directory/'answer.md').write_text('new content',encoding='utf-8')
            else:self.assertEqual((directory/'answer.md').read_text(encoding='utf-8'),'new content')
            return report.copy()
        with patch.object(self.runner,'_invoke',side_effect=invoke):self.service.execute(self.store.claim())
        row=self.store.get('p:one')
        self.assertEqual(row['state'],'awaiting_review',row.get('error'))
        binding=self.store.get_run(row['current_run_id'])['workspace_binding']
        self.assertEqual(binding['workspace_path'],str(self.source))
        self.assertNotEqual(binding['workspace_path'],binding['snapshot_path'])
        self.assertEqual(observed[1][1],Path(binding['snapshot_path']))
        (self.source/'answer.md').write_text('later change',encoding='utf-8')
        self.assertEqual(self.control.workspaces.preview(row['current_run_id'],'answer.md')['text'],'new content')
        self.assertTrue(self.service._check_artifacts(row))
        self.assertEqual(self.service.dida.completions,0)
        from dispatcher.workspace_lock import busy_paths
        self.assertEqual(busy_paths(self.root),[])

    def test_busy_workspace_is_rejected_before_assignment(self):
        choice=self.choice()
        from dispatcher.workspace_lock import WorkspaceLease
        with WorkspaceLease(self.root,self.source,'other-run'):
            with self.assertRaises(ValueError):self.assign(choice)

    def test_missing_source_is_rechecked_at_actual_start(self):
        self.assign(self.choice())
        moved=self.root/'moved';self.source.rename(moved)
        with patch.object(self.runner,'_invoke') as invoke:
            self.service.execute(self.store.claim());invoke.assert_not_called()
        self.assertIn(self.store.get('p:one')['state'],('needs_input','failed'))

    def test_new_named_workspace_is_separate_and_persistent(self):
        self.assign({'mode':'new','name':'课程笔记'})
        report={'status':'complete','summary':'done','artifacts':['answer.md'],'checks':['read'],'needs_input':''}
        def invoke(run,prompt,phase,effort,readonly=False,**kwargs):
            if phase=='execution':(Path(kwargs['workspace'])/'answer.md').write_text('notes')
            return report.copy()
        with patch.object(self.runner,'_invoke',side_effect=invoke):self.service.execute(self.store.claim())
        row=self.store.get('p:one');self.assertEqual(row['state'],'awaiting_review',row.get('error'))
        binding=self.store.get_run(row['current_run_id'])['workspace_binding']
        self.assertIn('课程笔记',binding['workspace_path'])
        self.assertTrue(Path(binding['workspace_path']).is_dir())
        self.assertTrue(Path(binding['snapshot_path']).is_dir())

    def test_writeback_rejects_replaced_snapshot_junction(self):
        import os
        import subprocess
        if os.name!='nt':self.skipTest('Windows junction regression')
        self.assign({'mode':'new','name':'junction-check'})
        report={'status':'complete','summary':'done','artifacts':['answer.md'],'checks':['read'],'needs_input':''}
        def invoke(run,prompt,phase,effort,readonly=False,**kwargs):
            if phase=='execution':(Path(kwargs['workspace'])/'answer.md').write_text('same bytes')
            return report.copy()
        with patch.object(self.runner,'_invoke',side_effect=invoke):self.service.execute(self.store.claim())
        row=self.store.get('p:one');self.assertEqual(row['state'],'awaiting_review')
        run=self.store.get_run(row['current_run_id'])
        snapshot=Path(run['workspace_binding']['snapshot_path'])
        outside=self.root/'outside-snapshot'
        snapshot.rename(outside)
        subprocess.run(['cmd','/c','mklink','/J',str(snapshot),str(outside)],check=True,capture_output=True)
        try:
            self.assertFalse(self.service._check_artifacts(row))
            self.assertEqual(self.store.get('p:one')['state'],'needs_review')
            self.assertEqual(self.service.dida.completions,0)
        finally:
            snapshot.rmdir()  # Remove only the junction, never its target.


if __name__=='__main__':unittest.main()
