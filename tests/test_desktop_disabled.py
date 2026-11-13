"""Explicit desktop disablement must not fall back to a CLI or stale worker."""
import contextlib
import copy
import importlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from dispatcher.control import Controller
from dispatcher.core import Store
from dispatcher.execution_settings import agent_capabilities, resolve_execution_settings
from dispatcher.executors import discover_executors
from dispatcher.routing import RoutingError, choose_executor, task_available_executors
from dispatcher.runner import CodexRunner, MultiAgentRunner
from dispatcher.service import Service
from dispatcher.workspace import WorkspaceManager
from dispatcher.workbuddy_runner import execute_desktop, refresh_worker_registry
from tests.test_service import FakeDida


class DesktopDisabledTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = {'codex_command': [sys.executable],
            'enabled_executors': ['codex', 'workbuddy', 'doubao'],
            'executor_commands': {'workbuddy': [sys.executable], 'doubao': [sys.executable]},
            'workbuddy_transport': 'mcp', 'doubao_transport': 'disabled',
            'require_human_review': True}
        self.stale = {name: {'name': name, 'enabled': True, 'detected': True,
                            'command': [sys.executable], 'transport': 'desktop_mcp'}
                      for name in ('codex', 'workbuddy', 'doubao')}
        with patch('dispatcher.runner.discover_executors', return_value=self.stale):
            self.runner = MultiAgentRunner(self.root, self.config)
        self.runner.doubao_broker = Mock(provider='doubao')
        self.runner.doubao_broker.status.side_effect = AssertionError('Disabled broker must not be queried')
        self.runner.workbuddy_broker = Mock(provider='workbuddy')
        self.runner.workbuddy_broker.status.return_value = {'online': True, 'ready': True}

    def test_discovery_disabled_overrides_installed_enabled_membership(self):
        registry = discover_executors(self.root, self.config)
        self.assertTrue(registry['doubao']['detected'])
        self.assertFalse(registry['doubao']['enabled'])
        self.assertEqual(registry['doubao']['transport'], 'disabled')
        self.assertIn('已停用', registry['doubao']['status'])
        self.assertTrue(registry['workbuddy']['enabled'])

    def test_refresh_clears_stale_enabled_state_without_querying_disabled_broker(self):
        before = copy.deepcopy(self.stale)
        registry = refresh_worker_registry(self.stale, self.config, self.runner)
        self.assertFalse(registry['doubao']['enabled'])
        self.assertEqual(registry['doubao']['transport'], 'disabled')
        self.assertIn('已停用', registry['doubao']['status'])
        self.runner.doubao_broker.status.assert_not_called()
        self.assertTrue(registry['workbuddy']['enabled'])
        self.assertEqual(self.stale, before)
        self.assertEqual(self.config['workbuddy_transport'], 'mcp')

    def test_disabled_capabilities_and_settings_refuse_all_layers(self):
        for name in ('workbuddy', 'doubao'):
            config = dict(self.config, **{name + '_transport': 'disabled'})
            with self.subTest(provider=name):
                capability = agent_capabilities(name, config)
                self.assertFalse(capability['model'])
                self.assertEqual(capability['models'], [])
                self.assertEqual(capability['efforts'], [])
                self.assertIn('已停用', capability['note'])
                for defaults, overrides in (({}, {}), ({'model': 'saved-model'}, {}),
                                            ({}, {'effort': 'high'})):
                    with self.assertRaisesRegex(ValueError, '已停用'):
                        resolve_execution_settings(name, config, defaults, overrides, 'easy')

    def test_external_and_direct_desktop_entry_refuse_before_any_launch(self):
        for name in ('workbuddy', 'doubao'):
            with self.subTest(provider=name):
                self.runner.config = dict(self.config, **{name + '_transport': 'disabled'})
                with (patch.object(self.runner, 'workspace_path', side_effect=AssertionError('No workspace access')),
                      patch('dispatcher.runner.invocation', side_effect=AssertionError('No CLI invocation')),
                      patch('dispatcher.runner.run_process', side_effect=AssertionError('No process')),
                      patch.object(self.runner, '_invoke_doubao', side_effect=AssertionError('No native CLI'))):
                    with self.assertRaisesRegex(RoutingError, '已停用'):
                        self.runner._invoke_external(name, {}, self.root / 'uncreated-run')
                    with self.assertRaisesRegex(RoutingError, '已停用'):
                        execute_desktop(self.runner, {}, self.root / 'uncreated-run', executor=name)
                self.assertFalse((self.root / 'uncreated-run').exists())

    def test_manual_explicit_and_automatic_routes_do_not_reuse_stale_enabled_state(self):
        task = {'id': 't', 'projectId': 'p', 'title': '整理阅读提纲', 'status': 0, 'tags': []}
        for manual, tags in (('doubao', []), (None, ['agent:doubao'])):
            row = {'payload': dict(task, tags=tags), 'difficulty': 'easy', 'kind': 'document',
                   'manual_executor': manual}
            with self.subTest(manual=manual), patch.object(CodexRunner, 'prepare') as prepare:
                with self.assertRaises(RoutingError):
                    self.runner.prepare(row)
                prepare.assert_not_called()
        available = task_available_executors({'kind': 'document'}, self.config, self.runner.registry)
        config = dict(self.config, executor_routes={'easy': ['doubao', 'codex']})
        route = choose_executor(task, 'easy', 'document', config, available)
        self.assertEqual(route['executor'], 'codex')
        self.assertTrue(route['fallback'])

    def test_controller_rejects_assignment_and_exposes_disabled_status(self):
        store = Store(self.root / 'tasks.db')
        self.addCleanup(store.close)
        task = {'id': 't', 'projectId': 'p', 'title': '整理阅读提纲', 'status': 0, 'tags': []}
        service = Service(self.root, self.config, store, FakeDida(task), self.runner)
        controller = Controller(service, WorkspaceManager(self.root, store), list(self.stale.values()))
        with self.assertRaises(RoutingError):
            controller._validate_assignment_route('doubao', task)
        with self.assertRaises(RoutingError):
            controller._validate_assignment_route('auto', dict(task, tags=['agent:doubao']))
        with self.assertRaisesRegex(ValueError, '已停用'):
            controller._validate_agent_settings('doubao', {})
        agents = {agent['id']: agent for agent in controller.state()['agents']}
        self.assertFalse(agents['doubao']['enabled'])
        self.assertIn('已停用', agents['doubao']['status'])
        self.assertFalse(agents['doubao']['capabilities']['model'])
        self.assertTrue(agents['workbuddy']['enabled'])
        self.runner.doubao_broker.status.assert_not_called()


