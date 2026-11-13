import json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from dispatcher.core import Store
from dispatcher.service import Service
from dispatcher.startup import Supervisor

class BackgroundEfficiencyTests(unittest.TestCase):
    def test_status_snapshot_is_throttled_and_identical_content_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'data').mkdir()
            service=Service(root,{},Mock(),Mock(),Mock())
            service.snapshot=Mock(return_value={'counts':{'ignored':48}})
            for tick in range(20):service.publish_status(now=tick)
            self.assertEqual(service.snapshot.call_count,4)
            path=root/'data/status.json';before=path.stat().st_mtime_ns
            service.publish_status(now=20)
            self.assertEqual(path.stat().st_mtime_ns,before)
            service.snapshot.return_value={'counts':{'awaiting_review':1}}
            service.publish_status(now=21,force=True)
            self.assertEqual(json.loads(path.read_text())['counts'],{'awaiting_review':1})

    def test_writeback_scan_does_not_deserialize_unrelated_results(self):
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'tasks.db')
            store.sync([{'id':'t','projectId':'p','title':'ordinary','status':0}],{})
            with patch.object(store,'_row',wraps=store._row) as decode:
                self.assertEqual(store.list_tasks(states=('approved','writeback_pending')),[])
                decode.assert_not_called()
            self.assertEqual(len(store.list_tasks()),1)
            self.assertEqual(store.list_tasks(states=()),[])
            with self.assertRaises(ValueError):store.list_tasks(states=('not-a-state',))
            store.close()

    def test_windowless_supervisor_uses_console_python_for_captured_daemon_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            python=Path(directory)/'python.exe'
            supervisor=Supervisor(directory,python_executable=str(python))
            self.assertEqual(supervisor.command()[0],str(python))

if __name__=='__main__':unittest.main()
