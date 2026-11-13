import hashlib
import unittest
from unittest.mock import Mock

from tests import test_service
from dispatcher.control import Controller


class EofRecoveryPipelineTests(unittest.TestCase):
    setUp = test_service.ServiceTests.setUp
    tearDown = test_service.ServiceTests.tearDown
    def recovered_runner(self):
        self.s.config['require_human_review'] = True
        original = self.runner.execute

        def execute(row, run):
            report = original(row, run)
            report.update(status='recovered', executor='antigravity', vendor_status='ERROR',
                          runtime_warning={'kind': 'completed_eof', 'message': 'Recovered EOF artifacts'},
                          completion_evidence={'event_stream_sha256': 'a' * 64, 'finish_step_index': 3},
                          recovered_artifact_sha256={'result.md': hashlib.sha256(b'done').hexdigest()})
            return report

        def verify(row, run, report):
            result = test_service.FakeRunner.verify(self.runner, row, run, report)
            result['status'] = 'complete'
            return result

        self.runner.execute = execute
        self.runner.verify = Mock(side_effect=verify)

    def test_recovered_artifacts_require_independent_verification_and_human_review(self):
        self.recovered_runner()
        self.s.execute(self.store.claim())
        current = self.store.get('p:t')
        self.runner.verify.assert_called_once()
        self.assertEqual(current['state'], 'awaiting_review')
        self.assertEqual(current['result']['execution']['status'], 'recovered')
        self.assertEqual(current['result']['execution']['vendor_status'], 'ERROR')
        self.assertEqual(current['result']['runtime_warning']['kind'], 'completed_eof')
        projection = Controller(self.s, Mock(), []).task(current)
        self.assertIn('EOF', projection['runtime_warning'])
        self.assertIn('ERROR', projection['runtime_warning'])
        self.assertTrue(projection['can_approve'])
        self.assertEqual(self.dida.completions, 0)
        self.assertEqual(self.dida.comments, 0)

    def test_recovery_is_refused_when_human_review_disabled(self):
        self.recovered_runner()
        self.s.config['require_human_review'] = False
        self.s.execute(self.store.claim())
        self.runner.verify.assert_not_called()
        self.assertEqual(self.store.get('p:t')['state'], 'failed')
        self.assertEqual(self.dida.completions, 0)

    def test_recovery_does_not_bypass_failed_artifact_verification(self):
        self.recovered_runner()
        self.runner.verify.side_effect = lambda *args: {'status': 'failed', 'summary': 'Wrong content',
            'artifacts': ['result.md'], 'checks': ['Read actual file'], 'needs_input': ''}
        self.s.execute(self.store.claim())
        current = self.store.get('p:t')
        self.assertEqual(current['state'], 'needs_review')
        self.assertEqual(current['result']['execution']['vendor_status'], 'ERROR')
        self.assertEqual(self.dida.completions, 0)

    def test_changed_recovery_artifact_is_rejected_before_verifier(self):
        self.recovered_runner()
        original = self.runner.execute

        def tamper(row, run):
            report = original(row, run)
            (run/'workspace/result.md').write_text('changed', encoding='utf-8')
            return report

        self.runner.execute = tamper
        self.s.execute(self.store.claim())
        self.runner.verify.assert_not_called()
        self.assertEqual(self.store.get('p:t')['state'], 'failed')
        self.assertEqual(self.dida.completions, 0)

    def test_later_configuration_change_cannot_remove_recovery_review_gate(self):
        self.recovered_runner()
        self.s.execute(self.store.claim())
        row = self.store.get('p:t')
        self.s.config['require_human_review'] = False
        self.store.mark(row['id'], 'writeback_pending')
        self.s.writeback(self.store.get(row['id']))
        self.assertEqual(self.dida.completions, 0)
        self.assertEqual(self.store.get(row['id'])['state'], 'awaiting_review')

    def test_verifier_cannot_substitute_another_file_for_recovered_artifacts(self):
        self.recovered_runner()
        def substitute(row, run, report):
            (run/'workspace/other.md').write_text('other', encoding='utf-8')
            return {'status': 'complete', 'summary': 'different file', 'artifacts': ['other.md'],
                    'checks': ['read'], 'needs_input': '',
                    'sha256': {'other.md': hashlib.sha256(b'other').hexdigest()}}
        self.runner.verify.side_effect = substitute
        self.s.execute(self.store.claim())
        self.assertEqual(self.store.get('p:t')['state'], 'failed')
        self.assertEqual(self.dida.completions, 0)

    def test_nested_recovery_accepts_equivalent_windows_hash_path(self):
        self.recovered_runner()
        original = self.runner.execute
        def nested(row, run):
            report = original(row, run)
            (run/'workspace/docs').mkdir()
            (run/'workspace/result.md').rename(run/'workspace/docs/result.md')
            report['artifacts'] = ['docs/result.md']
            report['recovered_artifact_sha256'] = {'docs/result.md': hashlib.sha256(b'done').hexdigest()}
            return report
        self.runner.execute = nested
        def verify(row, run, report):
            return {'status': 'complete', 'summary': 'checked', 'artifacts': ['docs/result.md'],
                    'checks': ['read file'], 'needs_input': '',
                    'sha256': {'docs\\result.md': hashlib.sha256(b'done').hexdigest()}}
        self.runner.verify.side_effect = verify
        self.s.execute(self.store.claim())
        self.assertEqual(self.store.get('p:t')['state'], 'awaiting_review')
        self.assertEqual(self.dida.completions, 0)
