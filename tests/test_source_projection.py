"""Source-completion projection and isolated browser-rendering regressions."""

import copy
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from dispatcher.control import Controller


class ProjectionStore:
    def __init__(self, row, run):
        self.row, self.run = row, run

    def get_run(self, run_id):
        return copy.deepcopy(self.run) if self.run and self.run['id'] == run_id else None

    def list_tasks(self):
        return [copy.deepcopy(self.row)]

    def get(self, task_id):
        return copy.deepcopy(self.row) if self.row['id'] == task_id else None

    def get_agent_settings(self, name):
        return {'settings': {}, 'revision': 0}

    def events(self, limit):
        return [{'task_id': self.row['id'], 'message': 'Original review retained'}]


def fixture(state='cancelled', source_status=2):
    row = {'id': 'p:t', 'payload': {'title': 'Phone completed task', 'content': 'Original description',
                                 'status': 0, 'id': 't', 'projectId': 'p'},
           'state': state, 'source_status': source_status, 'source_checked_at': '2026-11-07T12:34:56+08:00',
           'difficulty': 'medium', 'kind': 'document', 'delegated': 1, 'paused': 1,
           'current_run_id': 'run-1', 'run_dir': 'isolated/run-1', 'revision': 3,
           'result': {'summary': 'Original result', 'checks': ['Historical check'],
                      'sha256': {'answer.md': 'a' * 64}, 'executor': 'codex'}}
    run = {'id': 'run-1', 'task_id': 'p:t', 'state': 'awaiting_review',
           'title': 'Phone completed task', 'executor': 'codex', 'review_status': 'pending',
           'review_note': 'Original review note', 'result': copy.deepcopy(row['result'])}
    store = ProjectionStore(row, run)
    service = SimpleNamespace(store=store, config={}, stop=Event(), last_sync='2026-11-07T12:34:56+08:00',
                              last_error=None, tunnel=None)
    workspaces = SimpleNamespace(list_runs=lambda task_id=None: [copy.deepcopy(run)])
    controller = Controller(service, workspaces, [{'name': 'codex', 'enabled': True}])
    return controller, row, run


