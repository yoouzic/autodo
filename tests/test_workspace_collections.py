"""Only explicit collection containers are traversed; project contents stay unread."""
import importlib
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class WorkspaceCollectionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('dispatcher.workspace_collections'),
                             'Explicit collection discovery is required')
        self.module = importlib.import_module('dispatcher.workspace_collections')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'collection'
        self.root.mkdir()
        self.home = self.base / 'profile'
        self.home.mkdir()
        self.roaming = self.home / 'AppData' / 'Roaming'

    def spec(self, **changes):
        return dict({'path': str(self.root), 'label': 'Desktop projects', 'agent': 'codex', 'groups': []}, **changes)

    def validate(self, specs):
        return self.module.validate_collections(specs, home=self.home, roaming=self.roaming)

    def discover(self, specs):
        return self.module.discover_collections(specs, home=self.home, roaming=self.roaming)

    def mkdir(self, relative):
        path = self.root / relative
        path.mkdir(parents=True, exist_ok=True)
        return path

    def test_validation_normalizes_groups_and_expands_implicit_parent_containers(self):
        spec = self.spec(label='  Desktop projects  ', groups=['da_er\\math', 'da_er/math', 'research'])
        normalized = self.validate([spec])
        self.assertEqual(normalized, [{'path': str(self.root), 'label': 'Desktop projects', 'agent': 'codex',
                                       'groups': ['da_er', 'research', 'da_er/math'], 'exclude': []}])
        self.assertEqual(spec['groups'], ['da_er\\math', 'da_er/math', 'research'])

    def test_optional_agent_and_groups_have_safe_defaults(self):
        normalized = self.validate([{'path': str(self.root), 'label': 'Projects'}])[0]
        self.assertEqual(normalized['groups'], [])
        self.assertEqual(normalized['exclude'], [])
        project = self.mkdir('alpha')
        result = self.discover([normalized])
        self.assertEqual(result['items'][0]['agents'], [])
        self.assertEqual(result['items'][0]['collection_agent'], '')
        self.assertEqual(result['items'][0]['path'], str(project))

    def test_only_root_and_explicit_groups_are_traversed(self):
        self.mkdir('standalone/src/internal')
        self.mkdir('da_er/math/linear/src')
        self.mkdir('da_er/english/notes')
        self.mkdir('.git/objects')
        (self.root / 'README.md').write_text('not a project', encoding='utf-8')
        result = self.discover([self.spec(groups=['da_er/math'])])
        items = {item['relative_path']: item for item in result['items']}
        self.assertEqual(set(items), {'standalone', 'da_er/english', 'da_er/math/linear'})
        self.assertEqual(items['da_er/math/linear'], {
            'path': str(self.root / 'da_er/math/linear'), 'agents': ['codex'], 'origin': 'collection',
            'label': 'linear', 'collection_label': 'Desktop projects', 'collection_root': str(self.root),
            'relative_path': 'da_er/math/linear', 'category': 'da_er/math', 'collection_agent': 'codex'})
        self.assertEqual(set(result['containers']), {str(self.root), str(self.root / 'da_er'), str(self.root / 'da_er/math')})
        self.assertEqual(result['warnings'], [])

    def test_discovery_never_opens_file_contents_or_scans_project_children(self):
        project = self.mkdir('project')
        (project / 'credentials.json').write_text('PRIVATE', encoding='utf-8')
        (self.root / 'auth.json').write_text('PRIVATE', encoding='utf-8')
        scandir = self.module.os.scandir

        def only_containers(path):
            self.assertEqual(Path(path), self.root)
            return scandir(path)

        with patch.object(Path, 'open', side_effect=AssertionError('must not read file contents')), \
                patch.object(self.module.os, 'scandir', side_effect=only_containers):
            result = self.discover([self.spec()])
        self.assertEqual([item['path'] for item in result['items']], [str(project)])
        self.assertNotIn('PRIVATE', str(result))

    def test_hidden_and_dependency_cache_directories_are_skipped(self):
        for name in ('.git', '.runtime', '.ssh', '.hidden', 'node_modules', '.venv', 'venv',
                     '__pycache__', '.pytest_cache', '.cache', 'build', 'dist'):
            self.mkdir(name)
        self.mkdir('real-project')
        result = self.discover([self.spec()])
        self.assertEqual([item['label'] for item in result['items']], ['real-project'])

    def test_missing_root_and_groups_warn_but_remain_containers(self):
        missing = self.base / 'not-created'
        specs = [self.spec(path=str(missing), groups=['courses/math'])]
        self.assertEqual(self.validate(specs)[0]['path'], str(missing))
        result = self.discover(specs)
        self.assertEqual(result['items'], [])
        self.assertEqual(set(result['containers']), {str(missing), str(missing / 'courses'), str(missing / 'courses/math')})
        self.assertTrue(result['warnings'])

    def test_sensitive_roots_and_relative_or_unc_roots_are_rejected(self):
        roots = [self.root.anchor, self.home, self.home.parent, self.home / '.ssh',
                 self.home / '.codex' / 'sessions', self.roaming / 'Codex',
                 'relative/project', '\\\\server\\share\\projects']
        for root in roots:
            with self.subTest(root=root), self.assertRaises(ValueError):
                self.validate([self.spec(path=str(root))])

    def test_overlapping_and_case_alias_roots_are_rejected(self):
        for other in (self.root, self.root / 'sub', self.base, Path(str(self.root).upper())):
            with self.subTest(other=other), self.assertRaises(ValueError):
                self.validate([self.spec(), self.spec(path=str(other), label='Other')])

    def test_invalid_configuration_and_escape_groups_are_rejected(self):
        invalid = [None, {}, ['bad'], [self.spec(unknown='field')], [self.spec(agent='unknown')],
                   [self.spec(label='')], [self.spec(groups='courses')], [self.spec(exclude='out')]]
        for group in ('../outside', 'one/../../outside', '/absolute', 'C:relative',
                      'a:b', 'a/*', 'a/CON', '.git', 'a/./../b', 'a/b/c/d/e',
                      'line\nbreak', '.', 'trailing. ', 'a/secret\x00'):
            invalid.append([self.spec(groups=[group])])
        for specs in invalid:
            with self.subTest(specs=specs), self.assertRaises(ValueError):
                self.validate(specs)

    def test_four_group_levels_are_allowed(self):
        self.mkdir('a/b/c/d/project')
        result = self.discover([self.spec(groups=['a/b/c/d'])])
        self.assertEqual([item['relative_path'] for item in result['items']], ['a/b/c/d/project'])

    def test_excludes_skip_exact_paths_and_subtrees_without_globs(self):
        for relative in ('bi_sai/backups/private', 'bi_sai/project/src', 'docs/private', 'plain'):
            self.mkdir(relative)
        spec = self.spec(groups=['bi_sai', 'bi_sai/backups'], exclude=['bi_sai/backups', 'docs'])
        result = self.discover([spec])
        self.assertEqual({item['relative_path'] for item in result['items']}, {'bi_sai/project', 'plain'})
        self.assertEqual(set(result['excluded']), {str(self.root / 'bi_sai/backups'), str(self.root / 'docs')})
        self.assertIn(str(self.root / 'bi_sai/backups'), result['containers'])
        for excluded in ('../outside', '/outside', 'build/*', '.', 'file:stream'):
            with self.subTest(excluded=excluded), self.assertRaises(ValueError):
                self.validate([self.spec(exclude=[excluded])])

    def test_root_limit_keeps_unscanned_groups_as_containers(self):
        for relative in ('group/a', 'group/b', 'group/c', 'other/x'):
            self.mkdir(relative)
        with patch.object(self.module, 'MAX_DIRS_PER_ROOT', 2):
            result = self.discover([self.spec(groups=['group', 'other'])])
        self.assertTrue(result['warnings'])
        self.assertLessEqual(len(result['items']), 2)
        self.assertFalse(any(item['relative_path'] in {'group', 'other'} for item in result['items']))
        self.assertEqual(set(result['containers']), {str(self.root), str(self.root / 'group'), str(self.root / 'other')})

    def test_global_limit_counts_files_and_preserves_all_configured_containers(self):
        for number in range(5):
            (self.root / f'{number}.txt').write_text('content')
        other = self.base / 'other'
        other.mkdir()
        (other / 'project').mkdir()
        specs = [self.spec(), self.spec(path=str(other), label='Other', groups=['group'])]
        with patch.object(self.module, 'MAX_SCAN_ENTRIES', 2):
            result = self.discover(specs)
        self.assertTrue(result['warnings'])
        self.assertEqual(result['items'], [])
        self.assertIn(str(other / 'group'), result['containers'])

    def test_unreadable_collection_warns_and_other_roots_still_work(self):
        other = self.base / 'other'
        other.mkdir()
        (other / 'project').mkdir()
        scandir = self.module.os.scandir

        def denied(path):
            if Path(path) == self.root:
                raise PermissionError('denied')
            return scandir(path)

        with patch.object(self.module.os, 'scandir', side_effect=denied):
            result = self.discover([self.spec(), self.spec(path=str(other), label='Other')])
        self.assertTrue(result['warnings'])
        self.assertEqual([item['path'] for item in result['items']], [str(other / 'project')])

    def test_root_metadata_failure_preserves_all_configured_boundaries(self):
        other = self.base / 'other'
        (other / 'courses' / 'project').mkdir(parents=True)
        specs = self.validate([self.spec(groups=['groups/sub'], exclude=['private']),
                               self.spec(path=str(other), label='Other', groups=['courses'], exclude=['archives'])])
        lstat = Path.lstat

        def inaccessible(path, *args, **kwargs):
            if path == self.root:
                raise PermissionError('metadata unavailable')
            return lstat(path, *args, **kwargs)

        with patch.object(Path, 'lstat', autospec=True, side_effect=inaccessible):
            result = self.discover(specs)
        self.assertEqual(set(result['containers']), {str(self.root), str(self.root / 'groups'),
                         str(self.root / 'groups/sub'), str(other), str(other / 'courses')})
        self.assertEqual(set(result['excluded']), {str(self.root / 'private'), str(other / 'archives')})
        self.assertEqual([item['path'] for item in result['items']], [str(other / 'courses/project')])
        self.assertTrue(result['warnings'])

    @unittest.skipUnless(os.name == 'nt', 'Windows junction behavior')
    def test_root_changed_to_junction_keeps_boundaries_and_other_collections(self):
        import _winapi
        changed = self.base / 'changed-root'
        outside = self.base / 'outside'
        (outside / 'private-project').mkdir(parents=True)
        self.mkdir('courses/project')
        specs = self.validate([self.spec(path=str(changed), label='Changed', groups=['category'], exclude=['private']),
                               self.spec(groups=['courses'], exclude=['archives'])])
        _winapi.CreateJunction(str(outside), str(changed))
        self.addCleanup(lambda: changed.rmdir() if changed.exists() else None)
        result = self.discover(specs)
        self.assertEqual(set(result['containers']), {str(changed), str(changed / 'category'),
                                                    str(self.root), str(self.root / 'courses')})
        self.assertEqual(set(result['excluded']), {str(changed / 'private'), str(self.root / 'archives')})
        self.assertEqual([item['path'] for item in result['items']], [str(self.root / 'courses/project')])
        self.assertTrue(result['warnings'])
        with self.assertRaises(ValueError):
            self.validate(specs)

    @unittest.skipUnless(os.name == 'nt', 'Windows junction behavior')
    def test_real_junction_is_skipped_even_when_declared_group(self):
        import _winapi
        outside = self.base / 'outside'
        (outside / 'private').mkdir(parents=True)
        junction = self.root / 'linked'
        _winapi.CreateJunction(str(outside), str(junction))
        self.addCleanup(lambda: junction.rmdir() if junction.exists() else None)
        self.mkdir('project')
        result = self.discover([self.spec(groups=['linked'])])
        self.assertEqual([item['label'] for item in result['items']], ['project'])
        self.assertTrue(result['warnings'])
        self.assertIn(str(junction), result['containers'])

    def test_invalid_discovery_configuration_fails_with_warning(self):
        result = self.discover([self.spec(groups=['../escape'])])
        self.assertEqual(result['items'], [])
        self.assertTrue(result['warnings'])


if __name__ == '__main__':
    unittest.main()
