import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dispatcher.adapters import run_process
from dispatcher.external import invocation
from dispatcher.runner import MultiAgentRunner, write_antigravity_diagnostics
from tests import test_antigravity_recovery as recovery_fixtures


SESSION = '11111111-1111-4111-8111-111111111111'
REPORT = {'status': 'complete', 'summary': 'written', 'artifacts': ['result.md'],
          'checks': ['read file'], 'needs_input': ''}


def stream(*events):
    return '\n'.join(json.dumps(event) for event in events) + '\n'


def initial():
    return {'event': 'init', 'conversation_id': SESSION, 'init': {}}


def terminal(status='SUCCESS', report=REPORT):
    return {'event': 'result', 'result': {'status': status,
            'conversation_id': SESSION, 'response': json.dumps(report)}}


class AntigravityRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / 'run'
        (self.run / 'workspace').mkdir(parents=True)
        self.row = {'payload': {'id': 't', 'projectId': 'p', 'title': 'Write a document'},
                    'kind': 'document'}
        self.registry = {name: {'command': [name], 'enabled': True}
                         for name in ('antigravity', 'qoder')}

    def runner(self, timeout=600):
        with patch('dispatcher.runner.discover_executors', return_value=self.registry):
            return MultiAgentRunner(self.root, {'codex_command': ['codex'],
                'task_timeout_seconds': timeout})

    def invoke(self, process, timeout=600, name='antigravity'):
        (self.run / 'routing.json').write_text(json.dumps({'executor': name}), encoding='utf-8')
        with patch('dispatcher.runner.run_process', side_effect=process):
            return self.runner(timeout).execute(self.row, self.run)

    def diagnostics(self):
        path = self.run / 'execution-diagnostics.json'
        self.assertTrue(path.is_file(), 'failed run must retain structured diagnostics')
        return json.loads(path.read_text(encoding='utf-8'))

    def test_provider_log_is_local_and_permissions_remain_restricted(self):
        args, env, stdin, roots = invocation('antigravity', ['agy'], self.run / 'workspace',
                                           self.run, 'document', {'task_timeout_seconds': 600})
        self.assertIn('--log-file', args)
        self.assertEqual(Path(args[args.index('--log-file') + 1]),
                         self.run / 'workspace' / '.runtime' / 'antigravity.log')
        self.assertEqual(args[args.index('--print-timeout') + 1], '600s')
        settings = json.loads(args[args.index('--settings') + 1])
        self.assertFalse(settings['allowNonWorkspaceAccess'])
        self.assertTrue(settings['enableTerminalSandbox'])
        self.assertEqual(settings['toolPermission'], 'proceed-in-sandbox')
        self.assertNotIn('--dangerously-skip-permissions', args)

    def test_success_has_sixty_second_exit_grace_and_keeps_report(self):
        def process(command, **options):
            self.assertEqual(options['timeout'], 660)
            self.assertEqual(command[command.index('--print-timeout') + 1], '600s')
            return stream(initial(), terminal()), ''
        result = self.invoke(process)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['session_id'], SESSION)
        self.assertTrue((self.run / 'execution.json').is_file())

    def test_other_provider_keeps_original_hard_timeout(self):
        def process(command, **options):
            self.assertEqual(options['timeout'], 600)
            return json.dumps({'type': 'result', 'session_id': 'q1',
                               'subtype': 'success', 'result': REPORT}), ''
        self.assertEqual(self.invoke(process, name='qoder')['status'], 'complete')

    def test_completed_eof_is_preserved_as_recovered_not_success(self):
        (self.run/'workspace/result.md').write_text('finished document', encoding='utf-8')
        raw = recovery_fixtures.stream(recovery_fixtures.fixture())
        result = self.invoke(lambda *args, **kwargs: (raw, ''))
        self.assertEqual(result['status'], 'recovered')
        self.assertEqual(result['vendor_status'], 'ERROR')
        self.assertEqual(result['completion_evidence']['process_exit_code'], 0)
        self.assertEqual(json.loads((self.run/'execution.json').read_text(encoding='utf-8'))['status'], 'recovered')
        self.assertEqual(self.diagnostics()['stage'], 'artifact_recovery')

    def test_nonzero_process_never_uses_completed_eof_recovery(self):
        (self.run/'workspace/result.md').write_text('finished document', encoding='utf-8')
        raw = recovery_fixtures.stream(recovery_fixtures.fixture())
        def process(*args, **kwargs):
            (self.run/'execution.events.jsonl').write_text(raw, encoding='utf-8')
            raise RuntimeError('nonzero process')
        with self.assertRaises(RuntimeError):
            self.invoke(process)
        self.assertFalse((self.run/'execution.json').exists())

    def test_hard_timeout_records_only_allowlisted_metadata(self):
        secret = 'Bearer PRIVATE_TOKEN_DO_NOT_PERSIST'
        events = stream(initial(), {'event': 'step_update', 'step_update': {
            'step_type': 'error_message', 'state': 'DONE'}},
            {'event': 'step_update', 'step_update': {'step_type': 'tool',
             'tool_info': {'output': secret}}},
            {'event': 'step_update', 'step_update': {'step_type': 'error_message'}})
        def process(command, **options):
            (self.run / 'execution.events.jsonl').write_text(events, encoding='utf-8')
            (self.run / 'execution.stderr.log').write_text(secret, encoding='utf-8')
            raise TimeoutError('Process exceeded 660s: node.EXE; ' + secret)
        with self.assertRaisesRegex(TimeoutError, 'Antigravity') as raised:
            self.invoke(process)
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn('node.EXE', str(raised.exception))
        data = self.diagnostics()
        self.assertEqual(data['executor'], 'antigravity')
        self.assertEqual(data['stage'], 'hard_timeout')
        self.assertEqual(data['timeout_budgets'], {
            'provider_seconds': 600, 'exit_grace_seconds': 60, 'process_seconds': 660})
        self.assertEqual(data['session_id'], SESSION)
        self.assertEqual(data['event_count'], 4)
        self.assertEqual(data['last_event_type'], 'step_update')
        self.assertEqual(data['last_step_type'], 'error_message')
        self.assertEqual(data['opaque_error_count'], 2)
        self.assertFalse(data['has_terminal'])
        self.assertNotIn(secret, json.dumps(data))
        self.assertFalse((self.run / 'execution.json').exists())

    def test_nonzero_exit_retains_terminal_without_exposing_vendor_text(self):
        def process(command, **options):
            (self.run / 'execution.events.jsonl').write_text(
                stream(initial(), terminal('ERROR')), encoding='utf-8')
            raise RuntimeError('node.EXE exited 2: SECRET_PROVIDER_MESSAGE')
        with self.assertRaisesRegex(RuntimeError, 'Antigravity') as raised:
            self.invoke(process)
        self.assertNotIn('SECRET_PROVIDER_MESSAGE', str(raised.exception))
        data = self.diagnostics()
        self.assertEqual(data['stage'], 'process_exit')
        self.assertTrue(data['has_terminal'])
        self.assertEqual(data['terminal_status'], 'ERROR')
        self.assertFalse((self.run / 'execution.json').exists())

    def test_missing_terminal_is_not_success_even_if_artifact_exists(self):
        (self.run / 'workspace' / 'result.md').write_text('draft', encoding='utf-8')
        def process(command, **options):
            return stream(initial(), {'event': 'step_update', 'step_update': {
                'step_type': 'agent_response', 'state': 'DONE'}}), ''
        with self.assertRaisesRegex(ValueError, 'Antigravity'):
            self.invoke(process)
        self.assertEqual(self.diagnostics()['stage'], 'result_protocol')
        self.assertFalse(self.diagnostics()['has_terminal'])
        self.assertFalse((self.run / 'execution.json').exists())

    def test_malformed_stream_and_untrusted_metadata_are_not_copied(self):
        malicious = 'SECRET_TOKEN_IS_NOT_A_SESSION'
        def process(command, **options):
            return stream({'event': 'init', 'conversation_id': malicious},
                          {'event': malicious, 'step_update': {'step_type': malicious}}) + 'broken {', ''
        with self.assertRaises(ValueError):
            self.invoke(process)
        data = self.diagnostics()
        self.assertIsNone(data['session_id'])
        self.assertEqual(data['last_event_type'], 'unknown')
        self.assertNotIn(malicious, json.dumps(data))
        self.assertFalse(data['has_terminal'])

    def test_no_event_file_still_has_empty_diagnostics(self):
        def process(command, **options):
            raise TimeoutError('node.EXE exceeded timeout')
        with self.assertRaises(TimeoutError):
            self.invoke(process)
        data = self.diagnostics()
        self.assertEqual(data['event_count'], 0)
        self.assertIsNone(data['session_id'])
        self.assertFalse(data['has_terminal'])

    def test_real_process_can_finish_after_provider_deadline_in_grace(self):
        # A local stand-in exercises the real pipe/process collection, not a vendor.
        program = ('import time;time.sleep(0.08);print(' +
                   repr(stream(initial(), terminal('ERROR'))) + ')')
        (self.run / 'routing.json').write_text('{"executor":"antigravity"}', encoding='utf-8')
        with patch('dispatcher.runner.sandbox_command', return_value=[sys.executable, '-c', program]):
            with self.assertRaises(ValueError):
                self.runner(timeout=0.03).execute(self.row, self.run)
        data = self.diagnostics()
        self.assertTrue(data['has_terminal'])
        self.assertEqual(data['stage'], 'result_protocol')
        self.assertEqual(data['timeout_budgets']['process_seconds'], 60.03)
        self.assertFalse((self.run / 'execution.json').exists())

    def test_real_hard_timeout_keeps_partial_events_and_failure(self):
        program = 'import time;print(' + repr(stream(initial())) + ',flush=True);time.sleep(5)'
        def accelerated_process(command, **options):
            self.assertEqual(options['timeout'], 660)
            # Keep the real process-tree timeout path, but compress the test clock.
            options['timeout'] = 0.25
            return run_process(command, **options)
        with patch('dispatcher.runner.sandbox_command', return_value=[sys.executable, '-c', program]):
            with self.assertRaisesRegex(TimeoutError, 'Antigravity'):
                self.invoke(accelerated_process)
        data = self.diagnostics()
        self.assertEqual(data['stage'], 'hard_timeout')
        self.assertEqual(data['session_id'], SESSION)
        self.assertFalse((self.run / 'execution.json').exists())

    def test_terminal_success_followed_by_hard_timeout_remains_failure(self):
        def process(command, **options):
            (self.run / 'execution.events.jsonl').write_text(stream(initial(), terminal()), encoding='utf-8')
            raise TimeoutError('hung during shutdown')
        with self.assertRaises(TimeoutError):
            self.invoke(process)
        self.assertTrue(self.diagnostics()['has_terminal'])
        self.assertFalse((self.run / 'execution.json').exists())

    def test_malformed_values_and_oversize_payload_remain_bounded(self):
        events = stream(initial(), {'event': ['bad'], 'result': []},
                        {'event': 'step_update', 'step_update': {'step_type': {}}})
        events += json.dumps({'event': 'tool', 'output': 'S' * (4 * 1024 * 1024)})
        write_antigravity_diagnostics(self.run, 'result_protocol', 600, events)
        data = self.diagnostics()
        self.assertTrue(data['events_truncated'])
        self.assertEqual(data['event_count'], 3)
        self.assertEqual(data['last_step_type'], 'unknown')
        self.assertLess((self.run / 'execution-diagnostics.json').stat().st_size, 1500)

    def test_diagnostics_write_failure_does_not_replace_timeout(self):
        (self.run / 'execution-diagnostics.json').mkdir()
        def process(command, **options):
            raise TimeoutError('node.EXE exceeded timeout')
        with self.assertRaisesRegex(TimeoutError, 'Antigravity.*could not be saved'):
            self.invoke(process)


if __name__ == '__main__':
    unittest.main()
