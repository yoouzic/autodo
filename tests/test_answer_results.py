import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dispatcher.core import Store, manifest_digest, StateConflict
from dispatcher.runner import CodexRunner
from dispatcher.service import Service
from tests.test_service import FakeDida


ANSWER = {'protocol_version': 2, 'delivery_type': 'answer', 'status': 'complete',
          'summary': '两本书共需归还 3 次。', 'answer_markdown': '先归还第一本，再归还第二本。',
          'artifacts': [], 'checks': ['核对所给规则'], 'needs_input': '',
          'evidence_refs': [{'kind': 'task_input', 'label': '题目规则', 'detail': '以题目中给定规则为依据'}],
          'limitations': []}


class AnswerResultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def protocol(self):
        self.assertIsNotNone(importlib.util.find_spec('dispatcher.result_protocol'), 'Answer protocol missing')
        from dispatcher import result_protocol
        return result_protocol

    def test_complete_answer_requires_real_content_and_no_artifact(self):
        p = self.protocol()
        self.assertEqual(p.validate_answer(ANSWER), ANSWER)
        for change in ({'answer_markdown': ''}, {'answer_markdown': '  '}, {'artifacts': ['fake.md']},
                       {'protocol_version': True}, {'evidence_refs': [{'kind': 'tool_verified', 'label': 'x', 'detail': 'untrusted'}]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                p.validate_answer(dict(ANSWER, **change))

    def test_delivery_cannot_silently_downgrade_files_and_unsupported_agents(self):
        p = self.protocol()
        row = {'payload': {'title': '请概括三点，不需要改文件'}, 'kind': 'document'}
        self.assertEqual(p.resolve_delivery(row, 'codex')['mode'], 'answer')
        self.assertEqual(p.resolve_delivery(dict(row, delivery_mode='files'), 'codex')['mode'], 'files')
        self.assertEqual(p.resolve_delivery(row, 'qoder')['mode'], 'files')
        with self.assertRaises(ValueError):
            p.resolve_delivery(dict(row, delivery_mode='answer'), 'qoder')
        row['payload']['title'] = '解释算法并生成 README.md'
        self.assertEqual(p.resolve_delivery(row, 'codex')['mode'], 'files')

    def test_service_review_and_writeback_bind_the_answer(self):
        self.protocol()
        from dispatcher.result_receipt import freeze_answer
        store = Store(self.root/'test.db')
        task = {'id': 'a', 'projectId': 'p', 'title': '根据所给规则回答问题，不需要改文件', 'tags': ['AI'], 'status': 0}
        source = FakeDida(task.copy()); store.sync([task], {})
        runner = CodexRunner(self.root, {'model': 'test'})
        config = {'require_human_review': True, 'max_attempts': 2, 'write_comments': False}
        service = Service(self.root, config, store, source, runner)
        verdict = {'status': 'complete', 'summary': '已核验答案', 'artifacts': [], 'checks': ['对照给定规则检查答案'], 'needs_input': ''}
        def invoke(run, prompt, phase, *args, **kwargs):
            return copy.deepcopy(ANSWER if phase == 'execution' else verdict)
        with patch.object(runner, '_invoke', side_effect=invoke):
            service.execute(store.claim())
        row = store.get('p:a')
        self.assertEqual(row['state'], 'awaiting_review', row.get('error'))
        self.assertEqual(source.completions, 0)
        digest = manifest_digest(row['result']); self.assertTrue(digest)
        for change in ('answer_markdown', 'summary', 'evidence_refs', 'limitations'):
            bad = copy.deepcopy(row['result']); bad['execution'][change] = 'tampered'
            self.assertIsNone(manifest_digest(bad))
        approved = store.decide_review(row['id'], row['revision'], row['current_run_id'], row['fingerprint'], digest, True)
        bad = copy.deepcopy(approved['result']); bad['execution']['answer_markdown'] = 'changed'
        with self.assertRaises(StateConflict): store.mark(row['id'], 'approved', result=bad)
        service.process_writebacks()
        self.assertEqual(store.get('p:a')['state'], 'done')
        self.assertEqual(source.completions, 1)

    def test_receipt_disk_and_identity_tampering_rejected(self):
        self.protocol()
        from dispatcher.result_receipt import freeze_answer, check_answer_receipt
        row = {'id': 'p:t', 'fingerprint': 'f', 'current_run_id': 'a'*32, '_delivery_policy': {'mode': 'answer'}}
        receipt, digest = freeze_answer(self.root, row, ANSWER)
        result = {'status': 'complete', 'checks': ['checked'], 'execution': copy.deepcopy(ANSWER),
                  'result_receipt': receipt, 'review_digest': digest, 'verified_digest': digest, 'artifacts': [], 'sha256': {}}
        row['result'] = result
        self.assertEqual(manifest_digest(result), digest)
        check_answer_receipt(self.root, row)
        for key in ('id', 'fingerprint', 'current_run_id'):
            with self.assertRaises(ValueError): check_answer_receipt(self.root, dict(row, **{key: 'changed'}))
        bad = copy.deepcopy(result)
        bad['execution']['file_changes'] = {'status': 'verified', 'added': 0, 'modified': 0, 'deleted': 0}
        self.assertIsNone(manifest_digest(bad))
        path = self.root/'data/result-receipts'/('a'*32+'.json')
        path.write_text('{}', encoding='utf-8')
        with self.assertRaises(ValueError): check_answer_receipt(self.root, row)
        with self.assertRaises((FileExistsError, ValueError)): freeze_answer(self.root, row, ANSWER)

    def test_codebuddy_answer_tools_and_schema_preserved(self):
        p = self.protocol()
        from dispatcher.codebuddy import build_invocation, normalize_result
        run = self.root/'run'; workspace = run/'workspace'; workspace.mkdir(parents=True)
        argv, _, _, _ = build_invocation(['node', 'cb'], workspace, run, 'answer', {}, run/'runtime',
                                         schema=p.ANSWER_SCHEMA, home=self.root, readonly=True)
        self.assertEqual(argv[argv.index('--tools')+1], 'Read,StructuredOutput')
        permissions = json.loads(argv[argv.index('--settings')+1])['permissions']['allow']
        self.assertEqual(permissions, ['StructuredOutput'])
        stream = json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'structured_output': ANSWER})
        self.assertEqual(normalize_result(stream)['answer_markdown'], ANSWER['answer_markdown'])

    def test_assignment_and_projection_do_not_expose_full_receipts_in_state(self):
        from dispatcher.control import Controller
        from dispatcher.workspace import WorkspaceManager
        store = Store(self.root/'db')
        task = {'id': 'a', 'projectId': 'p', 'title': '一个任务', 'status': 0}
        store.sync([task], {})
        service = Service(self.root, {}, store, FakeDida(task), CodexRunner(self.root, {}))
        control = Controller(service, WorkspaceManager(self.root, store), [{'name': 'codex', 'enabled': True}])
        row = store.get('p:a')
        command = control.submit({'action': 'assign', 'request_id': 'answer-select', 'task_id': row['id'],
                                  'revision': row['revision'], 'executor': 'codex', 'delivery_mode': 'answer'})
        control.process_pending()
        self.assertEqual(store.get_command(command['id'])['status'], 'done')
        self.assertEqual(store.get('p:a')['delivery_mode'], 'answer')
        claimed = store.claim()
        store.mark(claimed['id'], 'needs_input', result=dict(ANSWER, status='needs_input', needs_input='提供规则'))
        listed = control.state()
        self.assertNotIn('answer_markdown', json.dumps(listed))
        self.assertEqual(control.detail('p:a')['task']['delivery']['answer_markdown'], ANSWER['answer_markdown'])

    def test_service_cannot_send_empty_or_changed_answer_for_review(self):
        for mode in ('empty', 'file_write', 'verifier_failed', 'no_checks'):
            with self.subTest(mode=mode):
                root = self.root/mode; root.mkdir()
                store = Store(root/'db')
                task = {'id': 't', 'projectId': 'p', 'title': '只回答，不需要改文件', 'tags': ['AI'], 'status': 0}
                source = FakeDida(task.copy()); store.sync([task], {})
                runner = CodexRunner(root, {'model': 'test'})
                service = Service(root, {'require_human_review': True}, store, source, runner)
                def invoke(run, prompt, phase, *args, **kwargs):
                    if phase == 'execution':
                        result = copy.deepcopy(ANSWER)
                        if mode == 'empty': result['answer_markdown'] = ''
                        if mode == 'file_write': (Path(run)/'workspace/unwanted.txt').write_text('unexpected')
                        return result
                    return {'status': 'failed' if mode == 'verifier_failed' else 'complete', 'summary': 'review',
                            'artifacts': [], 'checks': [] if mode == 'no_checks' else ['checked'], 'needs_input': ''}
                with patch.object(runner, '_invoke', side_effect=invoke): service.execute(store.claim())
                row = store.get('p:t')
                self.assertIn(row['state'], ('failed', 'needs_review'))
                self.assertEqual(source.completions, 0)
                self.assertIsNone(manifest_digest(row['result']))

    def test_answer_permission_profile_has_no_workspace_write_grant(self):
        from dispatcher.executors import sandbox_command
        workspace = self.root/'workspace'; runtime = self.root/'runtime'
        argv = sandbox_command(['codex'], ['node','worker'], workspace, state_roots=[runtime], readonly=True)
        text = ' '.join(argv)
        self.assertIn('extends=":read-only"', text)
        self.assertNotIn('workspace-write', text)
        self.assertNotIn('extends=":workspace"', text)
        from dispatcher.runner import codex_arguments
        args = codex_arguments(workspace, 'schema', 'output', 'low', 'model', True)
        self.assertEqual(args[args.index('--sandbox')+1], 'read-only')


if __name__ == '__main__': unittest.main()
