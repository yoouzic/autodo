import json
import tempfile
import unittest
from pathlib import Path

from dispatcher.executors import discover_executors
from dispatcher.execution_settings import agent_capabilities, resolve_execution_settings, strip_model_options
from dispatcher.__main__ import load_config
from dispatcher.routing import task_available_executors


class DoubaoRegistryTests(unittest.TestCase):
    def test_only_verified_cli_layout_detected_and_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertFalse(discover_executors(root, {})['doubao']['detected'])
            cli = root/'.tools/providers/doubao/2.31.3/doubao-cli-2.31.3-win-x64/doubao.exe'
            cli.parent.mkdir(parents=True)
            cli.write_bytes(b'not executed')
            spec = discover_executors(root, {})['doubao']
            self.assertTrue(spec['detected'])
            self.assertFalse(spec['enabled'])
            self.assertEqual(spec['command'], [str(cli)])

    def test_model_only_allowed_and_effort_needs_actual_catalogue(self):
        self.assertEqual(agent_capabilities('doubao')['models'], [])
        result = resolve_execution_settings('doubao', {}, {}, {'model': 'provider-model'}, 'hard')
        self.assertEqual(result['cli_model'], 'provider-model')
        self.assertIsNone(result['effort'])
        with self.assertRaises(ValueError):
            resolve_execution_settings('doubao', {}, {}, {'model': 'provider-model', 'effort': 'max'}, 'hard')

    def test_catalogue_metadata_and_session_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'data').mkdir()
            path = root/'data/doubao-model-catalog.json'
            path.write_text(json.dumps({'models': [{'id': 'verified-model', 'label': 'Verified', 'efforts': ['high', 'max']}]}))
            config = load_config(root)
            result = resolve_execution_settings('doubao', config, {'model': 'verified-model', 'effort': 'high'}, {'effort': 'max'}, 'easy')
            self.assertEqual(result['effort'], 'max')
            self.assertEqual(result['sources']['effort'], 'session')
            path.write_text(json.dumps({'models': [{'id': '--inject', 'label': 'bad', 'efforts': ['max']}]}))
            with self.assertRaises(ValueError): load_config(root)

    def test_code_work_requires_independent_repository_checks(self):
        row = {'kind': 'code', 'repo': 'repo-path'}
        registry = {'doubao': {'enabled': True}}
        self.assertFalse(task_available_executors(row, {}, registry)['doubao'])
        config = {'repositories': {'project': 'repo-path'}, 'repository_checks': {'project': {'commands': [['check']]}}}
        self.assertTrue(task_available_executors(row, config, registry)['doubao'])

    def test_model_flags_are_stripped_and_preserve_other_argv(self):
        self.assertEqual(strip_model_options(['doubao.exe', '--model=abc', '--effort', 'high'], 'doubao'), ['doubao.exe'])


if __name__ == '__main__': unittest.main()