class SourceProjectionTests(unittest.TestCase):
    def test_source_completed_moves_to_done_without_claiming_review_success(self):
        controller, row, run = fixture()
        original = copy.deepcopy((row, run))
        task = controller.task(row)
        self.assertEqual(task['stage'], 'done')
        self.assertTrue(task['source_completed'])
        self.assertEqual(task['source_status'], 2)
        self.assertEqual(task['source_checked_at'], row['source_checked_at'])
        self.assertEqual(task['state'], 'cancelled')
        self.assertIsNone(task['auto_review_status'])
        self.assertEqual(task['review_status'], 'pending')
        self.assertEqual(task['review_note'], 'Original review note')
        self.assertEqual(task['summary'], 'Original result')
        self.assertEqual(task['checks'], ['Historical check'])
        self.assertEqual(task['current_run_id'], 'run-1')
        self.assertIsNotNone(task['manifest_digest'])
        visible = controller.detail(row['id'])['runs']
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]['id'], run['id'])
        self.assertEqual(visible[0]['review_status'], run['review_status'])
        self.assertEqual(visible[0]['summary'], 'Original result')
        self.assertEqual(visible[0]['checks'], ['Historical check'])
        self.assertNotIn('result', visible[0])  # private raw receipts stay local
        self.assertEqual((row, run), original)

    def test_source_completion_disables_all_task_actions_despite_stale_local_state(self):
        for state in ('ignored', 'queued', 'cancelled', 'awaiting_review', 'needs_review', 'revision_requested', 'done'):
            with self.subTest(state=state):
                controller, row, _ = fixture(state)
                task = controller.task(row)
                for action in ('assign', 'approve', 'reject', 'withdraw'):
                    self.assertFalse(task['can_' + action], action)
                self.assertEqual(task['stage'], 'done')
                if state != 'done':
                    self.assertIsNone(task['auto_review_status'])

    def test_reopened_cancelled_task_can_be_explicitly_reassigned(self):
        controller, row, _ = fixture(source_status=0)
        task = controller.task(row)
        self.assertIn('source_completed', task)
        self.assertFalse(task['source_completed'])
        self.assertEqual(task['stage'], 'backlog')
        self.assertTrue(task['can_assign'])
        self.assertFalse(task['can_approve'])
        self.assertFalse(task['can_reject'])
        self.assertEqual(row['paused'], 1)

    def test_existing_dispatcher_completion_keeps_its_review_meaning(self):
        controller, row, run = fixture(state='done')
        run['review_status'] = 'approved'
        task = controller.task(row)
        self.assertIn('source_completed', task)
        self.assertFalse(task['source_completed'])
        self.assertEqual(task['stage'], 'done')
        self.assertEqual(task['review_status'], 'approved')
        self.assertEqual(task['auto_review_status'], 'passed')

    def test_old_row_without_source_metadata_is_not_assumed_completed(self):
        controller, row, _ = fixture(state='ignored')
        row.pop('source_status')
        row.pop('source_checked_at')
        task = controller.task(row)
        self.assertIn('source_completed', task)
        self.assertFalse(task['source_completed'])
        self.assertIsNone(task['source_status'])
        self.assertIsNone(task['source_checked_at'])
        self.assertEqual(task['stage'], 'backlog')

    def test_sync_projection_reports_real_cadence_activity_and_warnings(self):
        controller, _, _ = fixture()
        controller.service.config['poll_seconds'] = 37
        controller.service.syncing = True
        controller.service.last_sync_duration_seconds = 4.25
        controller.service.warnings = ['部分任务状态尚未确认，将在下次同步重试。']
        system = controller.state()['system']
        self.assertIn('poll_seconds', system)
        self.assertEqual(system['poll_seconds'], 37)
        self.assertTrue(system['syncing'])
        self.assertEqual(system['last_sync_duration_seconds'], 4.25)
        self.assertEqual(system['sync_warnings'], controller.service.warnings)

    def test_sync_projection_has_compatible_defaults(self):
        controller, _, _ = fixture()
        system = controller.state()['system']
        self.assertIn('poll_seconds', system)
        self.assertEqual(system['poll_seconds'], 20)
        self.assertFalse(system['syncing'])
        self.assertIsNone(system['last_sync_duration_seconds'])
        self.assertEqual(system['sync_warnings'], [])

    def test_manual_sync_requests_an_immediate_source_recheck(self):
        controller, _, _ = fixture()
        calls = []
        controller.service.sync = lambda **kwargs: calls.append(kwargs) or {'synced': True}
        self.assertEqual(controller._execute('sync', {}), {'synced': True})
        self.assertEqual(calls, [{'force': True}])


