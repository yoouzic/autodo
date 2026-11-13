import hashlib
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from dispatcher.core import Store, manifest_digest
from dispatcher.runner import CodexRunner
from dispatcher.service import Service, InstanceLock


class FakeDida:
    def __init__(self, task):
        self.task = task
        self.completions = 0
        self.comments = 0
        self.reads = 0
        self.fail = False
    def fetch_tasks(self): return [self.task], []
    def get(self, task):
        self.reads += 1
        return self.task.copy()
    def ensure_comment(self, task, marker, text): self.comments += 1
    def complete(self, task):
        self.completions += 1
        if self.fail: raise RuntimeError('network')
        self.task['status'] = 2


class FakeRunner:
    def __init__(self, root): self.root, self.executions = Path(root), 0
    def prepare(self, row):
        d = self.root/'run'
        (d/'workspace').mkdir(parents=True, exist_ok=True)
        return d
    def execute(self, row, d):
        self.executions += 1
        (d/'workspace'/'result.md').write_text('done', encoding='utf-8')
        return {'status':'complete','summary':'done','artifacts':['result.md'],'checks':['read'], 'needs_input':''}
    def verify(self, row, d, result):
        reviewed = result.copy()
        workspace = (d/'workspace').resolve()
        reviewed['sha256'] = {
            str((workspace/name).relative_to(workspace)): hashlib.sha256((workspace/name).read_bytes()).hexdigest()
            for name in reviewed['artifacts']
        }
        return reviewed


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'test.db')
        self.task = {'id':'t','projectId':'p','title':'整理清单','tags':['AI'],'priority':5,'status':0}
        self.dida, self.runner = FakeDida(self.task.copy()), FakeRunner(self.root)
        self.s = Service(self.root, {'auto_complete':True,'daily_max_runs':10,'max_attempts':2,
                                    'require_human_review':False}, self.store, self.dida, self.runner)
        self.store.sync([self.task], {})
    def tearDown(self): self.temp.cleanup()
    def pending(self):
        with patch.object(self.s, 'writeback'):
            self.s.execute(self.store.claim())
        row = self.store.get('p:t')
        self.assertEqual(row['state'], 'writeback_pending')
        return row
    def test_verified_then_complete(self):
        self.s.execute(self.store.claim())
        self.assertEqual(self.store.get('p:t')['state'], 'done')
        self.assertEqual(self.dida.completions, 1)

    def test_new_run_replaces_old_session_before_execution_failure(self):
        row=self.store.claim()
        self.store.mark(row['id'],'running',result={'executor':'codex','thread_id':'old-thread'})
        original_prepare=self.runner.prepare
        def prepare(r):
            d=original_prepare(r)
            (d/'routing.json').write_text('{"executor":"dsh","reason":"easy route"}',encoding='utf-8')
            return d
        self.runner.prepare=prepare
        self.runner.execute=lambda *args: (_ for _ in ()).throw(RuntimeError('vendor failed'))
        self.s.execute(row)
        current=self.store.get(row['id'])
        self.assertEqual(current['result']['executor'],'dsh')
        self.assertNotIn('thread_id',current['result'])

    def test_recurring_task_waits_instead_of_consuming_future_instances(self):
        self.task['repeatFlag'] = 'RRULE:FREQ=DAILY'
        self.dida.task = self.task.copy()
        self.store.sync([self.task], {})
        self.s.execute(self.store.claim())
        self.assertEqual(self.runner.executions, 0)
        self.assertEqual(self.store.get('p:t')['state'], 'needs_input')
    def test_source_change_prevents_execution(self):
        self.dida.task['title'] = 'different'
        self.s.execute(self.store.claim())
        self.assertEqual(self.runner.executions, 0)
        self.assertEqual(self.store.get('p:t')['state'], 'needs_review')
    def test_writeback_failure_retries_only_writeback(self):
        self.dida.fail = True
        self.s.execute(self.store.claim())
        self.assertEqual(self.store.get('p:t')['state'], 'writeback_pending')
        self.dida.fail = False
        self.s.writeback(self.store.get('p:t'))
        self.assertEqual(self.runner.executions, 1)
        self.assertEqual(self.store.get('p:t')['state'], 'done')
    def test_missing_artifact_cannot_complete(self):
        self.runner.execute = lambda *_: {'status':'complete','summary':'fake','artifacts':['absent.md'],'checks':[]}
        self.s.execute(self.store.claim())
        self.assertEqual(self.dida.completions, 0)
        self.assertEqual(self.store.get('p:t')['state'], 'failed')
    def test_second_lock_rejected(self):
        with InstanceLock(self.root):
            with self.assertRaises(RuntimeError):
                with InstanceLock(self.root): pass

    def test_concurrent_writeback_sends_one_comment_and_completion(self):
        row = self.pending()
        barrier = threading.Barrier(2)
        original = self.dida.ensure_comment

        def overlapping_comment(*args):
            original(*args)
            try:
                barrier.wait(timeout=0.2)
            except threading.BrokenBarrierError:
                pass

        self.dida.ensure_comment = overlapping_comment
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.s.writeback, row) for _ in range(2)]
            for future in futures:
                future.result(timeout=5)
        self.assertEqual(self.dida.comments, 1)
        self.assertEqual(self.dida.completions, 1)
        self.assertEqual(self.store.get('p:t')['state'], 'done')

    def test_late_writeback_does_not_touch_done_state(self):
        row = self.pending()
        self.s.writeback(row)
        event_count, reads = len(self.store.events()), self.dida.reads
        self.s.writeback(row)
        self.assertEqual(self.dida.reads, reads)
        self.assertEqual(len(self.store.events()), event_count)
        self.assertEqual(self.dida.completions, 1)

    def test_stale_run_or_fingerprint_is_ignored(self):
        row = self.pending()
        reads = self.dida.reads
        for key in ('fingerprint', 'run_dir'):
            with self.subTest(key=key):
                stale = dict(row, **{key: 'stale'})
                self.s.writeback(stale)
                self.assertEqual(self.store.get('p:t')['state'], 'writeback_pending')
                self.assertIsNone(self.store.get('p:t')['error'])
                self.assertEqual(self.dida.reads, reads)
        self.assertEqual(self.dida.comments, 0)
        self.assertEqual(self.dida.completions, 0)

    def test_modified_artifact_blocks_writeback(self):
        row = self.pending()
        (Path(row['run_dir'])/'workspace'/'result.md').write_text('changed after verification')
        self.s.writeback(row)
        self.assertEqual(self.store.get('p:t')['state'], 'needs_review')
        self.assertEqual(self.dida.comments, 0)
        self.assertEqual(self.dida.completions, 0)

    def test_missing_hash_blocks_writeback(self):
        row = self.pending()
        result = dict(row['result'])
        result.pop('sha256')
        self.store.mark('p:t', 'writeback_pending', result=result)
        self.s.writeback(row)
        self.assertEqual(self.store.get('p:t')['state'], 'needs_review')
        self.assertEqual(self.dida.completions, 0)

    def test_hashes_must_cover_every_artifact(self):
        row = self.pending()
        (Path(row['run_dir'])/'workspace'/'second.md').write_text('extra')
        result = dict(row['result'], artifacts=['result.md', 'second.md'])
        self.store.mark('p:t', 'writeback_pending', result=result)
        self.s.writeback(self.store.get('p:t'))
        self.assertEqual(self.store.get('p:t')['state'], 'needs_review')
        self.assertEqual(self.dida.completions, 0)

    def test_artifact_changed_during_comment_cannot_complete(self):
        row = self.pending()
        original = self.dida.ensure_comment

        def mutate_artifact(*args):
            original(*args)
            (Path(row['run_dir'])/'workspace'/'result.md').write_text('changed during comment')

        self.dida.ensure_comment = mutate_artifact
        self.s.writeback(row)
        self.assertEqual(self.store.get('p:t')['state'], 'needs_review')
        self.assertEqual(self.dida.comments, 1)
        self.assertEqual(self.dida.completions, 0)

    def test_comment_only_writeback_also_rechecks_artifacts(self):
        self.s.config['auto_complete'] = False
        row = self.pending()

        def mutate_artifact(*_):
            (Path(row['run_dir'])/'workspace'/'result.md').write_text('changed during comment')

        self.dida.ensure_comment = mutate_artifact
        self.s.writeback(row)
        self.assertEqual(self.store.get('p:t')['state'], 'needs_review')
        self.assertEqual(self.dida.completions, 0)

    def test_missing_source_status_is_rejected_as_unknown_state(self):
        row = self.store.claim()
        del self.dida.task['status']
        self.assertFalse(self.s.current(row))
        self.assertEqual(self.store.get('p:t')['error'], 'Source task is no longer incomplete')

    def test_explicit_string_zero_status_is_allowed(self):
        row = self.store.claim()
        self.dida.task['status'] = '0'
        self.assertTrue(self.s.current(row))


