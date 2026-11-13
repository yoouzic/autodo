"""WorkBuddy argv and protocol tests never start a CLI or access real login data."""

import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPORT = {'status': 'complete', 'summary': 'done', 'artifacts': ['result.md'],
          'checks': ['read back result.md'], 'needs_input': ''}
SCHEMA = {'type': 'object', 'additionalProperties': False,
          'properties': {key: {'type': 'array' if key in {'artifacts', 'checks'} else 'string'}
                         for key in REPORT}, 'required': list(REPORT)}


class WorkBuddyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.workspace = self.base / 'native-project'
        self.workspace.mkdir()
        self.run = self.base / 'run'
        self.run.mkdir()
        self.runtime = self.run / 'runtime'
        self.home = self.base / 'isolated-profile'
        self.home.mkdir()

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('dispatcher.workbuddy'),
                             'The WorkBuddy adapter has not been implemented')
        from dispatcher import workbuddy
        return workbuddy

    def build(self, **changes):
        values = dict(command=['node.exe', 'codebuddy'], workspace=self.workspace,
                      run_dir=self.run, prompt='Only work on the delegated document.',
                      settings={'cli_model': 'glm-5.1', 'effort': 'low'}, runtime=self.runtime,
                      schema=SCHEMA, home=self.home)
        values.update(changes)
        return self.module().build_invocation(**values)

    def terminal(self, **changes):
        return dict({'type': 'result', 'subtype': 'success', 'is_error': False,
                     'permission_denials': [], 'session_id': 'wb-session-1',
                     'structured_output': copy.deepcopy(REPORT)}, **changes)

    def normalize(self, value, **kwargs):
        stdout = json.dumps(value) if isinstance(value, dict) else value
        return self.module().normalize_result(stdout, **kwargs)

    def test_invocation_has_fixed_tools_permissions_and_only_runtime_write_roots(self):
        module = self.module()
        with patch('pathlib.Path.open', side_effect=AssertionError('No credential/config bodies may be read')):
            argv, env, stdin, roots = self.build()
        self.assertEqual(argv[:2], ['node.exe', 'codebuddy'])
        self.assertEqual(argv[argv.index('--tools') + 1], 'Read,Write,Edit,Glob,Grep')
        self.assertEqual(argv[argv.index('--allowedTools') + 1], 'Read,Write,Edit,Glob,Grep')
        self.assertEqual(argv[argv.index('--permission-mode') + 1], 'dontAsk')
        self.assertIn('--strict-mcp-config', argv)
        self.assertEqual(json.loads(argv[argv.index('--mcp-config') + 1]), {'mcpServers': {}})
        self.assertEqual(argv[argv.index('--setting-sources') + 1], 'none')
        settings = json.loads(argv[argv.index('--settings') + 1])
        self.assertIs(settings['disableAllHooks'], True)
        self.assertIs(settings['autoUpdates'], False)
        self.assertIn('--no-session-persistence', argv)
        self.assertLessEqual(int(argv[argv.index('--max-turns') + 1]), 30)
        self.assertEqual(json.loads(argv[argv.index('--json-schema') + 1]), SCHEMA)
        self.assertEqual(argv[argv.index('--output-format') + 1], 'stream-json')
        self.assertEqual(env['CODEBUDDY_CONFIG_DIR'], str(self.home / '.workbuddy'))
        self.assertEqual(env['CODEBUDDY_PROJECT_CONFIG_DIR'], str(self.runtime / '.codebuddy'))
        self.assertEqual(env['CODEBUDDY_DISABLE_CRON'], '1')
        self.assertEqual(env['CODEBUDDY_CODE_DISABLE_BACKGROUND_TASKS'], '1')
        self.assertEqual(roots, [self.runtime])
        self.assertEqual(stdin, 'Only work on the delegated document.')
        self.assertNotIn(stdin, argv)
        self.assertFalse((self.home / '.workbuddy').exists())
        self.assertFalse(any(flag in argv for flag in ('-y', '--dangerously-skip-permissions',
            '--serve', '--host', '--background', '--bg', '--add-dir', '--plugin-dir', '--swarm')))
        self.assertNotIn('CODEBUDDY_IS_SANDBOX', env)
        self.assertNotIn(str(self.home / '.workbuddy'), list(map(str, roots)))

    def test_default_runtime_is_allowed_only_for_the_managed_live_workspace(self):
        workspace = self.run / 'workspace'
        workspace.mkdir()
        runtime = workspace / '.runtime'
        _, env, _, roots = self.build(workspace=workspace, runtime=runtime)
        self.assertEqual(roots, [runtime])
        self.assertEqual(env['CODEBUDDY_PROJECT_CONFIG_DIR'], str(runtime / '.codebuddy'))
        with self.assertRaises(ValueError):
            self.build(runtime=self.workspace / '.runtime')

    def test_builder_preserves_arguments_and_refuses_unsafe_base_flags_or_bad_limits(self):
        command = ['node.exe', 'D:/Program Files/WorkBuddy/bin/codebuddy']
        settings = {'model': 'auto', 'effort': 'high'}
        before = copy.deepcopy((command, settings, SCHEMA))
        argv, _, _, _ = self.build(command=command, settings=settings, max_turns=30)
        self.assertEqual(argv[argv.index('--model') + 1], 'auto')
        self.assertEqual(argv[argv.index('--effort') + 1], 'high')
        self.assertEqual((command, settings, SCHEMA), before)
        for command in ([], ['node.exe', 'codebuddy', '-y'], ['codebuddy', '--serve'], 'codebuddy'):
            with self.subTest(command=command), self.assertRaises(ValueError):
                self.build(command=command)
        for limit in (0, 31, True, '3'):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.build(max_turns=limit)
        for settings in ({'model': '--serve'}, {'effort': 'ultra'}, {'effort': []}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.build(settings=settings)

    def test_unique_success_uses_structured_business_report_without_promising_resume(self):
        value = self.terminal(result=json.dumps(dict(REPORT, status='failed')))
        result = self.normalize(value)
        self.assertEqual(result, dict(REPORT, executor='workbuddy', session_id='wb-session-1',
                                      resume_available=False))
        self.assertEqual(value['structured_output'], REPORT)

    def test_ndjson_stream_requires_one_terminal_as_the_last_event(self):
        init = {'type': 'system', 'subtype': 'init', 'session_id': 'wb-session-1'}
        assistant = {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'done'}]}}
        terminal = self.terminal()
        good = '\n'.join(json.dumps(item) for item in (init, assistant, terminal))
        self.assertEqual(self.normalize(good)['status'], 'complete')
        invalid = [json.dumps(init), good + '\n' + json.dumps(terminal),
                   good + '\n' + json.dumps(init), good + '\n{"type":',
                   json.dumps([init, terminal]), 'narration\n' + good]
        for value in invalid:
            with self.subTest(value=value[:60]), self.assertRaises(ValueError):
                self.normalize(value)

    def test_failure_denials_and_malformed_success_fields_are_rejected(self):
        for changes in ({'subtype': 'error_during_execution'}, {'is_error': True}, {'is_error': 0},
                        {'is_error': None}, {'permission_denials': [{'tool_name': 'Read'}]},
                        {'permission_denials': None}, {'error': 'PRIVATE_TOKEN'},
                        {'errors': ['PRIVATE_TOKEN']}):
            with self.subTest(changes=changes), self.assertRaises(ValueError) as failure:
                self.normalize(self.terminal(**changes))
            self.assertNotIn('PRIVATE_TOKEN', str(failure.exception))
        for field in ('type', 'subtype', 'is_error'):
            value = self.terminal()
            value.pop(field)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.normalize(value)
        stream = '\n'.join(map(json.dumps, [
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'is_error': True}]}},
            self.terminal()]))
        with self.assertRaises(ValueError):
            self.normalize(stream)
        with self.assertRaises(ValueError):
            self.normalize(self.terminal(), exit_code=7)

    def test_invalid_structured_output_cannot_fall_back_to_legacy_text(self):
        for canonical in ({}, [], False, 1, '', json.dumps(REPORT), dict(REPORT, extra='forbidden')):
            with self.subTest(canonical=canonical), self.assertRaises(ValueError):
                self.normalize(self.terminal(structured_output=canonical, result=json.dumps(REPORT)))
        for changes in ({'artifacts': 'result.md'}, {'checks': [False]}, {'needs_input': None},
                        {'status': 'success'}, {'summary': 7}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.normalize(self.terminal(structured_output=dict(REPORT, **changes)))

    def test_legacy_report_must_be_exact_json_and_business_failure_is_preserved(self):
        value = self.terminal(result=json.dumps(dict(REPORT, status='needs_input', needs_input='missing input')))
        value.pop('structured_output')
        self.assertEqual(self.normalize(value)['status'], 'needs_input')
        value['structured_output'] = None
        self.assertEqual(self.normalize(value)['needs_input'], 'missing input')
        for raw in ('```json\n' + json.dumps(REPORT) + '\n```', 'done ' + json.dumps(REPORT), '{}'):
            value['result'] = raw
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.normalize(value)

    def test_empty_output_only_means_login_when_observed_login_marker_is_present(self):
        module = self.module()
        for stderr in ('Authentication required. Please use /login. PRIVATE_TOKEN',
                       'Please use /login to authenticate. PRIVATE_TOKEN'):
            with self.subTest(stderr=stderr), self.assertRaises(module.WorkBuddyLoginRequired) as failure:
                module.normalize_result('', stderr=stderr)
            self.assertEqual(str(failure.exception), module.LOGIN_REQUIRED_MESSAGE)
            self.assertNotIn('PRIVATE_TOKEN', str(failure.exception))
        for stdout, stderr in (('', ''), ('  \n', 'Unexpected startup problem PRIVATE_TOKEN')):
            with self.subTest(stderr=stderr), self.assertRaises(ValueError) as failure:
                module.normalize_result(stdout, stderr=stderr)
            self.assertNotIsInstance(failure.exception, module.WorkBuddyLoginRequired)
            self.assertNotIn('PRIVATE_TOKEN', str(failure.exception))

    def test_duplicate_json_keys_and_oversized_output_are_rejected(self):
        module = self.module()
        raw = json.dumps(self.terminal()).replace('"is_error": false', '"is_error": true, "is_error": false')
        with self.assertRaises(ValueError):
            module.normalize_result(raw)
        with patch.object(module, 'MAX_OUTPUT_BYTES', 8), self.assertRaises(ValueError):
            module.normalize_result(json.dumps(self.terminal()))


if __name__ == '__main__':
    unittest.main()
