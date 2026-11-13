import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class RepositoryCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'source'
        self.repo.mkdir()
        self.run_dir = self.root / 'run'
        self.workspace = self.run_dir / 'workspace'
        self.workspace.mkdir(parents=True)
        self.row = {'repo': str(self.repo), 'payload': {'commands': [['evil.exe']]}}
        self.commands = [['python', '-B', '-m', 'unittest'], ['tool', 'literal;$(evil)']]
        self.config = {
            'repositories': {'example': str(self.repo)},
            'repository_checks': {'example': {'commands': self.commands, 'report': 'reports/REPORT.md'}},
            'windows_sandbox': 'unelevated',
            'task_timeout_seconds': 1800,
        }
        try:
            self.checks = importlib.import_module('dispatcher.checks')
        except ModuleNotFoundError:
            self.fail('Repository checks module has not been implemented')

    def test_no_configuration_or_unmatched_repository_does_nothing(self):
        for config, row in [({}, self.row), (self.config, {'repo': None}),
                            (self.config, {'repo': str(self.root / 'another')})]:
            with self.subTest(config=config, row=row), patch('dispatcher.adapters.run_process') as run:
                self.assertIsNone(self.checks.run_repository_checks(row, self.run_dir, config, ['codex']))
                run.assert_not_called()
        self.assertFalse((self.workspace / 'reports').exists())

    def test_static_argv_are_sandboxed_in_same_workspace_with_bounded_timeout(self):
        with patch('dispatcher.executors.sandbox_command', side_effect=lambda codex, argv, workspace, mode: codex + ['--'] + argv) as sandbox:
            with patch('dispatcher.adapters.run_process', side_effect=[('first output\n', ''), ('第二项通过\n', 'warning\n')]) as run:
                result = self.checks.run_repository_checks(self.row, self.run_dir, self.config, ['codex'])
        self.assertEqual(sandbox.call_args_list[0].args, (['codex'], self.commands[0], self.workspace, 'unelevated'))
        self.assertEqual(sandbox.call_args_list[1].args, (['codex'], self.commands[1], self.workspace, 'unelevated'))
        for number, call in enumerate(run.call_args_list, 1):
            self.assertEqual(call.args, (['codex', '--'] + self.commands[number - 1],))
            self.assertEqual(call.kwargs, {'cwd': self.workspace, 'log_prefix': self.run_dir / f'check-{number:02d}', 'timeout': 600})
        self.assertEqual(result, {'commands': [
            {'argv': self.commands[0], 'stdout': 'first output\n', 'stderr': '', 'exit_code': 0},
            {'argv': self.commands[1], 'stdout': '第二项通过\n', 'stderr': 'warning\n', 'exit_code': 0},
        ], 'report': 'reports/REPORT.md'})
        report = (self.workspace / result['report']).read_text(encoding='utf-8')
        for expected in ['first output', '第二项通过', 'warning', 'exit_code: 0', json.dumps(self.commands[1], ensure_ascii=False)]:
            self.assertIn(expected, report)

    def test_shorter_configured_timeout_is_preserved(self):
        self.config['task_timeout_seconds'] = 20
        with patch('dispatcher.adapters.run_process', return_value=('ok', '')) as run:
            self.checks.run_repository_checks(self.row, self.run_dir, self.config, ['codex'])
        self.assertEqual(run.call_args.kwargs['timeout'], 20)

    def test_failed_check_stops_and_does_not_write_success_report(self):
        with patch('dispatcher.adapters.run_process', side_effect=[('ok', ''), RuntimeError('exited 2: actual failure')]) as run:
            with self.assertRaisesRegex(RuntimeError, 'Repository check 2.*actual failure'):
                self.checks.run_repository_checks(self.row, self.run_dir, self.config, ['codex'])
        self.assertEqual(run.call_count, 2)
        self.assertFalse((self.workspace / 'reports/REPORT.md').exists())

    def test_timeout_does_not_write_success_report(self):
        with patch('dispatcher.adapters.run_process', side_effect=TimeoutError('timeout')):
            with self.assertRaisesRegex(RuntimeError, 'Repository check 1.*timeout'):
                self.checks.run_repository_checks(self.row, self.run_dir, self.config, ['codex'])
        self.assertFalse((self.workspace / 'reports/REPORT.md').exists())

    def test_report_traversal_and_external_absolute_paths_are_rejected_before_running(self):
        for report in ['../outside.md', 'reports/../../outside.md', str(self.root / 'outside.md')]:
            with self.subTest(report=report), patch('dispatcher.adapters.run_process') as run:
                self.config['repository_checks']['example']['report'] = report
                with self.assertRaisesRegex(ValueError, 'report'):
                    self.checks.run_repository_checks(self.row, self.run_dir, self.config, ['codex'])
                run.assert_not_called()

    def test_existing_report_symlink_is_not_overwritten(self):
        target = self.workspace / 'important.md'
        target.write_text('keep me', encoding='utf-8')
        report = self.workspace / 'REPORT.md'
        self.config['repository_checks']['example']['report'] = 'REPORT.md'
        original_is_symlink = Path.is_symlink
        with patch.object(Path, 'is_symlink', autospec=True, side_effect=lambda path: path == report or original_is_symlink(path)), patch('dispatcher.adapters.run_process') as run:
            with self.assertRaisesRegex(ValueError, 'report'):
                self.checks.run_repository_checks(self.row, self.run_dir, self.config, ['codex'])
            run.assert_not_called()
        self.assertEqual(target.read_text(encoding='utf-8'), 'keep me')

    def test_report_path_is_revalidated_after_commands(self):
        destination = self.workspace / 'reports'
        outside = self.root / 'outside'
        outside.mkdir()
        redirected = False
        original_resolve = Path.resolve

        def redirect(*args, **kwargs):
            nonlocal redirected
            redirected = True
            return 'ok', ''

        def resolve(path, *args, **kwargs):
            if redirected and path == destination / 'REPORT.md':
                return outside / 'REPORT.md'
            return original_resolve(path, *args, **kwargs)

        with patch.object(Path, 'resolve', autospec=True, side_effect=resolve), patch('dispatcher.adapters.run_process', side_effect=redirect):
            with self.assertRaisesRegex(ValueError, 'report'):
                self.checks.run_repository_checks(self.row, self.run_dir, self.config, ['codex'])
        self.assertFalse((outside / 'REPORT.md').exists())

    def test_invalid_commands_are_rejected_before_running(self):
        for commands in [[], 'python -m unittest', [[]], [['python', 4]], [['']]]:
            with self.subTest(commands=commands), patch('dispatcher.adapters.run_process') as run:
                self.config['repository_checks']['example']['commands'] = commands
                with self.assertRaisesRegex(ValueError, 'commands'):
                    self.checks.run_repository_checks(self.row, self.run_dir, self.config, ['codex'])
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
