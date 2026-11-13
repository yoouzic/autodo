"""Real subprocess coverage for workspace leases; only temporary paths are used."""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


CHILD = r'''
import os, sys, threading
from dispatcher.workspace_lock import WorkspaceLease, WorkspaceBusy
timer = threading.Timer(15, lambda: os._exit(98))
timer.daemon = True
timer.start()
try:
    with WorkspaceLease(sys.argv[1], sys.argv[2], sys.argv[3]):
        print('READY', flush=True)
        if sys.argv[4] == 'hold':
            if sys.stdin.readline().strip() == 'crash':
                os._exit(17)
except WorkspaceBusy:
    print('BUSY', flush=True)
    sys.exit(3)
'''


class WorkspaceLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'dispatcher'
        self.root.mkdir()
        self.workspace = Path(self.temp.name) / 'projects' / 'repo'
        self.workspace.mkdir(parents=True)
        self.registry = self.root / 'data' / 'workspace-leases'
        self.processes = []
        self.addCleanup(self.close_children)

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('dispatcher.workspace_lock'),
                             'The workspace OS lease has not been implemented')
        from dispatcher import workspace_lock
        return workspace_lock

    def start_child(self, path, *, hold=True):
        self.module()
        process = subprocess.Popen([sys.executable, '-u', '-c', CHILD,
            str(self.root), str(path), 'child-run', 'hold' if hold else 'once'],
            cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.processes.append(process)
        return process

    def ready(self, process):
        line = process.stdout.readline().strip()
        self.assertEqual(line, 'READY', line or process.stderr.read())

    def assert_child_busy(self, path):
        process = self.start_child(path, hold=False)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 3, (stdout, stderr))
        self.assertEqual(stdout.strip(), 'BUSY')

    def finish(self, process, command='release'):
        stdout, stderr = process.communicate(command + '\n', timeout=5)
        self.assertEqual(process.returncode, 17 if command == 'crash' else 0, (stdout, stderr))

    def close_children(self):
        for process in self.processes:
            if process.poll() is None:
                process.communicate('release\n', timeout=17)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream and not stream.closed:
                    stream.close()

    def test_same_directory_is_exclusive_across_processes_and_busy_listing_is_read_only(self):
        module = self.module()
        child = self.start_child(self.workspace)
        self.ready(child)
        records = list(self.registry.glob('lease-*.json'))
        before = {path.name: path.read_bytes() for path in records}
        self.assertEqual(module.busy_paths(self.root), [str(self.workspace.resolve())])
        self.assertEqual(before, {path.name: path.read_bytes() for path in records})
        with self.assertRaises(module.WorkspaceBusy):
            with module.WorkspaceLease(self.root, self.workspace, 'contender'):
                self.fail('An occupied workspace was entered')
        self.finish(child)
        self.assertEqual(module.busy_paths(self.root), [])

    def test_parent_and_child_paths_conflict_in_both_directions(self):
        module = self.module()
        nested = self.workspace / 'nested'
        nested.mkdir()
        for held, candidate in ((self.workspace, nested), (nested, self.workspace)):
            with self.subTest(held=held), module.WorkspaceLease(self.root, held, 'owner'):
                self.assert_child_busy(candidate)

    def test_two_concurrent_process_starts_admit_exactly_one_owner(self):
        self.module()
        first = self.start_child(self.workspace)
        second = self.start_child(self.workspace)
        outcomes = [(process, process.stdout.readline().strip()) for process in (first, second)]
        self.assertEqual(sorted(outcome for _, outcome in outcomes), ['BUSY', 'READY'])
        for process, outcome in outcomes:
            if outcome == 'READY':
                self.finish(process)
            else:
                process.communicate(timeout=5)
                self.assertEqual(process.returncode, 3)

    def test_sibling_directories_can_be_held_concurrently(self):
        module = self.module()
        sibling = self.workspace.parent / 'repo-other'
        sibling.mkdir()
        child = self.start_child(self.workspace)
        self.ready(child)
        with module.WorkspaceLease(self.root, sibling, 'sibling'):
            self.assertEqual(set(module.busy_paths(self.root)),
                             {str(self.workspace.resolve()), str(sibling.resolve())})
        self.finish(child)

    @unittest.skipUnless(os.name == 'nt', 'Windows case-insensitive identity')
    def test_windows_case_variants_refer_to_the_same_lock(self):
        module = self.module()
        with module.WorkspaceLease(self.root, self.workspace, 'owner'):
            self.assert_child_busy(str(self.workspace).upper())

    def test_exception_releases_the_lease_and_new_paths_can_be_reserved_before_creation(self):
        module = self.module()
        future = self.workspace / 'not-created-yet'
        with self.assertRaisesRegex(RuntimeError, 'business failure'):
            with module.WorkspaceLease(self.root, future, 'failing'):
                self.assertFalse(future.exists())
                self.assert_child_busy(self.workspace)
                raise RuntimeError('business failure')
        with module.WorkspaceLease(self.root, future, 'next'):
            self.assertEqual(module.busy_paths(self.root), [str(future.resolve())])
        self.assertEqual(list(self.registry.glob('lease-*.json')), [])

    def test_crash_releases_os_lock_and_only_next_acquire_removes_stale_record(self):
        module = self.module()
        child = self.start_child(self.workspace)
        self.ready(child)
        records = list(self.registry.glob('lease-*.json'))
        self.assertEqual(len(records), 1)
        self.finish(child, 'crash')
        self.assertEqual(module.busy_paths(self.root), [])
        self.assertTrue(records[0].exists(), 'Read-only busy_paths must not clean records')
        with module.WorkspaceLease(self.root, self.workspace, 'replacement'):
            self.assertFalse(records[0].exists())
        self.assertEqual(module.busy_paths(self.root), [])

    def test_old_mtime_is_not_authority_to_take_over_an_active_lease(self):
        module = self.module()
        with module.WorkspaceLease(self.root, self.workspace, 'owner'):
            for record in self.registry.glob('lease-*.json'):
                os.utime(record, (1, 1))
            self.assert_child_busy(self.workspace)

    def test_bad_metadata_fails_closed_and_is_not_deleted(self):
        module = self.module()
        self.registry.mkdir(parents=True)
        bad = self.registry / ('lease-' + 'a' * 32 + '.json')
        bad.write_text('{malformed DO_NOT_LEAK', encoding='utf-8')
        before = bad.read_bytes()
        with self.assertRaises(module.WorkspaceBusy) as failure:
            with module.WorkspaceLease(self.root, self.workspace, 'candidate'):
                self.fail('Malformed registration must prevent entry')
        self.assertNotIn('DO_NOT_LEAK', str(failure.exception))
        with self.assertRaises(module.WorkspaceBusy):
            module.busy_paths(self.root)
        self.assertEqual(bad.read_bytes(), before)

    def test_unknown_registry_files_are_not_read_or_deleted(self):
        module = self.module()
        self.registry.mkdir(parents=True)
        unknown = self.registry / 'auth.json'
        unknown.write_text('DO_NOT_READ', encoding='utf-8')
        original_open = Path.open
        def guarded_open(path, *args, **kwargs):
            self.assertNotEqual(path, unknown)
            return original_open(path, *args, **kwargs)
        with patch('pathlib.Path.open', guarded_open), self.assertRaises(module.WorkspaceBusy):
            with module.WorkspaceLease(self.root, self.workspace, 'candidate'):
                self.fail('Unknown registry files must prevent entry')
        self.assertTrue(unknown.exists())

    def test_oversized_metadata_fails_closed_without_reading_or_deleting_it(self):
        module = self.module()
        self.registry.mkdir(parents=True)
        record = self.registry / ('lease-' + 'b' * 32 + '.json')
        record.write_bytes(b'x' * (module.MAX_METADATA_BYTES + 1))
        with patch('pathlib.Path.open', side_effect=AssertionError('Oversized body must not be read')):
            with self.assertRaises(module.WorkspaceBusy):
                module.busy_paths(self.root)
        self.assertTrue(record.exists())

    def test_record_limit_fails_closed_without_displacing_the_owner(self):
        module = self.module()
        sibling = self.workspace.parent / 'other'
        sibling.mkdir()
        with module.WorkspaceLease(self.root, self.workspace, 'owner'):
            with patch.object(module, 'MAX_RECORDS', 1), self.assertRaises(module.WorkspaceBusy):
                with module.WorkspaceLease(self.root, sibling, 'candidate'):
                    self.fail('Registry limit must not be exceeded')
            self.assert_child_busy(self.workspace)

    @unittest.skipUnless(os.name == 'nt', 'Windows reparse point behavior')
    def test_workspace_and_registry_junctions_are_rejected(self):
        import _winapi
        module = self.module()
        alias = self.workspace.parent / 'alias'
        _winapi.CreateJunction(str(self.workspace), str(alias))
        self.addCleanup(lambda: alias.rmdir() if alias.exists() else None)
        with self.assertRaises(module.WorkspaceBusy):
            with module.WorkspaceLease(self.root, alias, 'candidate'):
                self.fail('Junction must be rejected before resolving')
        outside = Path(self.temp.name) / 'outside-registry'
        outside.mkdir()
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        _winapi.CreateJunction(str(outside), str(self.registry))
        self.addCleanup(lambda: self.registry.rmdir() if self.registry.exists() else None)
        with self.assertRaises(module.WorkspaceBusy):
            with module.WorkspaceLease(self.root, self.workspace, 'candidate'):
                self.fail('Registry junction must be rejected')
        self.assertEqual(list(outside.iterdir()), [])

    def test_busy_listing_of_missing_registry_creates_nothing(self):
        module = self.module()
        self.assertEqual(module.busy_paths(self.root), [])
        self.assertFalse((self.root / 'data').exists())


if __name__ == '__main__':
    unittest.main()
