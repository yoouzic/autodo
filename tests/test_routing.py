import unittest
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from dispatcher.routing import choose_executor, RoutingError
from dispatcher.runner import MultiAgentRunner


class RoutingTests(unittest.TestCase):
    def test_manual_executor_wins_over_conflicting_source_tags(self):
        route = choose_executor({'tags': ['agent:dsh', 'agent:zcode']}, 'easy', 'document', {},
                                {'codex': True}, manual_executor='codex')
        self.assertEqual(route['executor'], 'codex')
        self.assertFalse(route['automatic'])
        self.assertFalse(route['fallback'])
        self.assertIn('Manual', route['reason'])

    def test_unavailable_manual_executor_never_falls_back(self):
        with self.assertRaisesRegex(RoutingError, 'unavailable'):
            choose_executor({}, 'easy', 'document', {}, {'codex': True, 'dsh': False}, manual_executor='dsh')

    def test_prepare_applies_local_manual_override(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as leases:
            with patch('dispatcher.runner.discover_executors', return_value={'codex': {'enabled': True}}):
                runner = MultiAgentRunner(Path(directory), {'codex_command': ['codex']})
            row = {'payload': {'id': 't', 'projectId': 'p', 'tags': ['agent:dsh']},
                   'fingerprint': 'a' * 64, 'difficulty': 'easy', 'kind': 'document', 'manual_executor': 'codex'}
            run = runner.prepare(row)
            leases.callback(runner.release_workspace,row)
            self.assertIn('Manual assignment: codex', (run / 'routing.json').read_text(encoding='utf-8'))

    def test_difficulty_and_kind_routes(self):
        available = dict.fromkeys(['codex','dsh','zcode','qoder','antigravity'], True)
        for difficulty, kind, expected in [('easy','document','dsh'), ('medium','document','zcode'),
                                         ('medium','code','qoder'), ('hard','code','codex'),
                                         ('hard','document','codex')]:
            with self.subTest(difficulty=difficulty,kind=kind):
                self.assertEqual(choose_executor({'tags':[]},difficulty,kind,{},available)['executor'],expected)

    def test_unavailable_automatic_route_falls_back_before_launch(self):
        r = choose_executor({'priority':5},'medium','document',{}, {'codex':True,'zcode':False})
        self.assertEqual(r['executor'],'codex')
        self.assertTrue(r['fallback'])
        self.assertIn('zcode',r['reason'])

    def test_explicit_tag_cannot_silently_fallback(self):
        with self.assertRaises(RoutingError):
            choose_executor({'tags':['AI','agent:zcode']},'easy','document',{}, {'codex':True,'zcode':False})
        r = choose_executor({'tags':['agent:zcode']},'hard','code',{}, {'zcode':True})
        self.assertEqual(r['executor'],'zcode')
        self.assertFalse(r['automatic'])

    def test_priority_does_not_change_executor(self):
        a = choose_executor({'priority':5},'easy','document',{}, {'dsh':True})
        b = choose_executor({'priority':1},'easy','document',{}, {'dsh':True})
        self.assertEqual(a,b)

    def test_conflicting_and_unknown_tags_rejected(self):
        for tags in [['agent:dsh','agent:codex'],['agent:bogus']]:
            with self.assertRaises(RoutingError):
                choose_executor({'tags':tags},'easy','document',{}, {'dsh':True,'codex':True})

    def test_custom_order_is_respected(self):
        config={'executor_routes':{'easy':['zcode','dsh','codex']}}
        self.assertEqual(choose_executor({},'easy','document',config,{'zcode':True,'dsh':True})['executor'],'zcode')


if __name__=='__main__': unittest.main()
