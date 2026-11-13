import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dispatcher.adapters import Dida
from dispatcher.core import Store
from dispatcher.service import Service
from tests.test_service import FakeDida, FakeRunner


def source(identifier='one', **changes):
    return dict({'id':identifier,'projectId':'p','title':'Sync test','status':0,'tags':[]},**changes)


class Source:
    def __init__(self, active, results):
        self.active,self.results,self.probes=active,results,[]
    def fetch_tasks(self): return self.active, []
    def probe(self, task, *, timeout):
        self.probes.append((task['id'],timeout))
        value=self.results[task['id']]
        if isinstance(value,Exception): raise value
        return value


class SourceSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=Store(self.root/'tasks.db')
    def tearDown(self):
        self.store.close();self.temp.cleanup()
    def service(self, active, results):
        self.source=Source(active,results)
        return Service(self.root,{},self.store,self.source,None)

    def test_missing_completed_task_is_confirmed_without_source_write(self):
        self.store.sync([source()],{})
        service=self.service([],{'one':source(status=2)})
        service.sync()
        self.assertEqual(self.store.get('p:one').get('source_status'),2)
        self.assertEqual(len(self.source.probes),1)
        service.sync()
        self.assertEqual(len(self.source.probes),1,'Confirmed completion must not be probed every cycle')

    def test_failure_does_not_complete_task_or_block_other_reconciliation(self):
        self.store.sync([source('a'),source('b')],{})
        service=self.service([],{'a':TimeoutError('private upstream details'),'b':source('b',status=2)})
        service.sync()
        self.assertNotEqual(self.store.get('p:a').get('source_status'),2)
        self.assertEqual(self.store.get('p:b').get('source_status'),2)
        self.assertTrue(service.warnings)
        self.assertNotIn('private upstream',str(service.warnings))
        count=len(self.source.probes);service.sync()
        self.assertEqual(len(self.source.probes),count,'Unknown source should back off')

    def test_manual_sync_can_retry_backoff_without_inventing_completion(self):
        self.store.sync([source()],{})
        service=self.service([],{'one':RuntimeError('gone')})
        service.sync();service.sync(force=True)
        self.assertEqual(len(self.source.probes),2)
        self.assertNotEqual(self.store.get('p:one').get('source_status'),2)

    def test_wrong_identity_or_unknown_status_is_not_completion(self):
        for fresh in (source('other',status=2),source(projectId='wrong',status=2),source(status=None)):
            with self.subTest(fresh=fresh):
                self.store.sync([source()],{})
                service=self.service([],{'one':fresh});service.sync()
                self.assertNotEqual(self.store.get('p:one').get('source_status'),2)

    def test_active_result_and_reopened_source_do_not_need_extra_probe(self):
        self.store.sync([source()],{})
        service=self.service([source(title='Changed')],{})
        service.sync()
        self.assertEqual(self.source.probes,[])
        self.assertEqual(self.store.get('p:one')['payload']['title'],'Changed')

    def test_missing_but_still_active_retains_current_source_data(self):
        self.store.sync([source()],{})
        service=self.service([],{'one':source(title='Still active')})
        service.sync()
        self.assertEqual(self.store.get('p:one')['payload']['title'],'Still active')
        self.assertNotEqual(self.store.get('p:one').get('source_status'),2)

    def test_probe_work_is_bounded_per_cycle(self):
        rows=[source(str(i)) for i in range(35)]
        self.store.sync(rows,{})
        service=self.service([], {r['id']:dict(r,status=2) for r in rows})
        service.sync()
        self.assertGreater(len(self.source.probes),0)
        self.assertLessEqual(len(self.source.probes),20)
        self.assertTrue(all(0<t<=10 for _,t in self.source.probes))
        service.sync()
        self.assertEqual(len(self.source.probes),35)

    def test_missing_active_prefix_does_not_starve_later_completions(self):
        rows=[source(str(i)) for i in range(35)]
        self.store.sync(rows,{})
        service=self.service([], {r['id']:dict(r,status=0 if i<20 else 2) for i,r in enumerate(rows)})
        service.sync();service.sync()
        self.assertEqual(sum(row.get('source_status')==2 for row in self.store.list_tasks()),15)

    def test_legacy_done_task_is_confirmed_once_without_losing_history(self):
        self.store.sync([source()],{})
        self.store.mark('p:one','done')
        with self.store._transaction() as connection:
            connection.execute('UPDATE tasks SET source_checked_at=NULL WHERE id=?',('p:one',))
        service=self.service([],{'one':source(status=2)})
        service.sync();service.sync()
        self.assertEqual(len(self.source.probes),1)
        self.assertEqual(self.store.get('p:one')['source_status'],2)
        self.assertEqual(self.store.get('p:one')['state'],'done')


class ProbeAdapterTests(unittest.TestCase):
    def test_ordinary_get_rejects_wrong_project_identity(self):
        dida=Dida(['dida'],command=lambda args:source(projectId='other',status=2))
        with self.assertRaises(ValueError): dida.get(source())

    def test_probe_passes_short_timeout_and_validates_both_identity_fields(self):
        dida=Dida(['dida'])
        with patch('dispatcher.adapters.run_process',return_value=(' {"id":"one","projectId":"p","status":2}', '')) as run:
            result=dida.probe(source(),timeout=5)
        self.assertEqual(result['status'],2)
        self.assertEqual(run.call_args.kwargs['timeout'],5)
        with patch('dispatcher.adapters.run_process',return_value=(' {"id":"one","projectId":"other","status":2}', '')):
            with self.assertRaises(ValueError): dida.probe(source(),timeout=5)


class CompletionReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=Store(self.root/'tasks.db')
        self.task=source(tags=['AI'])
        self.dida=FakeDida(self.task.copy())
        self.service=Service(self.root,{'require_human_review':False},self.store,self.dida,FakeRunner(self.root))
        self.store.sync([self.task],{})
    def tearDown(self):
        self.store.close();self.temp.cleanup()
    def pending(self):
        with patch.object(self.service,'writeback'):self.service.execute(self.store.claim())
        return self.store.get('p:one')

    def test_successful_completion_records_source_status_and_detects_reopen(self):
        self.service.execute(self.store.claim())
        done=self.store.get('p:one')
        self.assertEqual(done['source_status'],2)
        self.assertEqual(done['payload']['status'],2)
        self.dida.task['status']=0
        self.service.sync()
        self.assertNotEqual(self.store.get('p:one')['state'],'done')

    def test_completion_ack_records_fresh_status_and_preserves_run(self):
        row=self.pending();self.dida.task['status']=2
        self.service.writeback(row)
        done=self.store.get(row['id'])
        self.assertEqual(done['state'],'done')
        self.assertEqual(done['source_status'],2)
        self.assertEqual(done['payload']['status'],2)
        self.assertEqual(done['current_run_id'],row['current_run_id'])
        self.assertEqual(self.dida.comments,0)
        self.assertEqual(self.dida.completions,0)

    def test_completed_but_edited_source_cannot_borrow_old_verified_result(self):
        row=self.pending();self.dida.task.update(status=2,title='Different source content')
        self.service.writeback(row)
        cancelled=self.store.get(row['id'])
        self.assertEqual(cancelled['state'],'cancelled')
        self.assertEqual(cancelled['source_status'],2)
        self.assertEqual(cancelled['current_run_id'],row['current_run_id'])
        self.assertEqual(self.dida.comments,0)
        self.assertEqual(self.dida.completions,0)


if __name__=='__main__': unittest.main()