class DesktopDisabledStartupTests(unittest.TestCase):
    def test_config_accepts_disabled_preserving_saved_settings(self):
        cli = importlib.import_module('dispatcher.__main__')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for provider in ('workbuddy', 'doubao'):
                config = {provider + '_transport': 'disabled', 'enabled_executors': [provider],
                          'agent_settings': {provider: {'model': 'saved-model'}}}
                (root / 'config.local.json').write_text(json.dumps(config), encoding='utf-8')
                with self.subTest(provider=provider):
                    value = cli.load_config(root)
                    self.assertEqual(value[provider + '_transport'], 'disabled')
                    self.assertEqual(value['agent_settings'], config['agent_settings'])
                config['agent_settings'][provider] = {'unsupported': 'value'}
                (root / 'config.local.json').write_text(json.dumps(config), encoding='utf-8')
                with self.assertRaises(ValueError):
                    cli.load_config(root)

    def test_disabled_daemon_starts_no_broker_but_keeps_workbuddy_setup_available(self):
        cli = importlib.import_module('dispatcher.__main__')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # WorkBuddy MCP setup still starts before membership is enabled.
            (root / 'config.local.json').write_text(json.dumps({
                'workbuddy_transport': 'mcp', 'doubao_transport': 'disabled',
                'enabled_executors': ['codex', 'doubao']}), encoding='utf-8')
            brokers = {}
            def construct(root, *, provider):
                self.assertEqual(provider, 'workbuddy')
                broker = Mock(provider=provider)
                broker.start.return_value = broker
                brokers[provider] = broker
                return broker
            service = SimpleNamespace(store=Mock(), runner=SimpleNamespace(), snapshot=lambda: {})
            def daemon():
                self.assertIs(service.runner.workbuddy_broker, brokers['workbuddy'])
                self.assertFalse(hasattr(service.runner, 'doubao_broker'))
            service.daemon = daemon
            with (patch.object(cli, 'make_service', return_value=service),
                  patch.object(cli, 'detect_agents', return_value=[]),
                  patch.object(cli, 'start_dashboard', return_value=contextlib.nullcontext()),
                  patch('dispatcher.workbuddy_mcp.WorkBuddyBroker', side_effect=construct),
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(cli.main(['--root', str(root), 'daemon']), 0)
            self.assertEqual(set(brokers), {'workbuddy'})
            brokers['workbuddy'].start.assert_called_once()
            brokers['workbuddy'].close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
