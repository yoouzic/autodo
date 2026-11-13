"""Human approval binds recovered artifacts and their host recovery evidence."""

import copy
import tempfile
import unittest
from pathlib import Path

from dispatcher.core import StateConflict, Store, manifest_digest


def recovered_result():
    hashes = {'answer.md': 'a' * 64}
    warning = {'kind': 'completed_eof', 'message': 'Artifacts recovered; vendor ERROR retained'}
    return {
        'status': 'complete',
        'sha256': hashes,
        'runtime_warning': copy.deepcopy(warning),
        'execution': {
            'status': 'recovered',
            'executor': 'antigravity',
            'vendor_status': 'ERROR',
            'runtime_warning': warning,
            'completion_evidence': {'stream_sha256': 'b' * 64},
            'recovered_artifact_sha256': dict(hashes),
        },
    }


class RecoveryManifestTests(unittest.TestCase):
    def test_ordinary_manifest_keeps_existing_digest_algorithm(self):
        report = {'sha256': {'dir\\b.md': ' ' + 'B' * 64 + ' ', 'a.md': 'a' * 64}}
        self.assertEqual(manifest_digest(report),
                         '601c2109446718c7b25d3b565112297722ae0844a9a5a6acf81a363f92d4bf2d')
        report.update(status='complete', summary='Changed ordinary narrative',
                      execution={'status': 'complete', 'executor': 'codex'})
        self.assertEqual(manifest_digest(report),
                         '601c2109446718c7b25d3b565112297722ae0844a9a5a6acf81a363f92d4bf2d')

    def test_recovery_digest_binds_all_completion_evidence_and_warning(self):
        report = recovered_result()
        approved_digest = manifest_digest(report)
        self.assertIsNotNone(approved_digest)
        changed = copy.deepcopy(report)
        changed['execution']['completion_evidence']['stream_sha256'] = 'c' * 64
        self.assertNotEqual(manifest_digest(changed), approved_digest)
        changed = copy.deepcopy(report)
        changed['execution']['completion_evidence']['future_detail'] = {'steps': [1, 2], 'checked': True}
        self.assertNotEqual(manifest_digest(changed), approved_digest)
        changed = copy.deepcopy(report)
        changed['runtime_warning']['message'] = 'Different warning'
        changed['execution']['runtime_warning'] = copy.deepcopy(changed['runtime_warning'])
        self.assertNotEqual(manifest_digest(changed), approved_digest)

    def test_recovery_digest_is_canonical_for_json_and_artifact_hashes(self):
        first = recovered_result()
        first['sha256'] = {'dir\\answer.md': ' A' + 'A' * 63 + ' '}
        first['execution']['recovered_artifact_sha256'] = {'dir/answer.md': 'a' * 64}
        first['execution']['completion_evidence'] = {'last_step': 7, 'stream_sha256': 'b' * 64}
        second = copy.deepcopy(first)
        second['sha256'] = {'dir/answer.md': 'a' * 64}
        second['execution']['completion_evidence'] = {'stream_sha256': 'b' * 64, 'last_step': 7}
        self.assertIsNotNone(manifest_digest(first))
        self.assertEqual(manifest_digest(first), manifest_digest(second))

    def test_missing_recovery_metadata_cannot_be_approved(self):
        for key in ('status', 'executor', 'vendor_status', 'runtime_warning',
                    'completion_evidence', 'recovered_artifact_sha256'):
            with self.subTest(execution_field=key):
                report = recovered_result()
                del report['execution'][key]
                self.assertIsNone(manifest_digest(report))
        for key in ('execution', 'runtime_warning'):
            with self.subTest(top_field=key):
                report = recovered_result()
                del report[key]
                self.assertIsNone(manifest_digest(report))

    def test_invalid_or_inconsistent_recovery_metadata_has_no_digest(self):
        cases = {
            'status': 'complete',
            'executor': 'codex',
            'vendor_status': 'SUCCESS',
            'runtime_warning': {'kind': 'completed_eof', 'message': 'Warning was replaced'},
            'completion_evidence': {},
            'recovered_artifact_sha256': {'answer.md': 'c' * 64},
        }
        for key, value in cases.items():
            with self.subTest(field=key):
                report = recovered_result()
                report['execution'][key] = value
                self.assertIsNone(manifest_digest(report))
        for evidence in ([], 'not metadata', {'invalid': float('nan')}, {'invalid': object()},
                         {'invalid': '\ud800'}):
            with self.subTest(evidence=repr(evidence)):
                report = recovered_result()
                report['execution']['completion_evidence'] = evidence
                self.assertIsNone(manifest_digest(report))

    def test_removing_all_recovery_markers_cannot_reuse_the_recovery_digest(self):
        recovered = recovered_result()
        ordinary = copy.deepcopy(recovered)
        del ordinary['execution']
        del ordinary['runtime_warning']
        self.assertIsNotNone(manifest_digest(ordinary))
        self.assertNotEqual(manifest_digest(recovered), manifest_digest(ordinary))

    def test_approved_recovery_cannot_drop_metadata_with_unchanged_artifacts(self):
        source = {'id': 'one', 'projectId': 'p', 'title': 'Draft notes', 'tags': ['AI'], 'status': 0}
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'queue.sqlite3')
            try:
                store.sync([source], {})
                row = store.claim()
                awaiting = store.mark(row['id'], 'awaiting_review',
                                      expected_run_id=row['current_run_id'],
                                      run_dir=str(Path(directory) / 'run'), result=recovered_result())
                approved = store.decide_review(awaiting['id'], awaiting['revision'],
                                               awaiting['current_run_id'], awaiting['fingerprint'],
                                               manifest_digest(awaiting['result']), True)
                stripped = copy.deepcopy(approved['result'])
                del stripped['execution']
                del stripped['runtime_warning']
                hidden_warning = copy.deepcopy(approved['result'])
                del hidden_warning['runtime_warning']
                changed_evidence = copy.deepcopy(approved['result'])
                changed_evidence['execution']['completion_evidence']['stream_sha256'] = 'c' * 64
                for changed in (stripped, hidden_warning, changed_evidence):
                    with self.subTest(changed=changed), self.assertRaises(StateConflict):
                        store.mark(approved['id'], 'writeback_pending',
                                   expected_run_id=approved['current_run_id'], result=changed)
                self.assertEqual(store.get(approved['id'])['state'], 'approved')
                self.assertEqual(store.get_run(approved['current_run_id'])['result'], approved['result'])
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