class HumanReviewServiceTests(unittest.TestCase):
    def setUp(self):
        ServiceTests.setUp(self)
        self.s.config.pop('require_human_review')

    tearDown = ServiceTests.tearDown

    def verified(self):
        self.s.execute(self.store.claim())
        row = self.store.get('p:t')
        self.assertEqual(row['state'], 'awaiting_review')
        return row

    def approve(self, row):
        return self.store.decide_review(row['id'], row['revision'], row['current_run_id'],
                                        row['fingerprint'], manifest_digest(row['result']), True)

    def test_default_stops_after_machine_review_without_external_writes(self):
        row = self.verified()
        self.assertEqual(self.dida.comments, 0)
        self.assertEqual(self.dida.completions, 0)
        self.assertEqual(self.store.get_run(row['current_run_id'])['review_status'], 'pending')

    def test_human_approval_allows_writeback_on_tick(self):
        row = self.approve(self.verified())
        self.s.process_writebacks()
        self.assertEqual(self.store.get(row['id'])['state'], 'done')
        self.assertEqual(self.dida.comments, 1)
        self.assertEqual(self.dida.completions, 1)

    def test_legacy_pending_requires_review_before_any_write(self):
        self.s.config['require_human_review'] = False
        with patch.object(self.s, 'writeback'):
            self.s.execute(self.store.claim())
        self.s.config.pop('require_human_review')
        self.s.writeback(self.store.get('p:t'))
        self.assertEqual(self.store.get('p:t')['state'], 'awaiting_review')
        self.assertEqual(self.dida.comments, 0)
        self.assertEqual(self.dida.completions, 0)

    def test_direct_private_writeback_cannot_bypass_review(self):
        row = self.verified()
        self.s._writeback(row)
        self.assertEqual(self.dida.comments, 0)
        self.assertEqual(self.dida.completions, 0)

    def test_manually_delegated_untagged_task_can_execute(self):
        self.task['tags'] = []
        self.dida.task = self.task.copy()
        self.store.sync([self.task], {})
        row = self.store.get('p:t')
        row = self.store.withdraw(row['id'], row['revision'])
        self.store.assign(row['id'], 'codex', row['revision'], self.task, {})
        self.verified()
        self.assertEqual(self.runner.executions, 1)

    def test_late_worker_error_cannot_override_reassigned_run(self):
        original = self.store.claim()
        self.store.mark(original['id'], 'failed', expected_run_id=original['current_run_id'])
        row = self.store.get(original['id'])
        self.store.assign(row['id'], 'codex', row['revision'], self.task, {})
        new = self.store.claim()
        self.runner.execute = lambda *args: (_ for _ in ()).throw(RuntimeError('late old failure'))
        self.s.execute(original)
        current = self.store.get(new['id'])
        self.assertEqual(current['current_run_id'], new['current_run_id'])
        self.assertEqual(current['state'], 'running')
        self.assertIsNone(current['error'])

    def test_approval_invalidated_before_comment_never_writes(self):
        row = self.approve(self.verified())
        self.dida.task['title'] = 'Changed source'
        self.s.process_writebacks()
        self.assertEqual(self.store.get(row['id'])['state'], 'needs_review')
        self.assertEqual(self.dida.comments, 0)
        self.assertEqual(self.dida.completions, 0)

    def test_rejection_feedback_is_in_execution_prompt(self):
        runner = CodexRunner(self.root, {'codex_command': ['codex']})
        row = self.store.get('p:t')
        row['feedback'] = '补充来源，并重做第三段'
        self.assertIn(row['feedback'], runner.prompt(row))

    def test_late_worker_exception_after_reassignment_does_not_mark_new_run_failed(self):
        original = self.store.claim()
        def late_failure(*args):
            self.store.mark(original['id'], 'failed', expected_run_id=original['current_run_id'])
            failed = self.store.get(original['id'])
            self.store.assign(failed['id'], 'codex', failed['revision'], self.task, {})
            self.store.claim()
            raise RuntimeError('Failure from the old worker')
        self.runner.execute = late_failure
        self.s.execute(original)
        current = self.store.get(original['id'])
        self.assertNotEqual(current['current_run_id'], original['current_run_id'])
        self.assertEqual(current['state'], 'running')
        self.assertIsNone(current['error'])

    def test_legacy_pending_without_result_is_sent_to_review(self):
        row = self.store.claim()
        row = self.store.mark(row['id'], 'writeback_pending')
        self.s.writeback(row)
        self.assertEqual(self.store.get(row['id'])['state'], 'needs_review')
        self.assertEqual(self.dida.comments, 0)

    def test_daemon_recovers_commands_and_processes_control_and_approval_each_tick(self):
        row = self.approve(self.verified())
        command = self.store.submit_command('sync', {}, 'interrupted')
        self.store.next_command()
        self.s.config['daily_max_runs'] = 0
        control = Mock()
        control.process_pending.side_effect = self.s.stop.set
        self.s.control = control
        self.s.daemon()
        control.process_pending.assert_called_once_with()
        self.assertEqual(self.store.get_command(command['id'])['status'], 'failed')
        self.assertEqual(self.store.get(row['id'])['state'], 'done')
        self.assertEqual(self.dida.completions, 1)


if __name__ == '__main__': unittest.main()
