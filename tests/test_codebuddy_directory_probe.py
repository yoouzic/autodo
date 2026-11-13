import copy,json,tempfile,unittest,hashlib
from pathlib import Path
from dispatcher.codebuddy import normalize_result

class DirectoryProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.workspace=Path(self.temp.name);(self.workspace/'index.html').write_text('<h1>Cabin</h1>')
        self.events=[]
        def call(identifier,name,path,error=False):
            self.events.append({'type':'assistant','message':{'content':[{'type':'tool_use','id':identifier,'name':name,'input':{'file_path':path}}]}})
            self.events.append({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':identifier,'is_error':error,'content':[{'type':'text','text':'<tool_use_error>Path is a directory, not a file: .\nThis tool can only read files, not directories.</tool_use_error>' if error else 'ok'}]}]}})
        call('a','Read',str(self.workspace),True);call('b','Write','index.html');call('c','Read','index.html')
        self.events.append({'type':'result','subtype':'success','is_error':False,'permission_denials':[],
            'structured_output':{'status':'complete','summary':'built','artifacts':['index.html'],'checks':['read file'],'needs_input':''}})

    def parse(self,events=None):
        return normalize_result('\n'.join(map(json.dumps,events or self.events)),workspace=self.workspace)

    def test_completed_native_success_with_directory_probe_can_be_independently_reviewed(self):
        result=self.parse();self.assertEqual(result['status'],'complete')
        self.assertEqual(result['tool_error_count'],1);self.assertTrue(result['directory_probe_recovered'])
        self.assertIn('index.html',result['recovered_artifact_sha256'])
        self.assertEqual(self.events[1]['message']['content'][0]['is_error'],True)

    def test_denials_transport_errors_and_unread_artifacts_stay_fail_closed(self):
        variants=[]
        e=copy.deepcopy(self.events);e[-1]['permission_denials']=[{'tool':'Write'}];variants.append(e)
        e=copy.deepcopy(self.events);e[1]['message']['content'][0]['content'][0]['text']='Permission denied';variants.append(e)
        e=copy.deepcopy(self.events);e[0]['message']['content'][0]['name']='Bash';variants.append(e)
        e=copy.deepcopy(self.events);e[0]['message']['content'][0]['input']['file_path']=str(self.workspace.parent);variants.append(e)
        variants.append(self.events[:-3]+[self.events[-1]])
        variants.append(self.events+[self.events[-1]])
        variants.append([{'type':'error','message':'network failure'}]+self.events)
        for e in variants:
            with self.subTest(events=len(e)),self.assertRaises(ValueError):self.parse(e)
        (self.workspace/'index.html').write_text('')
        with self.assertRaises(ValueError):self.parse()

    def test_service_requires_exact_hashes_and_human_review(self):
        from dispatcher.core import Store
        from dispatcher.service import Service
        from tests.test_service import FakeDida,FakeRunner
        for human,bad_hash,expected in [(True,False,'awaiting_review'),(False,False,'failed'),(True,True,'failed')]:
            with self.subTest(human=human,bad_hash=bad_hash),tempfile.TemporaryDirectory() as d:
                root=Path(d);store=Store(root/'data/tasks.db')
                task={'id':'t','projectId':'p','title':'Write a document','tags':['AI'],'status':0}
                dida=FakeDida(task);runner=FakeRunner(root);base=runner.execute
                def execute(row,run):
                    report=base(row,run)
                    return dict(report,executor='codebuddy',directory_probe_recovered=True,
                        recovered_artifact_sha256={'result.md':'wrong' if bad_hash else hashlib.sha256(b'done').hexdigest()})
                runner.execute=execute
                service=Service(root,{'require_human_review':human},store,dida,runner)
                service.sync();service.execute(store.claim())
                self.assertEqual(store.get('p:t')['state'],expected)
                self.assertEqual((dida.comments,dida.completions),(0,0));store.close()

if __name__=='__main__':unittest.main()