@unittest.skipUnless(shutil.which('node'), 'Node is required for isolated UI rendering')
class SourceBrowserProjectionTests(unittest.TestCase):
    def render_fixture(self, source_status=2, executor='codex', recommendation=None, task_fields=None, now=None):
        controller, row, run = fixture(source_status=source_status)
        if task_fields:
            row['payload'].update(task_fields)
            if 'priority' in task_fields: row['priority'] = task_fields['priority']
        row['result']['executor'] = executor
        run['executor'] = executor
        state = controller.state()
        if recommendation:
            state['tasks'][0]['recommended_executor'] = recommendation
        state['agents'] = []
        state['system'].update(poll_seconds=37, syncing=True, last_sync_duration_seconds=4.25,
                               sync_warnings=['部分任务状态尚未确认，将在下次同步重试。'])
        data = {'state': state, 'detail': controller.detail(row['id']), 'now': now}
        script = r'''
const fs = require('node:fs'), vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const ids = new Map();
class Element {
  constructor(tag, text='') { this.tagName=tag.toUpperCase(); this.children=[]; this._text=text; this.dataset={}; this.style={values:{},setProperty(k,v){this.values[k]=v;}}; this.listeners={}; this.options=[]; this.hidden=false; this.disabled=false; this.value=''; this.className=''; this.classList={add(){},remove(){},toggle(){}}; }
  set id(value) {this._id=value; ids.set(value,this);} get id(){return this._id;}
  set textContent(value){this._text=String(value); this.children=[];} get textContent(){return this._text+this.children.map(x=>typeof x==='string'?x:x.textContent).join('');}
  get childNodes(){return this.children;}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this._text='';this.children=items;this.options=[];}
  add(option){this.options.push(option);this.append(option);}
  setAttribute(name,value){this[name]=value;} removeAttribute(name){delete this[name];}
  addEventListener(name,handler){this.listeners[name]=handler;}
  contains(){return false;} querySelector(){return null;} querySelectorAll(){return [];}
  focus(){} getClientRects(){return [1];}
}
const document={getElementById(id){if(!ids.has(id)){const item=new Element('div'); item.id=id;}return ids.get(id);},createElement(tag){return new Element(tag);},querySelectorAll(){return [];},querySelector(){return null;},addEventListener(){},body:new Element('body'),hidden:false,activeElement:null};
document.getElementById('task-drawer').hidden=true;
const context={document,window:{addEventListener(){}},location:{hash:'#board'},Option:class extends Element {constructor(text,value){super('option',text);this.value=value;}},AbortController,console,setTimeout(){return 1;},clearTimeout(){},setInterval(){},fetch:async path=>({ok:true,status:200,json:async()=>path==='/api/state'?input.state:input.detail})};
if(input.now)context.Date=class extends Date {constructor(...args){super(...(args.length?args:[input.now]));}static now(){return new Date(input.now).getTime();}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
const descendants = node=>[node,...node.children.filter(x=>typeof x!=='string').flatMap(descendants)];
setImmediate(async()=>{
  const board=document.getElementById('view-content');
  const card=descendants(board).find(x=>x.className==='task-card');
  const boardText=board.textContent;
  if(!card)throw new Error('Task card was not rendered');
  card.listeners.click();
  await new Promise(resolve=>setImmediate(resolve));
  const drawer=document.getElementById('drawer-content');
  console.log(JSON.stringify({board:boardText,card:card.textContent,cardLabel:card['aria-label'],cardColor:card.style.values['--task-color'],cardClasses:descendants(card).map(x=>x.className),drawer:drawer.textContent,buttons:descendants(drawer).filter(x=>x.tagName==='BUTTON').map(x=>({text:x.textContent,disabled:x.disabled})),sync:document.getElementById('last-sync').textContent,cadence:document.getElementById('sync-cadence').textContent,warning:document.getElementById('system-error').textContent,syncButton:document.getElementById('sync-button').textContent}));
});
'''
        result = subprocess.run([shutil.which('node'), '-e', script,
                                 str(Path(__file__).resolve().parents[1] / 'dispatcher/static/app.js')],
                                input=json.dumps(data), capture_output=True, text=True, encoding='utf-8', timeout=10,
                                env=dict(os.environ,TZ='America/Los_Angeles') if now else None)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_completed_task_is_labelled_and_does_not_offer_review(self):
        ui = self.render_fixture()
        self.assertIn('滴答已完成', ui['board'])
        self.assertIn('滴答已完成', ui['drawer'])
        self.assertIn('不代表自动检查或人工复审通过', ui['drawer'])
        self.assertIn('Original review note', ui['drawer'])
        self.assertFalse(any('批准并完成滴答' in item['text'] for item in ui['buttons']))
        self.assertNotIn('自动检查通过', ui['drawer'])
        self.assertIn('codex', ui['card'])
        self.assertIn('codex', ui['cardLabel'])

    def test_completed_unassigned_task_does_not_suggest_pending_assignment(self):
        for recommendation in (None, 'dsh'):
            with self.subTest(recommendation=recommendation):
                ui = self.render_fixture(executor=None, recommendation=recommendation)
                for field in ('card', 'cardLabel'):
                    self.assertIn('未派工', ui[field])
                    self.assertNotIn('待分配', ui[field])
                    self.assertNotIn('未分配', ui[field])
                    self.assertNotIn('建议', ui[field])

    def test_footer_distinguishes_source_sync_from_display_refresh_and_warns(self):
        ui = self.render_fixture()
        self.assertIn('界面每 2 秒刷新', ui['cadence'])
        self.assertIn('滴答每 37 秒同步', ui['cadence'])
        self.assertRegex(ui['sync'], r'\d{2}:\d{2}:\d{2}')
        self.assertIn('部分任务状态尚未确认', ui['warning'])
        self.assertIn('正在同步', ui['syncButton'])

    def test_reopened_task_explains_manual_reassignment(self):
        ui = self.render_fixture(source_status=0)
        self.assertIn('滴答已重新打开', ui['drawer'])
        self.assertTrue(any(item['text']=='重新分配' and not item['disabled'] for item in ui['buttons']))


if __name__ == '__main__':
    unittest.main()
