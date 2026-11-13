import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from dispatcher.adapters import Dida, parse_json_output
from dispatcher.runner import artifact_paths, codex_arguments


class AdapterTests(unittest.TestCase):
    def test_complete_uses_non_json_command(self):
        with patch('dispatcher.adapters.run_process', return_value=('任务已完成', '')) as process:
            Dida(['node', 'dida.js']).complete({'projectId':'p','id':'t'})
        self.assertEqual(process.call_args.args[0], ['node','dida.js','task','complete','p','t'])

    def test_json_and_ndjson(self):
        self.assertEqual(parse_json_output('[{"id":"a"}]'), [{"id": "a"}])
        self.assertEqual(parse_json_output('{"id":"a"}\n{"id":"b"}\n'), [{"id": "a"}, {"id": "b"}])
        with self.assertRaises(ValueError):
            parse_json_output('authentication failed')

    def test_sync_merges_project_and_filter_inbox(self):
        calls = []
        def command(args):
            calls.append(args)
            if args[:2] == ['project', 'list']:
                return [{'id': 'p', 'name': 'work', 'kind': 'TASK'}]
            if args[:2] == ['project', 'data']:
                return {'tasks': [{'id': 'a', 'projectId': 'p', 'title': 'A', 'status': 0}]}
            if args[:2] == ['task', 'filter']:
                return [{'id': 'b', 'projectId': 'inbox', 'title': 'B', 'status': 0}]
            raise AssertionError(args)
        d = Dida([], command=command)
        tasks, warnings = d.fetch_tasks()
        self.assertEqual({t['id'] for t in tasks}, {'a', 'b'})
        self.assertFalse(warnings)
        self.assertIn(['task', 'filter', '--status', '0'], calls)

    def test_project_failure_is_not_empty_success(self):
        def command(args):
            if args[:2] == ['project', 'list']:
                raise RuntimeError('auth required')
            return []
        with self.assertRaisesRegex(RuntimeError, 'auth'):
            Dida([], command=command).fetch_tasks()

    def test_artifact_must_be_real_nonempty_file_in_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root/'result.md').write_text('result', encoding='utf-8')
            self.assertEqual(artifact_paths(root, ['result.md']), [root/'result.md'])
            for value in ('../secret.txt', 'missing.txt', '.'):
                with self.assertRaises(ValueError):
                    artifact_paths(root, [value])
            with self.assertRaises(ValueError):
                artifact_paths(root, [])

    def test_codex_args_use_stdin_and_sandbox(self):
        args = codex_arguments(Path('work'), Path('schema.json'), Path('result.json'), 'high', 'my-model', False)
        self.assertIn('workspace-write', args)
        self.assertIn('--ignore-user-config', args)
        self.assertIn('windows.sandbox="elevated"', args)
        self.assertEqual(args[-1], '-')
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', args)
        self.assertIn('model_reasoning_effort="high"', args)
        readonly = codex_arguments(Path('work'), Path('s'), Path('r'), 'medium', None, True)
        self.assertIn('read-only', readonly)


if __name__ == '__main__':
    unittest.main()
