"""Offline evidence tests; never launch a provider or modify historical runs."""
import copy
import hashlib
import importlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dispatcher.executors import normalize_result


SESSION = '22222222-2222-4222-8222-222222222222'
EOF_ERROR = ('API error (attempt 1): request failed: Post '
             '"https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse": EOF')
REPORT = {'status': 'complete', 'summary': 'Document written', 'artifacts': ['result.md'],
          'checks': ['Read back the document'], 'needs_input': ''}


def step(index, kind, state='DONE', **extra):
    return {'event': 'step_update', 'step_update': dict(
        conversation_id=SESSION, step_index=index, step_type=kind, state=state, **extra)}


def fixture():
    return [
        {'event': 'init', 'conversation_id': SESSION,
         'init': {'model': 'gemini-3.8-flash-medium'}},
        step(0, 'user_input'),
        step(1, 'tool', tool_name='write_file',
             tool_info={'name': 'write_file', 'parameters': {'Path': 'result.md'}}),
        step(2, 'error_message'),
        step(3, 'tool', 'ACTIVE', tool_name='read_file',
             tool_info={'name': 'read_file', 'parameters': {'Path': 'result.md'}}),
        step(3, 'tool', tool_name='read_file',
             tool_info={'name': 'read_file', 'parameters': {'Path': 'result.md'}}),
        step(4, 'agent_response'),
        step(5, 'tool', 'ACTIVE', tool_name='finish',
             tool_info={'name': 'finish', 'parameters': copy.deepcopy(REPORT)}),
        step(5, 'finish'),
        {'event': 'result', 'result': {'conversation_id': SESSION, 'status': 'ERROR',
         'finished_reason': 'error', 'error': EOF_ERROR, 'num_turns': 1,
         'structured_output': copy.deepcopy(REPORT), 'response': json.dumps(REPORT)}}]


def stream(events):
    return '\n'.join(json.dumps(event, ensure_ascii=False) for event in events) + '\n'


class AntigravityRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / 'workspace'
        self.workspace.mkdir()
        self.content = b'# Document\nVerified offline content.\n'
        (self.workspace / 'result.md').write_bytes(self.content)
        self.events = fixture()

    def module(self):
        spec = importlib.util.find_spec('dispatcher.antigravity_recovery')
        self.assertIsNotNone(spec, 'the strict EOF recovery classifier is not implemented')
        return importlib.import_module('dispatcher.antigravity_recovery')

    def recover(self, events=None, stdout=None, **options):
        options.setdefault('process_exit_code', 0)
        return self.module().recover_completed_eof(
            stream(self.events if events is None else events) if stdout is None else stdout,
            self.workspace, **options)

    def report(self, **changes):
        report = dict(REPORT, **changes)
        self.events[-1]['result']['structured_output'] = copy.deepcopy(report)
        self.events[-1]['result']['response'] = json.dumps(report)
        self.events[-3]['step_update']['tool_info']['parameters'] = copy.deepcopy(report)

    def test_early_eof_followed_by_completed_finish_recovers_without_vendor_success(self):
        raw = stream(self.events)
        before = sorted(path.relative_to(self.workspace) for path in self.workspace.rglob('*'))
        result = self.recover(stdout=raw)
        self.assertIsNotNone(result)
        self.assertEqual(result['status'], 'recovered')
        self.assertEqual(result['reported_status'], 'complete')
        self.assertEqual(result['vendor_status'], 'ERROR')
        self.assertEqual(result['executor'], 'antigravity')
        self.assertEqual(result['session_id'], SESSION)
        self.assertEqual(result['runtime_warning']['kind'], 'completed_eof')
        self.assertEqual(result['artifacts'], REPORT['artifacts'])
        self.assertEqual(result['recovered_artifact_sha256'], {
            'result.md': hashlib.sha256(self.content).hexdigest()})
        self.assertEqual(result['completion_evidence']['error_step_index'], 2)
        self.assertEqual(result['completion_evidence']['finish_step_index'], 5)
        self.assertEqual(result['completion_evidence']['stream_sha256'],
                         hashlib.sha256(raw.encode('utf-8')).hexdigest())
        self.assertNotIn(EOF_ERROR, json.dumps(result))
        self.assertEqual(before, sorted(path.relative_to(self.workspace)
                                        for path in self.workspace.rglob('*')))
        with self.assertRaisesRegex(ValueError, 'did not report SUCCESS'):
            normalize_result('antigravity', raw)

    def test_finish_done_can_carry_parameters(self):
        self.events[-2] = step(5, 'tool', tool_name='finish',
                              tool_info={'name': 'finish', 'parameters': copy.deepcopy(REPORT)})
        self.assertIsNotNone(self.recover())

    def test_exact_stream_reading_eof_form_is_supported(self):
        self.events[-1]['result']['error'] = 'API error (attempt 1): stream reading error: unexpected EOF'
        self.assertIsNotNone(self.recover())

    def test_ineligible_execution_context_is_rejected(self):
        for options in ({'process_exit_code': 1}, {'process_exit_code': None},
                        {'process_exit_code': False}, {'process_exit_code': 0.0},
                        {'stderr': 'timeout'}, {'stderr': '\n'}, {'kind': 'code'},
                        {'require_human_review': False}, {'require_human_review': 1}):
            with self.subTest(options=options):
                self.assertIsNone(self.recover(**options))

    def test_arbitrary_eof_errors_are_never_recovered(self):
        errors = ['EOF', 'unexpected EOF', 'authentication failed: EOF',
                  'permission denied: EOF', 'quota exceeded: EOF', 'timeout: EOF',
                  EOF_ERROR + '; authentication failed', EOF_ERROR + '\nquota exceeded',
                  EOF_ERROR.replace('daily-cloudcode-pa.googleapis.com', 'evil.example'),
                  EOF_ERROR.replace('https:', 'http:'), EOF_ERROR.replace('attempt 1', 'attempt 2'),
                  EOF_ERROR.replace(': EOF', ': timeout: EOF'),
                  'API error (attempt 1): stream reading error: unexpected EOF; quota exceeded']
        for error in errors:
            with self.subTest(error=error):
                self.events[-1]['result']['error'] = error
                self.assertIsNone(self.recover())

    def test_terminal_envelope_must_remain_exact_error(self):
        for change in ({'status': 'SUCCESS'}, {'status': 'TIMEOUT'},
                       {'finished_reason': 'timeout'}, {'finished_reason': None},
                       {'denied_actions': ['write']}, {'is_error': True}):
            events = fixture()
            events[-1]['result'].update(change)
            with self.subTest(change=change):
                self.assertIsNone(self.recover(events))

    def test_model_and_session_identity_are_strict(self):
        for mutate in (
                lambda e: e[0]['init'].update(model='gemini-3.8-pro'),
                lambda e: e[0]['init'].pop('model'),
                lambda e: e[0].update(conversation_id='not-a-uuid'),
                lambda e: e[-1]['result'].update(conversation_id='11111111-1111-4111-8111-111111111111'),
                lambda e: e[3]['step_update'].update(conversation_id='another-session'),
                lambda e: e[3].update(conversation_id='another-session')):
            events = fixture()
            mutate(events)
            self.assertIsNone(self.recover(events))

    def test_optional_step_session_can_be_absent(self):
        for event in self.events[1:-1]:
            event['step_update'].pop('conversation_id')
        self.assertIsNotNone(self.recover())

    def test_missing_duplicate_out_of_order_or_unknown_envelopes_are_rejected(self):
        candidates = [self.events[1:], self.events[:-1], [self.events[0]] + self.events,
                      self.events + [self.events[-1]], self.events + [step(6, 'agent_response')],
                      self.events[:2] + [{'event': 'background'}] + self.events[2:],
                      self.events[:2] + [None] + self.events[2:]]
        for events in candidates:
            with self.subTest(events=events):
                self.assertIsNone(self.recover(events))

    def test_unfinished_missing_unknown_or_post_finish_step_is_rejected(self):
        candidates = []
        for state in ('ACTIVE', 'ERROR', 'CANCELLED', None):
            events = fixture()
            events[-2]['step_update']['state'] = state
            candidates.append(events)
        candidates.append(self.events[:-3] + self.events[-1:])
        candidates.append(self.events[:-2] + self.events[-1:])
        candidates.append(self.events[:-1] + [step(6, 'agent_response')] + self.events[-1:])
        for events in candidates:
            with self.subTest(events=events):
                self.assertIsNone(self.recover(events))

    def test_all_steps_must_be_done_and_error_must_precede_finish(self):
        for mutate in (
                lambda e: e[2]['step_update'].update(state='ACTIVE'),
                lambda e: e[4]['step_update'].update(state='ERROR'),
                lambda e: e[4]['step_update'].update(step_index=True),
                lambda e: e[4]['step_update'].update(step_index=99),
                lambda e: e.insert(-1, step(6, 'error_message')),
                lambda e: e.pop(3),
                lambda e: e[3]['step_update'].update(message='permission denied')):
            events = fixture()
            mutate(events)
            self.assertIsNone(self.recover(events))

    def test_error_message_rejects_unknown_metadata_at_both_levels(self):
        for level in ('event', 'detail'):
            for key, value in (('message', 'permission denied'),
                               ('content', 'another provider error'),
                               ('details', {'reason': 'authentication failed'}),
                               ('text', ''), ('unknown_error_field', None),
                               ('duration_seconds', {'message': 'permission denied'})):
                events = fixture()
                error = events[3] if level == 'event' else events[3]['step_update']
                error[key] = value
                with self.subTest(level=level, key=key):
                    self.assertIsNone(self.recover(events))

    def test_exactly_one_user_input_step_must_be_at_index_zero(self):
        for scenario in ('missing', 'second', 'moved'):
            events = fixture()
            if scenario in ('missing', 'moved'):
                events[1]['step_update']['step_type'] = 'agent_response'
            if scenario in ('second', 'moved'):
                events[6]['step_update']['step_type'] = 'user_input'
            with self.subTest(scenario=scenario):
                self.assertIsNone(self.recover(events))

    def test_only_file_tools_and_finish_are_permitted(self):
        for name in ('run_command', 'shell', 'search_web', 'call_mcp_tool',
                     'invoke_subagent', 'schedule', 'wait', 'background', 'unknown'):
            events = fixture()
            events[2]['step_update'].update(tool_name=name, tool_info={'name': name})
            with self.subTest(name=name):
                self.assertIsNone(self.recover(events))

    def test_errors_denials_and_tool_name_mismatch_are_rejected(self):
        for change in ({'error': 'EOF'}, {'denied_actions': ['write']}, {'is_error': True},
                       {'status': 'ERROR'}, {'name': 'run_command'},
                       {'output': {'error': 'permission denied'}}):
            events = fixture()
            events[2]['step_update']['tool_info'].update(change)
            with self.subTest(change=change):
                self.assertIsNone(self.recover(events))

    def test_finish_parameters_must_match_canonical_exactly(self):
        for parameters in (None, {}, json.dumps(REPORT), dict(REPORT, summary='different')):
            events = fixture()
            events[-3]['step_update']['tool_info']['parameters'] = parameters
            with self.subTest(parameters=parameters):
                self.assertIsNone(self.recover(events))

    def test_canonical_must_be_complete_with_checks_and_no_input(self):
        for changes in ({'status': 'failed'}, {'status': 'needs_input'}, {'needs_input': 'answer'},
                        {'checks': []}, {'checks': ['']}, {'artifacts': []},
                        {'summary': 4}, {'secret': 'DO_NOT_COPY'}):
            self.events = fixture()
            self.report(**changes)
            with self.subTest(changes=changes):
                self.assertIsNone(self.recover())
        self.events = fixture()
        self.events[-1]['result'].pop('structured_output')
        self.assertIsNone(self.recover())

    def test_raw_vendor_fields_are_not_copied(self):
        self.events[-1]['result']['credentials'] = 'SECRET_CREDENTIALS_DO_NOT_COPY'
        self.events[0]['init']['message'] = 'SECRET_INIT_DO_NOT_COPY'
        result = self.recover()
        self.assertIsNotNone(result)
        self.assertNotIn('SECRET_', json.dumps(result))

    def test_response_must_be_strict_json_equal_to_canonical_when_present(self):
        for response in ('SECRET_RESPONSE_DO_NOT_COPY', '', json.dumps(dict(REPORT, summary='other')),
                         '```json\n' + json.dumps(REPORT) + '\n```',
                         json.dumps(REPORT).replace('"status": "complete"',
                                                   '"status": "complete", "status": "complete"')):
            self.events[-1]['result']['response'] = response
            with self.subTest(response=response):
                self.assertIsNone(self.recover())
        self.events[-1]['result'].pop('response')
        self.assertIsNotNone(self.recover())

    def test_single_turn_and_contiguous_step_indices_are_required(self):
        for turns in (None, 0, 2, True, 1.0):
            self.events[-1]['result']['num_turns'] = turns
            self.assertIsNone(self.recover())
        self.events = fixture()
        self.events[1]['step_update']['step_index'] = 1
        self.assertIsNone(self.recover())
        self.events = fixture()
        self.events.pop(2)
        self.assertIsNone(self.recover())

    def test_file_tool_paths_and_snapshot_parameters_are_verified(self):
        for name in ('../outside.md', '.runtime/result.md', 'C:result.md', 'result.md:secret',
                     '\\\\?\\C:\\result.md', 'NUL', 'CON.md', 'LPT1.txt', '/outside.md'):
            events = fixture()
            events[2]['step_update']['tool_info']['parameters']['Path'] = name
            with self.subTest(path=name):
                self.assertIsNone(self.recover(events))
        self.events[5]['step_update']['tool_info']['parameters']['StartLine'] = 2
        self.assertIsNone(self.recover())
        self.events = fixture()
        self.events[2]['step_update']['tool_info'].pop('parameters')
        self.assertIsNone(self.recover())

    def test_absolute_workspace_tool_path_is_allowed(self):
        self.events[2]['step_update']['tool_info']['parameters']['Path'] = str(self.workspace / 'result.md')
        self.assertIsNotNone(self.recover())

    def test_artifact_paths_must_be_canonical_relative_safe_paths(self):
        for name in ('../outside.md', '/absolute.md', 'C:/absolute.md', 'C:result.md',
                     '\\server\\share\\file.md', 'nested\\result.md', './result.md',
                     'nested/../result.md', '.runtime/log', '.git/config', 'nested/.GIT/file',
                     'result.md:secret', 'result.md ', 'result.md.', 'result.md\x00',
                     'nested//result.md'):
            self.report(artifacts=[name])
            with self.subTest(name=name):
                self.assertIsNone(self.recover())

    def test_missing_empty_directory_and_duplicate_artifacts_are_rejected(self):
        (self.workspace / 'empty.md').touch()
        (self.workspace / 'directory').mkdir()
        for names in (['missing.md'], ['empty.md'], ['directory'], ['result.md', 'result.md']):
            self.report(artifacts=names)
            with self.subTest(names=names):
                self.assertIsNone(self.recover())

    def test_symbolic_link_artifact_is_rejected(self):
        target = Path(self.temp.name) / 'outside.md'
        target.write_text('outside', encoding='utf-8')
        link = self.workspace / 'linked.md'
        try:
            link.symlink_to(target)
        except OSError as exc:
            self.skipTest(f'symlink privilege unavailable: {exc}')
        self.report(artifacts=['linked.md'])
        self.assertIsNone(self.recover())

    def test_reparse_point_artifact_is_rejected(self):
        # Windows junctions share FILE_ATTRIBUTE_REPARSE_POINT with symlinks.
        original = Path.lstat
        def lstat(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if path.name == 'result.md':
                class ReparseStat:
                    st_file_attributes = 0x400
                    def __getattr__(self, key):
                        return getattr(value, key)
                return ReparseStat()
            return value
        with patch.object(Path, 'lstat', lstat):
            self.assertIsNone(self.recover())

    @unittest.skipUnless(os.name == 'nt', 'Windows junction semantics')
    def test_real_windows_junction_cannot_supply_artifacts_or_tool_paths(self):
        import _winapi
        target = Path(self.temp.name) / 'outside'
        target.mkdir()
        (target / 'outside.md').write_text('outside', encoding='utf-8')
        junction = self.workspace / 'alias'
        _winapi.CreateJunction(str(target), str(junction))
        self.assertTrue(junction.is_junction())
        # Remove only this verified junction itself, never recursively its target.
        self.addCleanup(junction.rmdir)
        self.report(artifacts=['alias/outside.md'])
        self.assertIsNone(self.recover())
        self.report()
        self.events[2]['step_update']['tool_info']['parameters']['Path'] = str(junction / 'outside.md')
        self.assertIsNone(self.recover())
        self.assertEqual((target / 'outside.md').read_text(encoding='utf-8'), 'outside')

    def test_mutation_during_artifact_read_is_rejected(self):
        original = os.fstat
        calls = 0
        def fstat(descriptor):
            nonlocal calls
            info = original(descriptor)
            calls += 1
            if calls > 1:
                class MutatedStat:
                    st_mtime_ns = info.st_mtime_ns + 1
                    def __getattr__(self, key):
                        return getattr(info, key)
                return MutatedStat()
            return info
        with patch.object(os, 'fstat', fstat):
            self.assertIsNone(self.recover())

    def test_non_json_types_and_excessive_nesting_fail_closed(self):
        for raw in (None, 123, [], {}, '\ud800', '[' * 2000 + ']' * 2000):
            if raw is None:
                self.assertIsNone(self.module().recover_completed_eof(raw, self.workspace, process_exit_code=0))
            else:
                self.assertIsNone(self.recover(stdout=raw))

    def test_utf8_byte_budget_is_enforced_before_json_decoding(self):
        with patch.object(self.module(), 'MAX_STREAM_BYTES', 100):
            self.assertIsNone(self.recover(stdout='中' * 50))

    def test_stream_must_be_bounded_valid_unambiguous_jsonl(self):
        valid = stream(self.events)
        invalid = ['', valid + '{', valid + '\n', valid.replace('"event": "init"',
                   '"event": "init", "event": "init"', 1),
                   valid.replace('"model": "gemini-3.8-flash-medium"', '"model": NaN', 1),
                   ' ' * (4 * 1024 * 1024 + 1), '[[]]', '\ufeff' + valid]
        for raw in invalid:
            with self.subTest(length=len(raw)):
                self.assertIsNone(self.recover(stdout=raw))

    def test_artifact_count_file_and_total_byte_limits_are_enforced(self):
        module = self.module()
        with patch.object(module, 'MAX_ARTIFACT_BYTES', len(self.content) - 1):
            self.assertIsNone(self.recover())
        with patch.object(module, 'MAX_TOTAL_ARTIFACT_BYTES', len(self.content) - 1):
            self.assertIsNone(self.recover())
        with patch.object(module, 'MAX_ARTIFACTS', 0):
            self.assertIsNone(self.recover())


if __name__ == '__main__':
    unittest.main()
