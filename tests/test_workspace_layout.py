import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from dispatcher.workspace_layout import task_directory, task_key_hash, task_summary


class WorkspaceLayoutTests(unittest.TestCase):
    def test_summary_preserves_chinese_and_normalizes_width(self):
        self.assertEqual(task_summary({'title': '  借阅提醒 ＡＢＣ 方案  '}), '借阅提醒 ABC 方案')

    def test_summary_uses_first_content_sentence_without_a_title(self):
        for content in ('第一句。第二句', '第一句！第二句', '第一句?第二句', '第一句\n第二句',
                        '第一句．第二句', '第一句｡第二句'):
            with self.subTest(content=content):
                self.assertEqual(task_summary({'title': '  ', 'content': content}), '第一句')
        self.assertEqual(task_summary({'content': 'First sentence. Second sentence.'}), 'First sentence')

    def test_summary_falls_back_when_task_has_no_usable_words(self):
        for task in ({}, {'title': None, 'content': None}, {'title': '<>:"/\\|?*\x00 . '}):
            with self.subTest(task=task):
                self.assertEqual(task_summary(task), '未命名任务')

    def test_summary_removes_unsafe_characters_and_trailing_dots(self):
        summary = task_summary({'title': '  借阅：提醒 / 架构\\方案<>:"|?*\x00\x1f\u202e  评审\t完成...  '})
        self.assertEqual(summary, '借阅 提醒 架构 方案 评审 完成')

    def test_summary_truncates_to_36_characters_then_trims_dots(self):
        self.assertEqual(task_summary({'title': '中' * 40}), '中' * 36)
        self.assertEqual(task_summary({'title': '中' * 34 + '. ' + '余下'}), '中' * 34)

    def test_summary_protects_windows_device_names_including_extensions(self):
        for title in ('CON', 'nul.txt', 'AUX', 'prn', 'COM1', 'LPT9', 'COM¹', 'LPT²',
                      'CONIN$', 'CONOUT$', 'NUL.' + 'x' * 40):
            with self.subTest(title=title):
                summary = task_summary({'title': title})
                self.assertTrue(summary.startswith('_'), summary)
                self.assertLessEqual(len(summary), 36)

    def test_task_directory_uses_stable_task_identity_and_executor(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            task = {'projectId': 'p', 'id': 'one', 'title': '借阅提醒：架构 / 方案'}
            first = task_directory(root, task, 'zcode')
            self.assertEqual(first.parent, root.resolve()/'workspaces'/'zcode')
            self.assertIn('借阅提醒', first.name)
            self.assertTrue(first.name.endswith('--' + hashlib.sha256(b'p:one').hexdigest()[:10]))
            self.assertEqual(task_key_hash(task), hashlib.sha256(b'p:one').hexdigest()[:10])
            self.assertEqual(first, task_directory(root, task.copy(), 'zcode'))
            self.assertNotEqual(first, task_directory(root, dict(task, id='two'), 'zcode'))
            self.assertNotEqual(first, task_directory(root, dict(task, projectId='other'), 'zcode'))
            self.assertNotEqual(first, task_directory(root, dict(task, title='新标题'), 'zcode'))
            self.assertNotEqual(first, task_directory(root, task, 'codex'))
            self.assertFalse((root/'workspaces').exists())

    def test_invalid_executor_is_rejected_without_creating_directories(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            task = {'projectId': 'p', 'id': 'one', 'title': '任务'}
            for executor in ('unknown', '../outside', 'codex/../zcode', '', 'CODEX'):
                with self.subTest(executor=executor), self.assertRaises(ValueError):
                    task_directory(root, task, executor)
            self.assertFalse((root/'workspaces').exists())

    def test_task_identity_must_have_nonempty_project_and_task_ids(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for task in ({}, {'projectId': 'p'}, {'id': 'one'},
                         {'projectId': '', 'id': 'one'}, {'projectId': 'p', 'id': '  '},
                         {'projectId': None, 'id': 'one'}, {'projectId': 'p', 'id': 0}):
                with self.subTest(task=task):
                    try:
                        task_directory(root, task, 'codex')
                    except (KeyError, ValueError) as exc:
                        self.assertIsInstance(exc, ValueError)
                    else:
                        self.fail('Missing task identity must be rejected explicitly')
            self.assertFalse((root/'workspaces').exists())

    def test_symlink_escapes_are_rejected_at_each_layout_level(self):
        self._check_link_escapes(junction=False)

    @unittest.skipUnless(os.name == 'nt', 'Windows junction behavior')
    def test_windows_junction_escapes_are_rejected_at_each_layout_level(self):
        self._check_link_escapes(junction=True)

    def _check_link_escapes(self, *, junction):
        task = {'projectId': 'p', 'id': 'one', 'title': '任务'}
        for level in ('workspaces', 'executor', 'task', 'runs'):
            with self.subTest(level=level), tempfile.TemporaryDirectory() as d:
                root = Path(d)/'project'
                root.mkdir()
                outside = Path(d)/'outside'
                outside.mkdir()
                target_task = task_directory(root, task, 'zcode')
                links = {'workspaces': root/'workspaces', 'executor': target_task.parent,
                         'task': target_task, 'runs': target_task/'runs'}
                link = links[level]
                link.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if junction:
                        import _winapi
                        _winapi.CreateJunction(str(outside), str(link))
                    else:
                        link.symlink_to(outside, target_is_directory=True)
                except OSError as exc:
                    self.skipTest(f'Directory links unavailable: {exc}')
                try:
                    with self.assertRaises(ValueError):
                        task_directory(root, task, 'zcode')
                    self.assertEqual(list(outside.iterdir()), [])
                finally:
                    link.rmdir() if junction else link.unlink()


if __name__ == '__main__':
    unittest.main()
