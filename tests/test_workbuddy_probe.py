"""Fixed probe inbox tests use fake time and no model or production workspace."""
import importlib.util
import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path


class FakeBroker:
    def __init__(self):
        self._lock = threading.RLock()
        self.value = {'online': True, 'ready': True, 'active_run_id': None, 'queued_count': 0,
                      'wait_calls': 1, 'wait_active': True, 'progress_token_seen': True,
                      'progress_count': 1, 'connection_generation': 1}
        self.submissions = []
        self.results = {}
        self.cancelled = []
        self.bindings = {}
    def status(self): return dict(self.value)
    def submit(self, run_id, workspace, prompt, **kwargs):
        self.submissions.append((run_id, workspace, prompt, kwargs))
        self.results[run_id] = {'run_id': run_id, 'state': 'queued', 'submission': None, 'artifact_sha256': {}}
    def result(self, run_id): return self.results[run_id]
    def cancel(self, run_id): self.cancelled.append(run_id); return True
    def _bind_probe_wait(self, run_id, wait_calls, connection_generation):
        self.bindings[run_id] = {'wait_calls': wait_calls, 'connection_generation': connection_generation}


class WorkBuddyProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'dispatcher'
        self.root.mkdir()
        (self.root / 'workspaces').mkdir()
        self.now = [100.0]
        self.broker = FakeBroker()

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('dispatcher.workbuddy_probe'),
                             'Fixed WorkBuddy probe inbox is not implemented')
        from dispatcher import workbuddy_probe
        return workbuddy_probe

    def setup_inbox(self, epoch='1' * 64):
        module = self.module()
        inbox = module.ProbeInbox(self.root, epoch, self.broker, clock=lambda: self.now[0])
        return module, inbox

    def test_no_model_job_before_same_wait_is_ready_for_seventy_seconds(self):
        module, inbox = self.setup_inbox()
        request_id = module.request_probe(self.root)
        inbox.tick()
        self.assertEqual(module.get_probe_result(self.root, request_id)['state'], 'waiting')
        self.now[0] += 69
        inbox.tick()
        self.assertFalse(self.broker.submissions)
        self.now[0] += 1
        self.broker.value['progress_count'] = 8
        inbox.tick()
        self.assertEqual(len(self.broker.submissions), 1)
        run_id, workspace, prompt, options = self.broker.submissions[0]
        self.assertEqual(options, {'workspace_mode': 'default', 'timeout': 180, 'priority': 0})
        self.assertEqual(workspace, self.root / 'workspaces' / 'workbuddy-probes' / request_id / 'workspace')
        self.assertEqual((workspace / 'seed.txt').read_bytes(), b'KEEP\n')
        self.assertIn('probe.txt', prompt)
        receipt = module.get_probe_result(self.root, request_id)
        self.assertEqual(receipt['state'], 'running')
        self.assertEqual(receipt['wait_evidence']['wait_calls'], 1)
        self.assertGreaterEqual(receipt['wait_evidence']['idle_wait_seconds'], 70)
        self.assertEqual(receipt['wait_evidence']['progress_count'], 8)
        for _ in range(3): inbox.tick()
        self.assertEqual(len(self.broker.submissions), 1)

    def test_result_requires_exact_seed_output_and_report_hash(self):
        import hashlib
        module, inbox = self.setup_inbox()
        request_id = module.request_probe(self.root)
        inbox.tick(); self.now[0] += 70; inbox.tick()
        run_id, workspace, _, _ = self.broker.submissions[0]
        (workspace / 'probe.txt').write_bytes(b'READY\n')
        self.broker.results[run_id] = {'run_id': run_id, 'state': 'submitted',
            'submission': {'status': 'complete', 'summary': 'fixed probe done', 'artifacts': ['probe.txt'],
                           'checks': ['seed checked'], 'needs_input': ''},
            'artifact_sha256': {'probe.txt': hashlib.sha256(b'READY\n').hexdigest()},
            'claim_evidence': {'expected': self.broker.bindings[run_id], 'actual': self.broker.bindings[run_id],
                               'matched': True, 'preempted': False, 'wait_active': True}}
        inbox.tick()
        result = module.get_probe_result(self.root, request_id)
        self.assertEqual(result['state'], 'passed')
        self.assertEqual(result['result']['submission']['artifacts'], ['probe.txt'])
        self.assertEqual(result['workspace'], str(workspace))
        self.assertNotIn('host_token', json.dumps(result))

    def test_changing_wait_call_or_losing_ready_fails_instead_of_resetting_clock(self):
        for changes in ({'wait_calls': 2}, {'ready': False}, {'online': False}, {'queued_count': 1}):
            with self.subTest(changes=changes):
                module, inbox = self.setup_inbox(epoch=('%064x' % (len(self.broker.submissions) + int(time.time_ns())))[-64:])
                request_id = module.request_probe(self.root)
                inbox.tick()
                self.now[0] += 10
                self.broker.value.update(changes)
                inbox.tick()
                self.assertEqual(module.get_probe_result(self.root, request_id)['state'], 'failed')
                self.assertFalse(self.broker.submissions)
                self.broker = FakeBroker()

    def test_concurrent_request_and_replay_do_not_dispatch_twice(self):
        module, inbox = self.setup_inbox()
        request_id = module.request_probe(self.root)
        with self.assertRaises(module.ProbeError): module.request_probe(self.root)
        inbox.tick(); self.now[0] += 70; inbox.tick()
        self.assertEqual(len(self.broker.submissions), 1)
        self.broker.results[self.broker.submissions[0][0]]['state'] = 'cancelled'
        inbox.tick()
        for _ in range(3): inbox.tick()
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(module.get_probe_result(self.root, request_id)['state'], 'failed')

    def test_queue_race_is_rechecked_before_workspace_creation_or_submission(self):
        module, inbox = self.setup_inbox()
        request_id = module.request_probe(self.root)
        inbox.tick()
        self.now[0] += 70
        count = [0]
        original = self.broker.status
        def raced_status():
            count[0] += 1
            if count[0] == 2:
                self.assertTrue(self.broker._lock._is_owned())
                self.broker.value['queued_count'] = 1
            return original()
        self.broker.status = raced_status
        inbox.tick()
        self.assertFalse(self.broker.submissions)
        self.assertFalse((self.root / 'workspaces/workbuddy-probes' / request_id).exists())
        self.assertEqual(module.get_probe_result(self.root, request_id)['state'], 'failed')

    def test_connection_generation_change_invalidates_same_numbered_wait(self):
        module, inbox = self.setup_inbox()
        request_id = module.request_probe(self.root)
        inbox.tick(); self.now[0] += 70
        self.broker.value['connection_generation'] += 1
        inbox.tick()
        self.assertFalse(self.broker.submissions)
        self.assertEqual(module.get_probe_result(self.root, request_id)['state'], 'failed')

    def test_tampered_request_custom_fields_and_old_epoch_are_rejected(self):
        module, inbox = self.setup_inbox()
        request_id = module.request_probe(self.root)
        path = self.root / 'data/workbuddy-probe/request.json'
        value = json.loads(path.read_text())
        value['prompt'] = 'Do something else'
        path.write_text(json.dumps(value))
        inbox.tick()
        self.assertFalse(self.broker.submissions)
        self.assertNotEqual(module.get_probe_result(self.root, request_id)['state'], 'passed')
        other = module.ProbeInbox(self.root, '2' * 64, self.broker, clock=lambda: self.now[0])
        other.tick()
        self.assertFalse(self.broker.submissions)

    def test_readonly_lookup_does_not_create_directories(self):
        module = self.module()
        absent = self.root / 'absent'
        with self.assertRaises(module.ProbeError): module.get_probe_result(absent, 'a' * 32)
        self.assertFalse(absent.exists())

    def test_fixed_prompt_uses_explicit_frame_but_still_requires_exact_newline(self):
        module=self.module()
        example=re.search(r'JSON 示例[^\n]*：(\{[^\n]+\})。另外',module._PROMPT)
        self.assertIsNotNone(example)
        params=json.loads(example.group(1))
        self.assertEqual(params['text_format'],'framed')
        from dispatcher.workbuddy_mcp import _write_text
        self.assertEqual(_write_text(params['text'].strip(),params['text_format']),'READY\n')

    def test_old_inbox_cannot_overwrite_new_epoch_receipt(self):
        module, old = self.setup_inbox()
        first = module.request_probe(self.root)
        old.tick()
        new = module.ProbeInbox(self.root, '2' * 64, self.broker, clock=lambda: self.now[0])
        second = module.request_probe(self.root)
        new.tick()
        before = (self.root / 'data/workbuddy-probe/receipt.json').read_bytes()
        self.now[0] += 10
        self.broker.value['online'] = False
        old.tick()
        self.assertEqual((self.root / 'data/workbuddy-probe/receipt.json').read_bytes(), before)
        self.assertEqual(module.get_probe_result(self.root, second)['state'], 'waiting')


if __name__ == '__main__': unittest.main()
