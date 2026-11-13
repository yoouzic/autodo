"""Bounded workspace copies use only temporary fixtures, never real agent data."""
import hashlib
import importlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class WorkspaceCopyTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('dispatcher.workspace_copy'),
                             'A bounded workspace copier is required')
        self.copy = importlib.import_module('dispatcher.workspace_copy')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.destination = self.root / 'destination'
        self.source.mkdir()

    def put(self, relative, value=b'ordinary content'):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        return path

    def test_regular_files_and_empty_directories_copy_with_deterministic_manifest(self):
        self.put('z.txt', b'z')
        self.put('nested/notes.txt', b'hello')
        (self.source / 'empty').mkdir()
        self.destination.mkdir()
        receipt = self.copy.copy_workspace(self.source, self.destination)
        manifest = [{'path': 'nested/notes.txt', 'sha256': hashlib.sha256(b'hello').hexdigest(), 'bytes': 5},
                    {'path': 'z.txt', 'sha256': hashlib.sha256(b'z').hexdigest(), 'bytes': 1}]
        encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        self.assertEqual(receipt, {'file_count': 2, 'total_bytes': 6,
                                  'manifest_sha256': hashlib.sha256(encoded).hexdigest(),
                                  'excluded_count': 0, 'manifest': manifest})
        self.assertTrue((self.destination / 'empty').is_dir())
        self.assertEqual((self.destination / 'nested/notes.txt').read_bytes(), b'hello')
        self.assertFalse(os.path.samefile(self.source / 'z.txt', self.destination / 'z.txt'))
        (self.destination / 'z.txt').write_bytes(b'changed destination')
        self.assertEqual((self.source / 'z.txt').read_bytes(), b'z')

    def test_filters_do_not_open_sensitive_files_or_descend_into_excluded_directories(self):
        included = self.put('src/auth.py', b'code')
        self.put('README.md', b'docs')
        excluded = ['.env', '.env.example', '.npmrc', '.netrc', 'auth.json',
                    'credentials.yaml', 'provider_config.json', 'client_secret.json',
                    'server.key', 'certificate.pfx', '.git/config', '.runtime/session.json',
                    '.codex/auth.json', '.dsh/credentials.json', '.qoder/token.json',
                    'node_modules/module/index.js', '.venv/site.py', '.cache/output.txt',
                    '__pycache__/file.pyc', 'keys/key.txt', '.dispatcher-doubao-run.json',
                    'nested/.DISPATCHER-DOUBAO-previous.json']
        for name in excluded:
            self.put(name, b'PRIVATE_CONTENT_MUST_NOT_BE_READ')
        opened = []
        real_open = self.copy.os.open

        def track_open(path, *args, **kwargs):
            opened.append(Path(path))
            return real_open(path, *args, **kwargs)

        with patch.object(self.copy.os, 'open', side_effect=track_open):
            receipt = self.copy.copy_workspace(self.source, self.destination)
        self.assertEqual(receipt['excluded_count'], len(excluded))
        self.assertEqual(receipt['file_count'], 2)
        self.assertIn(included, opened)
        self.assertTrue(all(self.source / name not in opened for name in excluded))
        self.assertEqual({p.as_posix() for p in self.destination.rglob('*') if p.is_file()},
                         {(self.destination / 'README.md').as_posix(),
                          (self.destination / 'src/auth.py').as_posix()})
        self.assertNotIn('PRIVATE_CONTENT', json.dumps(receipt))

    def test_nonempty_destination_is_rejected_without_overwriting(self):
        self.put('data.txt')
        self.destination.mkdir()
        keep = self.destination / 'keep.txt'
        keep.write_bytes(b'keep')
        with self.assertRaises(ValueError):
            self.copy.copy_workspace(self.source, self.destination)
        self.assertEqual(keep.read_bytes(), b'keep')

    def test_git_worktree_pointer_is_filtered_without_opening(self):
        self.put('.git', b'gitdir: D:/private/git-admin')
        self.put('notes.txt', b'keep')
        real_open = self.copy.os.open

        def reject_git_open(path, *args, **kwargs):
            self.assertNotEqual(Path(path), self.source / '.git')
            return real_open(path, *args, **kwargs)

        with patch.object(self.copy.os, 'open', side_effect=reject_git_open):
            receipt = self.copy.copy_workspace(self.source, self.destination)
        self.assertEqual(receipt['excluded_count'], 1)
        self.assertFalse((self.destination / '.git').exists())

    def test_excluded_directory_cannot_be_selected_as_the_source_root(self):
        private = self.source / '.git'
        private.mkdir()
        with patch.object(self.copy.os, 'scandir', side_effect=AssertionError('private root scanned')):
            with self.assertRaises(ValueError):
                self.copy.copy_workspace(private, self.destination)

    def test_destination_created_during_prescan_is_not_taken_over(self):
        self.put('notes.txt', b'keep')
        real_scan = self.copy._scan

        def create_destination(path):
            result = real_scan(path)
            self.destination.mkdir()
            (self.destination / 'other.txt').write_bytes(b'other task')
            return result

        with patch.object(self.copy, '_scan', side_effect=create_destination), self.assertRaises((ValueError, FileExistsError)):
            self.copy.copy_workspace(self.source, self.destination)
        self.assertFalse((self.destination / 'notes.txt').exists())
        self.assertEqual((self.destination / 'other.txt').read_bytes(), b'other task')

    def test_source_and_destination_overlap_are_rejected(self):
        self.put('data.txt')
        for destination in (self.source, self.source / 'nested-copy', self.root):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                self.copy.copy_workspace(self.source, destination)
        self.assertEqual((self.source / 'data.txt').read_bytes(), b'ordinary content')

    def test_overlap_uses_canonical_paths_not_alias_spelling(self):
        alias = self.root / 'source-alias'
        alias.mkdir()
        resolve = Path.resolve

        def canonical(path, *args, **kwargs):
            return self.source if path == alias else resolve(path, *args, **kwargs)

        with patch.object(Path, 'resolve', autospec=True, side_effect=canonical), self.assertRaises(ValueError):
            self.copy.copy_workspace(alias, self.source / 'nested-copy')

    def test_drive_profile_private_and_unc_roots_are_rejected_before_scanning(self):
        dangerous = [Path(self.source.anchor), Path.home(), Path.home().parent,
                     Path.home() / '.ssh', Path('\\\\server\\share\\project')]
        with patch.object(self.copy.os, 'scandir', side_effect=AssertionError('unsafe root was scanned')):
            for source in dangerous:
                with self.subTest(source=source), self.assertRaises(ValueError):
                    self.copy.copy_workspace(source, self.destination)

    def test_limits_fail_instead_of_silently_truncating(self):
        self.put('a.txt', b'123')
        self.put('nested/b.txt', b'456')
        for name, limit in [('MAX_FILES', 1), ('MAX_TOTAL_BYTES', 5),
                            ('MAX_FILE_BYTES', 2), ('MAX_DEPTH', 1), ('MAX_ENTRIES', 1)]:
            with self.subTest(limit=name), patch.object(self.copy, name, limit), self.assertRaises(ValueError):
                self.copy.copy_workspace(self.source, self.destination)
        self.assertEqual((self.source / 'a.txt').read_bytes(), b'123')

    def test_source_change_after_copy_fails_and_retains_partial_destination(self):
        source_file = self.put('data.txt', b'original')
        real_copy = self.copy._copy_file

        def mutate(*args, **kwargs):
            result = real_copy(*args, **kwargs)
            source_file.write_bytes(b'modified')
            return result

        with patch.object(self.copy, '_copy_file', side_effect=mutate), self.assertRaises(ValueError):
            self.copy.copy_workspace(self.source, self.destination)
        self.assertTrue((self.destination / 'data.txt').is_file())
        self.assertEqual(source_file.read_bytes(), b'modified')

    def test_new_source_file_during_copy_is_detected(self):
        self.put('data.txt', b'original')
        real_copy = self.copy._copy_file

        def mutate(*args, **kwargs):
            result = real_copy(*args, **kwargs)
            self.put('new.txt', b'new')
            return result

        with patch.object(self.copy, '_copy_file', side_effect=mutate), self.assertRaises(ValueError):
            self.copy.copy_workspace(self.source, self.destination)

    def test_source_is_rechecked_after_destination_validation(self):
        source_file = self.put('data.txt', b'original')
        real_scan = self.copy._scan

        def mutate_during_destination_scan(path):
            result = real_scan(path)
            if path == self.destination:
                source_file.write_bytes(b'modified')
            return result

        with patch.object(self.copy, '_scan', side_effect=mutate_during_destination_scan), self.assertRaises(ValueError):
            self.copy.copy_workspace(self.source, self.destination)

    def test_replacement_between_lstat_and_open_is_rejected_before_read(self):
        source_file = self.put('data.txt', b'original')
        replacement = self.root / 'replacement.txt'
        replacement.write_bytes(b'other bytes')
        real_open = self.copy.os.open
        replaced = False

        def replace_before_open(path, *args, **kwargs):
            nonlocal replaced
            if Path(path) == source_file and not replaced:
                replaced = True
                source_file.replace(self.root / 'original.txt')
                replacement.replace(source_file)
            return real_open(path, *args, **kwargs)

        with patch.object(self.copy.os, 'open', side_effect=replace_before_open), self.assertRaises(ValueError):
            self.copy.copy_workspace(self.source, self.destination)
        self.assertTrue(replaced)
        self.assertFalse((self.destination / 'data.txt').exists())

    def test_symlink_file_and_directory_are_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'file.txt').write_bytes(b'outside')
        link = self.source / 'link'
        try:
            link.symlink_to(outside / 'file.txt')
        except OSError as exc:
            self.skipTest(f'Symlinks unavailable: {exc}')
        with self.assertRaises(ValueError):
            self.copy.copy_workspace(self.source, self.destination)
        link.unlink()
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.copy.copy_workspace(self.source, self.destination)

    @unittest.skipUnless(os.name == 'nt', 'Windows junction behavior')
    def test_real_junction_and_junction_ancestors_are_rejected(self):
        import _winapi
        outside = self.root / 'outside'
        outside.mkdir()
        junction = self.source / '.runtime'
        _winapi.CreateJunction(str(outside), str(junction))
        self.addCleanup(lambda: junction.rmdir() if junction.exists() else None)
        with self.assertRaises(ValueError):
            self.copy.copy_workspace(self.source, self.destination)
        with self.assertRaises(ValueError):
            self.copy.copy_workspace(junction, self.destination)
        with self.assertRaises(ValueError):
            self.copy.copy_workspace(outside, junction / 'nested')


if __name__ == '__main__':
    unittest.main()
