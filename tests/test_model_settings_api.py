"""Authenticated model controls share the durable queue and session boundaries."""
import http.client
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from dispatcher.control import Controller
from dispatcher.core import Store
from dispatcher.dashboard import start_dashboard
from dispatcher.service import Service
from dispatcher.workspace import WorkspaceManager
from tests.test_service import FakeDida, FakeRunner


class ModelSettingsAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'tasks.db')
        self.source = {'id':'t', 'projectId':'p', 'title':'Write notes', 'tags':[], 'status':0}
        self.config = {'model':'gpt-6.1-sol', 'require_human_review':True,
                       'enabled_executors':['codex','qoder'],
                       'executor_commands':{'codex':['codex','--opaque-private-flag','PRIVATE_SENTINEL']}}
        self.dida = FakeDida(self.source.copy())
        self.runner = FakeRunner(self.root)
        self.service = Service(self.root, self.config, self.store, self.dida, self.runner)
        self.control = Controller(self.service, WorkspaceManager(self.root, self.store),
                                  [{'name':name,'enabled':True} for name in ('codex','qoder')])
        self.service.sync()
        self.catalog = patch('dispatcher.execution_settings._codex_models', return_value=[
            {'id':'gpt-6.1-sol','label':'gpt-6.1-sol','efforts':['low','medium','high','xhigh']}])
        self.catalog.start()

    def tearDown(self):
        self.catalog.stop()
        self.store.close()
        self.temp.cleanup()

    def submit(self, action, **fields):
        return self.control.submit({'action':action,'request_id':str(uuid.uuid4()),**fields})

    def command(self, action, **fields):
        command = self.submit(action, **fields)
        self.control.process_pending()
        return self.store.get_command(command['id'])

    def assignment(self, executor='codex', **fields):
        row = self.store.get('p:t')
        return self.submit('assign', task_id=row['id'], revision=row['revision'], executor=executor, **fields)

    def test_configure_uses_queue_persists_and_does_not_dispatch(self):
        original = self.store.get('p:t')
        self.assertIn('settings', self.control.state()['agents'][0],
                      'Agent cards must expose versioned model defaults')
        queued = self.submit('configure_agent', executor='codex', revision=0,
                             settings={'model':'gpt-6.1-sol','effort':'high'})
        self.assertEqual(self.store.get_agent_settings('codex')['revision'], 0)
        self.control.process_pending()
        result = self.store.get_command(queued['id'])
        self.assertEqual(result['status'], 'done', result['error'])
        agent = next(a for a in self.control.state()['agents'] if a['id']=='codex')
        self.assertEqual(agent['settings'], {'model':'gpt-6.1-sol','effort':'high'})
        self.assertEqual(agent['settings_revision'], 1)
        self.assertEqual(agent['effective_defaults']['model'], 'gpt-6.1-sol')
        self.assertEqual(agent['effective_defaults']['effort'], 'high')
        self.assertTrue(agent['capabilities']['model'])
        self.assertEqual(self.store.get('p:t'), original)
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.runner.executions, 0)
        self.assertEqual((self.dida.comments,self.dida.completions,self.dida.reads), (0,0,0))
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(self.control.state()))

    def test_configure_cas_rejects_stale_without_replacing_default(self):
        first = self.command('configure_agent', executor='codex', revision=0, settings={'effort':'high'})
        self.assertEqual(first['status'], 'done')
        old = self.command('configure_agent', executor='codex', revision=0, settings={'effort':'low'})
        self.assertEqual(old['http_status'], 409)
        self.assertEqual(self.store.get_agent_settings('codex')['settings'], {'effort':'high'})

    def test_configure_rejects_extra_fields_injection_and_invalid_effort(self):
        base = {'action':'configure_agent','executor':'codex','revision':0,'settings':{}}
        for changes in ({'flags':['--unsafe']}, {'settings':{'flags':'--unsafe'}},
                        {'settings':{'model':'--unsafe'}}, {'settings':{'model':'x\n--unsafe'}},
                        {'settings':{'effort':'all'}}, {'settings':[]}, {'revision':True},
                        {'executor':'unknown'}, {'settings':{'model':'unlisted','effort':'high'}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.control.submit(dict(base, request_id=str(uuid.uuid4()), **changes))
        self.assertEqual(self.store.get_agent_settings('codex')['revision'], 0)
        self.assertIsNone(self.store.next_command())

    def test_configure_rechecks_changed_project_baseline_at_execution(self):
        queued = self.submit('configure_agent', executor='codex', revision=0, settings={'effort':'high'})
        self.config['agent_settings'] = {'codex':{'model':'unlisted'}}
        self.control.process_pending()
        result = self.store.get_command(queued['id'])
        self.assertEqual(result['http_status'], 400)
        self.assertEqual(self.store.get_agent_settings('codex')['revision'], 0)

    def test_session_override_is_stored_and_auto_cannot_accept_it(self):
        row = self.store.get('p:t')
        settings = {'model':'gpt-6.1-sol','effort':'high'}
        with self.assertRaises(ValueError):
            self.assignment('auto', execution_overrides=settings)
        self.assertEqual(self.store.get('p:t'), row)
        queued = self.assignment(execution_overrides=settings)
        self.control.process_pending()
        self.assertEqual(self.store.get_command(queued['id'])['status'], 'done')
        self.assertEqual(self.control.task(self.store.get('p:t'))['execution_overrides'], settings)
        self.assertEqual(self.store.get_agent_settings('codex')['settings'], {})
        self.assertEqual(self.store.claim()['execution_overrides'], settings)

    def test_session_overrides_reject_unknown_keys_and_capability_conflicts(self):
        for overrides in ({'args':'--unsafe'}, {'model':'--unsafe'}, {'effort':'max'},
                          {'model':'unknown','effort':'high'}, None, [], {'effort':''}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.assignment(execution_overrides=overrides)
        with self.assertRaises(ValueError):
            self.assignment('qoder', execution_overrides={'model':'Qwen3.8-Max','effort':'high'})
        self.assertIsNone(self.store.next_command())

    def test_assignment_rechecks_new_source_difficulty(self):
        self.config['models'] = {'low':'gpt-6.1-sol','high':'unlisted'}
        queued = self.assignment(execution_overrides={'effort':'high'})
        self.dida.task['tags'] = ['difficulty:hard']
        self.control.process_pending()
        result = self.store.get_command(queued['id'])
        self.assertEqual(result['http_status'], 400)
        self.assertEqual(self.store.get('p:t')['state'], 'ignored')

    def test_projection_uses_frozen_current_run_not_current_defaults(self):
        queued = self.assignment()
        self.control.process_pending()
        self.assertEqual(self.store.get_command(queued['id'])['status'], 'done')
        claimed = self.store.claim()
        frozen = {'executor':'codex','model':'old-model','effort':'low'}
        self.store.mark('p:t','running',expected_run_id=claimed['current_run_id'],
                        result={'executor':'codex','execution_settings':frozen})
        self.command('configure_agent',executor='codex',revision=0,settings={'model':'gpt-6.1-sol','effort':'high'})
        task = self.control.task(self.store.get('p:t'))
        self.assertEqual(task['execution_settings'], frozen)
        self.assertEqual(task['stage'], 'running')
        self.assertFalse(task['can_assign'])
        self.assertEqual(self.store.get_run(claimed['current_run_id'])['execution_settings'], frozen)

    def test_configure_http_requires_same_origin_and_csrf(self):
        with start_dashboard(self.service.snapshot,port=0,control=self.control) as server:
            port = server.server_address[1]
            def request(method,path,payload=None,headers=None):
                connection = http.client.HTTPConnection('127.0.0.1',port)
                try:
                    connection.request(method,path,body=json.dumps(payload) if payload else None,headers=headers or {})
                    response = connection.getresponse()
                    return response.status,response.read()
                finally:
                    connection.close()
            code, body = request('GET','/api/state')
            self.assertEqual(code,200)
            state = json.loads(body)
            payload = {'action':'configure_agent','request_id':'configure-http',
                       'executor':'codex','revision':0,'settings':{'effort':'high'}}
            headers = {'Content-Type':'application/json','Origin':f'http://127.0.0.1:{port}',
                       'X-Dispatcher-Token':state['csrf_token']}
            self.assertEqual(request('POST','/api/actions',payload)[0],403)
            self.assertEqual(request('POST','/api/actions',payload,dict(headers,Origin='https://evil.example'))[0],403)
            self.assertEqual(request('POST','/api/actions',payload,headers)[0],202)
            self.control.process_pending()
            self.assertEqual(self.store.get_agent_settings('codex')['settings'],{'effort':'high'})
            self.assertIsNone(self.store.claim())


if __name__ == '__main__':
    unittest.main()
