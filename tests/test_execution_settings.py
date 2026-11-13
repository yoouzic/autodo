import importlib
import json
import tempfile
import unittest
from pathlib import Path


class ExecutionSettingsTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('dispatcher.execution_settings'),
                             'Execution settings resolver is not implemented')
        self.settings = importlib.import_module('dispatcher.execution_settings')

    def test_defaults_then_session_override_and_difficulty(self):
        resolve = self.settings.resolve_execution_settings
        value = resolve('codex', {'model': 'gpt-6.1-sol'},
                        {'model': 'gpt-6-astra', 'effort': 'high'}, {'effort': 'low'}, 'hard')
        self.assertEqual((value['model'], value['effort']), ('gpt-6-astra', 'low'))
        self.assertEqual(value['sources'], {'model': 'agent', 'effort': 'session'})
        self.assertEqual(resolve('codex', {'model': 'gpt-6.1-sol'}, {}, {}, 'hard')['effort'], 'high')

    def test_unknown_fields_and_argument_injection_rejected(self):
        for payload in ({'args': ['--unsafe']}, {'model': '--model'}, {'model': 'x\n--flag'},
                        {'effort': True}, {'model': ''}, {'effort': 'bogus'}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.settings.validate_execution_settings('codex', payload)

    def test_dsh_has_no_medium_effort(self):
        with self.assertRaises(ValueError):
            self.settings.validate_execution_settings('dsh', {'effort': 'medium'})
        self.assertEqual(self.settings.validate_execution_settings('dsh', {'effort': 'max'}), {'effort': 'max'})

    def test_dsh_model_settings_require_verified_provider(self):
        value=self.settings.resolve_execution_settings('dsh',{}, {},
            {'model':'deepseek-flash','effort':'low'},'easy')
        self.assertEqual(value['provider'],'deepseek-official')
        with self.assertRaises(ValueError):
            self.settings.resolve_execution_settings('dsh',{}, {}, {'effort':'low'},'easy')

    def test_dsh_installed_catalog_includes_pro_with_same_provider(self):
        value=self.settings.resolve_execution_settings('dsh',{}, {},
            {'model':'deepseek-v4-pro','effort':'max'},'hard')
        self.assertEqual(value['provider'],'deepseek-official')

    def test_antigravity_inherited_variant_can_change_effort(self):
        config = {'executor_commands': {'antigravity': ['agy', '--model', 'gemini-3.8-flash-medium']}}
        value = self.settings.resolve_execution_settings('antigravity', config, {}, {'effort': 'high'}, 'easy')
        self.assertEqual((value['model'], value['effort'], value['cli_model']),
                         ('gemini-3.8-flash', 'high', 'gemini-3.8-flash-high'))

    def test_antigravity_explicit_conflict_and_fixed_effort_rejected(self):
        for settings in ({'model': 'gemini-3.8-flash-medium', 'effort': 'high'},
                         {'model': 'claude-sonnet-4-6', 'effort': 'high'}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.settings.resolve_execution_settings('antigravity', {}, {}, settings, 'medium')

    def test_changing_model_does_not_inherit_incompatible_agent_effort(self):
        value = self.settings.resolve_execution_settings('antigravity', {},
                    {'model': 'gemini-3.8-flash', 'effort': 'medium'},
                    {'model': 'claude-sonnet-4-6'}, 'hard')
        self.assertIsNone(value['effort'])

    def test_model_only_override_keeps_compatible_default_effort(self):
        value = self.settings.resolve_execution_settings('codex', {},
                    {'model': 'gpt-6.1-sol', 'effort': 'high'},
                    {'model': 'gpt-6-astra'}, 'easy')
        self.assertEqual(value['effort'], 'high')
        self.assertEqual(value['sources']['effort'], 'agent')

    def test_qoder_known_model_specific_levels(self):
        value = self.settings.resolve_execution_settings('qoder', {}, {},
                    {'model': 'GLM-5.3', 'effort': 'max'}, 'easy')
        self.assertEqual(value['effort'], 'max')
        with self.assertRaises(ValueError):
            self.settings.resolve_execution_settings('qoder', {}, {},
                    {'model': 'GLM-5.3', 'effort': 'medium'}, 'easy')

    def test_clear_defaults_preserves_vendor_default_without_fake_model(self):
        value = self.settings.resolve_execution_settings('qoder', {}, {}, {}, 'hard')
        self.assertIsNone(value['model'])
        self.assertIsNone(value['effort'])

    def test_settings_are_not_shared_mutable_state(self):
        defaults = {'model': 'gpt-6.1-sol', 'effort': 'high'}
        value = self.settings.resolve_execution_settings('codex', {}, defaults, {}, 'easy')
        defaults['model'] = 'gpt-6-astra'
        self.assertEqual(value['model'], 'gpt-6.1-sol')

    def test_zcode_catalog_allows_only_advertised_effort_and_public_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'data').mkdir()
            (root/'data/zcode-model-catalog.json').write_text(json.dumps({'models':[
                {'id':'local/glm-5.3-flash','model':'glm-5.3-flash','label':'GLM Flash',
                 'efforts':['low','high','max'],'available':True,'secret':'must not project'}]}))
            catalogue=self.settings.load_zcode_catalog(root)
            self.assertEqual(catalogue,[{'id':'glm-5.3-flash','label':'GLM Flash','efforts':['low','high','max']}])
            config={'_zcode_model_catalog':catalogue}
            self.assertEqual(self.settings.resolve_execution_settings('zcode',config,{},
                {'model':'glm-5.3-flash','effort':'low'},'hard')['effort'],'low')
            with self.assertRaises(ValueError):
                self.settings.resolve_execution_settings('zcode',config,{},
                    {'model':'glm-5.3-flash','effort':'medium'},'hard')


if __name__ == '__main__':
    unittest.main()
