/* Isolated DOM harness: no browser, live service, files, or executor is started. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const test=require('node:test');
const path=require('node:path');

function harness({choice,items=[],runs=[],workspaceCapability=true}={}) {
  const roots=[];
  const descendants=node=>[node,...node.children.flatMap(descendants)];
  class Element {
    constructor(tag,text='') {this.tagName=tag.toUpperCase();this.children=[];this._text=text;this._value='';this.style={};this.dataset={};this.listeners={};this.hidden=false;this.disabled=false;this.className='';this.scrollTop=0;this.classList={add(){},remove(){},toggle(){}};}
    get textContent(){return this._text+this.children.map(n=>n.textContent).join('');}
    set textContent(value){this._text=String(value);this.children=[];}
    get options(){return this.children.flatMap(n=>n.tagName==='OPTION'?[n]:n.tagName==='OPTGROUP'?n.children:[]);}
    get value(){return this.tagName==='SELECT'&&!this._valueSet?this.options[0]?.value||'':this._value;}
    set value(value){this._value=this.tagName==='SELECT'&&!this.options.some(option=>option.value===String(value))?'':String(value);this._valueSet=true;}
    append(...nodes){for(const value of nodes){const n=typeof value==='string'?new Element('span',value):value;n.parent=this;this.children.push(n);}}
    replaceChildren(...nodes){this._text='';this.children=[];this._value='';this._valueSet=false;this.append(...nodes);}
    replaceWith(node){const parent=this.parent;if(parent){parent.children.splice(parent.children.indexOf(this),1,node);node.parent=parent;}}
    add(node){this.append(node);}
    setAttribute(name,value){this[name]=value;}
    removeAttribute(name){delete this[name];}
    addEventListener(name,fn){this.listeners[name]=fn;}
    focus(){document.activeElement=this;}
    contains(node){return descendants(this).includes(node);}
    setSelectionRange(){}
    getClientRects(){return [1];}
    querySelectorAll(){return [];}
  }
  const document={createElement:tag=>new Element(tag),getElementById:id=>roots.flatMap(descendants).find(n=>n.id===id)||null,activeElement:null};
  for(const id of ['drawer-content','drawer-scroll','drawer-title','task-drawer','agent-rack','view-content','result-count','agent-settings-editor']){const node=new Element('div');node.id=id;roots.push(node);}
  const task={id:'task-1',title:'任务',revision:1,state:'ignored',stage:'backlog',can_assign:true,manual_executor:'codex',workspace_suggested_name:'整理课程资料',...(choice?{workspace_choice:choice}:{})};
  const agents=['codex','claude'].map(name=>({name,enabled:true,capabilities:{model:true,models:[{id:'model-a',efforts:['high']}]},effective_defaults:{model:'model-a'}}));
  const state={tasks:[task],runs,agents,repositories:[{alias:'course'},{alias:'chosen'}],workspace_catalog:{items,warnings:[]},...(workspaceCapability?{workspace_policy:{reuse_strategy:'in_place',managed_root:'D:\\managed'}}:{})};
  const submitted=[],requests=[];
  const source=fs.readFileSync(path.join(__dirname,'../dispatcher/static/app.js'),'utf8');
  const cut=source.indexOf("  $('logout-button').addEventListener");
  const context={document,Option:class extends Element {constructor(text,value){super('option',text);this.value=value;}},console,AbortController,URLSearchParams,Intl,Date,crypto:{randomUUID:()=>String(Math.random())},setTimeout(){},clearTimeout(){}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../dispatcher/static/task-conversation.js'),'utf8'),context);
  vm.runInNewContext(source.slice(0,cut)+`globalThis.ui={renderAssignment,renderDetail,renderRunHistory,renderExecutionSettings,renderDefaults(name){settingsExecutor=name;settingsSignature='';renderAgentSettings();},renderWorkspaces(){view='workspaces';renderRuns();},renderAgentTargets(){endDrag=()=>{};renderAgents();},setState(value){state=value;connected=true;},setDetail(value){detailData=value;},setAction(fn){action=fn;},setApi(fn){api=fn;},selectRun(id){selectedRunId=id;detailSignature='';}};})();`,context);
  const ui=context.ui;
  ui.setState(state);ui.setAction((name,payload)=>submitted.push(JSON.parse(JSON.stringify({name,payload}))));
  ui.setApi(async url=>{requests.push(url);return state.workspace_catalog;});
  const detail={task,runs,events:[]};ui.setDetail(detail);
  const render=()=>ui.renderDetail(detail);
  const get=id=>document.getElementById(id);
  const change=(id,value)=>{const n=get(id);assert.ok(n,`Missing field: ${id}`);n.value=value;n.listeners.change?.({target:n});};
  const input=(id,value)=>{const n=get(id);assert.ok(n,`Missing field: ${id}`);n.value=value;n.listeners.input?.({target:n});};
  const buttons=()=>descendants(get('drawer-content')).filter(n=>n.tagName==='BUTTON');
  const button=label=>buttons().find(n=>n.textContent===label);
  render();
  return {ui,state,task,detail,render,get,change,input,button,submitted,requests,buttonsIn:id=>descendants(get(id)).filter(n=>n.tagName==='BUTTON'),text:()=>get('drawer-content').textContent};
}

const alpha={id:'ws_aaaaaaaaaaaaaaaaaaaaaaaa',label:'课程资料',path:'D:\\course',agents:['codex'],origin:'external',repo_alias:'course',available:true};
const beta={id:'ws_bbbbbbbbbbbbbbbbbbbbbbbb',label:'研究草稿',path:'D:\\research',agents:['claude'],origin:'managed',available:true};
const blocked={id:'ws_cccccccccccccccccccccccc',label:'已移动目录',path:'D:\\missing',agents:['codex'],origin:'external',available:false,reason:'目录不存在'};

test('conversation typing survives polling and submits against the original parent revision',()=>{
  const h=harness();h.task.current_run_id='round1';h.task.fingerprint='fp';h.task.state='awaiting_review';
  h.task.continuation={available:true,ready:true,executor:'codex',action:'follow_up',questions:[],settings:{model:'model-a',effort:'high'}};
  h.detail.runs=[{id:'round1',task_id:h.task.id,round_number:1}];h.detail.input_draft={revision:0,message:'saved',answers:{}};h.render();
  assert.equal(h.get('followup-message').value,'saved');
  h.input('followup-message','预算改为350');h.task.source_checked_at='new timestamp';h.render();
  assert.equal(h.get('followup-message').value,'预算改为350');
  h.button('提交追问并继续').listeners.click();
  const submitted=h.submitted.pop();assert.equal(submitted.name,'follow_up');
  assert.equal(submitted.payload.message,'预算改为350');assert.equal(submitted.payload.run_id,'round1');
  assert.equal(submitted.payload.revision,h.task.revision);assert.ok(submitted.payload.request_id);
});

test('historical round never offers continuation or current round approval',()=>{
  const h=harness();h.task.current_run_id='new';h.task.continuation={available:true,ready:true,action:'follow_up',questions:[],settings:{}};
  h.detail.runs=[{id:'new',round_number:2},{id:'old',round_number:1,review_status:'rejected',review_note:'原复审意见',round_input:{message:'原始补充',answers:{}}}];
  h.ui.selectRun('old');h.render();
  assert.equal(h.get('followup-message'),null);assert.ok(!h.button('提交追问并继续'));
  assert.match(h.text(),/原复审意见/);assert.match(h.text(),/原始补充/);
});

test('full model picker lists alternatives even with an existing task model and keeps custom IDs',()=>{
  const h=harness();h.state.agents[0].capabilities.models.push({id:'model-b',label:'Model B',efforts:['low']});
  h.task.execution_overrides={model:'model-a',effort:'high'};h.render();
  const picker=h.get('assign-model-picker');assert.ok(picker,'Full model picker must be visible');
  assert.ok(picker.options.some(o=>o.value==='model-a'));assert.ok(picker.options.some(o=>o.value==='model-b'));
  h.change('assign-model-picker','model-b');
  assert.equal(h.get('assign-model').value,'model-b');assert.equal(h.get('assign-effort').value,'');
  assert.ok(h.get('assign-effort').options.some(o=>o.value==='low'));
  h.change('assign-effort','low');h.button('确认分配').listeners.click();
  assert.deepEqual(h.submitted.pop().payload.execution_overrides,{model:'model-b',effort:'low'});
  h.input('assign-model','custom-model-id');h.button('确认分配').listeners.click();
  assert.deepEqual(h.submitted.pop().payload.execution_overrides,{model:'custom-model-id'});
});

test('agent defaults offer the complete model list without saving until explicitly requested',async()=>{
  const h=harness();const agent=h.state.agents[0];
  agent.settings={model:'model-a',effort:'high'};agent.settings_revision=3;
  agent.capabilities.models.push({id:'model-b',efforts:['low']});
  h.ui.renderDefaults('codex');const picker=h.get('settings-model-picker');assert.ok(picker);
  assert.ok(picker.options.some(o=>o.value==='model-b'));
  h.change('settings-model-picker','model-b');h.change('settings-effort','low');
  assert.equal(h.submitted.length,0);assert.equal(agent.settings.model,'model-a');
  const save=h.buttonsIn('agent-settings-editor').find(b=>b.textContent==='保存此执行器默认值');
  await save.listeners.click();
  assert.deepEqual(h.submitted.pop(),{name:'configure_agent',payload:{executor:'codex',revision:3,settings:{model:'model-b',effort:'low'}}});
});

test('needs input explanation is visible without an execution failure badge',()=>{
  const h=harness();h.task.state='needs_input';h.task.error='缺少可核验的额度数据。';h.render();
  assert.match(h.text(),/需要输入/);assert.match(h.text(),/缺少可核验的额度数据/);
  assert.doesNotMatch(h.text(),/执行异常/);
  h.task.state='failed';h.task.error='运行进程失败';h.render();assert.match(h.text(),/执行异常/);
});

test('delivery preference survives polling and drag reassignment',()=>{
  const h=harness();h.state.delivery_modes=['auto','answer','files'];h.task.delivery_mode='answer';h.render();
  assert.equal(h.get('assign-delivery').value,'answer');
  h.change('assign-delivery','files');h.task.revision++;h.render();
  assert.equal(h.get('assign-delivery').value,'files');
  h.button('确认分配').listeners.click();
  assert.equal(h.submitted.pop().payload.delivery_mode,'files');
  h.ui.renderAgentTargets();
  const target=h.buttonsIn('agent-rack').find(n=>n.listeners.drop);
  target.listeners.drop({preventDefault(){},dataTransfer:{getData(){return h.task.id;}}});
  assert.equal(h.submitted.pop().payload.delivery_mode,'answer');
});

test('legacy tasks use default; named workspace uses the suggestion and explicit payload',()=>{
  const h=harness();
  assert.equal(h.get('assign-workspace-mode')?.value,'default');
  assert.equal(h.get('assign-workspace-name')?.value,'整理课程资料');
  h.button('确认分配').listeners.click();
  assert.deepEqual(h.submitted.pop().payload.workspace_choice,{mode:'default'});
  h.change('assign-workspace-mode','new');h.input('assign-workspace-name','课程总结');
  h.button('确认分配').listeners.click();
  assert.deepEqual(h.submitted[0].payload.workspace_choice,{mode:'new',name:'课程总结'});
});

test('an older server without workspace policy keeps the original fields and assignment payloads',()=>{
  const h=harness({workspaceCapability:false,items:[alpha],choice:{mode:'existing',workspace_id:alpha.id}});
  assert.equal(h.get('assign-workspace-mode'),null);
  assert.equal(h.get('assign-workspace-name'),null);
  assert.equal(h.get('assign-workspace-existing'),null);
  assert.equal(h.button('刷新目录'),undefined);
  h.input('assign-model','model-a');h.change('assign-effort','high');h.change('assign-repo','chosen');
  h.button('确认分配').listeners.click();
  assert.deepEqual(h.submitted.pop(),{name:'assign',payload:{task_id:h.task.id,revision:h.task.revision,executor:'codex',execution_overrides:{model:'model-a',effort:'high'},repo_alias:'chosen'}});
  h.change('assign-executor','claude');h.task.revision++;h.render();
  assert.equal(h.get('assign-executor').value,'claude');
  assert.equal(h.get('assign-workspace-mode'),null);
  h.ui.renderAgentTargets();
  const target=h.get('agent-rack').children.find(n=>n.dataset.executor==='claude');
  target.listeners.drop({preventDefault(){},dataTransfer:{getData(){return h.task.id;}}});
  assert.deepEqual(h.submitted.pop(),{name:'assign',payload:{task_id:h.task.id,revision:h.task.revision,executor:'claude'}});
  h.input('assign-model','model-a');h.change('assign-effort','high');
  h.state.workspace_policy={reuse_strategy:'in_place',managed_root:'D:\\managed'};h.render();
  assert.equal(h.get('assign-workspace-mode').value,'existing');
  assert.equal(h.get('assign-workspace-existing').value,alpha.id);
  assert.equal(h.get('assign-model').value,'model-a');assert.equal(h.get('assign-effort').value,'high');assert.equal(h.get('assign-repo').value,'chosen');
});

test('existing workspace defaults to task choice, sorts matching agents first, and explains direct changes',()=>{
  const h=harness({choice:{mode:'existing',workspace_id:alpha.id},items:[beta,alpha,blocked]});
  assert.equal(h.get('assign-workspace-existing')?.value,alpha.id);
  const values=h.get('assign-workspace-existing').options.map(n=>n.value);
  assert.ok(values.indexOf(alpha.id)<values.indexOf(beta.id));
  assert.ok(h.get('assign-workspace-existing').options.find(n=>n.value===blocked.id).disabled);
  assert.match(h.text(),/本次任务会直接修改所选目录；日志与验收快照单独保存/);
  assert.match(h.text(),/执行目录：D:\\course/);
  assert.doesNotMatch(h.text(),/已有目录会复制|从已有目录继续（复制）/);
  assert.equal(h.get('assign-repo').value,'');
  h.change('assign-repo','chosen');h.change('assign-workspace-existing',beta.id);
  assert.equal(h.get('assign-repo').value,'chosen');
  h.change('assign-executor','claude');
  const reordered=h.get('assign-workspace-existing').options.map(n=>n.value);
  assert.ok(reordered.indexOf(beta.id)<reordered.indexOf(alpha.id));
  assert.equal(h.get('assign-workspace-existing').value,beta.id);
  assert.equal(h.submitted.length,0);
  h.button('确认分配').listeners.click();
  assert.deepEqual(h.submitted[0].payload.workspace_choice,{mode:'existing',workspace_id:beta.id});
  assert.equal(h.submitted[0].payload.repo_alias,'chosen');
});

test('drag assignment carries the task workspace choice, including explicit legacy default',()=>{
  for(const choice of [undefined,{mode:'existing',workspace_id:alpha.id},{mode:'new',name:'既有草稿'}]){
    const h=harness({choice,items:[alpha]});h.ui.renderAgentTargets();
    const target=h.get('agent-rack').children.find(n=>n.dataset.executor==='claude');
    target.listeners.drop({preventDefault(){},dataTransfer:{getData(){return h.task.id;}}});
    assert.deepEqual(h.submitted[0].payload.workspace_choice,choice||{mode:'default'});
  }
});

test('a newly busy execution directory blocks assignment and preserves all drafts',async()=>{
  const h=harness({items:[alpha],choice:{mode:'existing',workspace_id:alpha.id}});
  h.input('assign-workspace-name','稍后新建名称');h.input('assign-model','model-a');h.change('assign-effort','high');h.change('assign-repo','chosen');
  h.state.workspace_catalog={items:[{...alpha,available:false,reason:'目录正在被另一任务使用'}],warnings:[]};
  await h.button('刷新目录').listeners.click();
  assert.equal(h.get('assign-workspace-existing').value,alpha.id);
  assert.equal(h.get('assign-workspace-name').value,'稍后新建名称');
  assert.equal(h.get('assign-model').value,'model-a');assert.equal(h.get('assign-effort').value,'high');assert.equal(h.get('assign-repo').value,'chosen');
  assert.ok(h.button('确认分配').disabled);assert.match(h.text(),/目录正在被另一任务使用/);
  h.button('确认分配').listeners.click();assert.equal(h.submitted.length,0);
});

test('in-place runs distinguish the execution directory from the frozen acceptance snapshot',()=>{
  const run={id:'old-run',workspace:'D:\\runs\\old-run\\snapshot',executor:'codex',workspace_binding:{mode:'existing',strategy:'in_place',source:{id:alpha.id,path:alpha.path},workspace_path:alpha.path,snapshot_path:'D:\\runs\\old-run\\snapshot'}};
  const h=harness({runs:[run]});
  assert.match(h.text(),/执行目录：D:\\course/);
  assert.match(h.text(),/本次验收快照：D:\\runs\\old-run\\snapshot/);
  assert.match(h.text(),/直接修改所选目录/);
  assert.doesNotMatch(h.text(),/独立副本，原目录保留/);
  assert.ok(h.button('打开验收快照目录'));
  h.button('打开验收快照目录').listeners.click();
  assert.deepEqual(h.submitted.pop(),{name:'open_workspace',payload:{run_id:'old-run'}});
  h.ui.renderWorkspaces();
  const tableButton=h.buttonsIn('view-content').find(n=>n.textContent==='打开验收快照目录');
  assert.ok(tableButton);tableButton.listeners.click();
  assert.deepEqual(h.submitted.pop(),{name:'open_workspace',payload:{run_id:'old-run'}});
});

test('history resolves redirected snapshots before the original frozen binding path',()=>{
  const binding={mode:'existing',strategy:'in_place',workspace_path:alpha.path,snapshot_path:'D:\\original-snapshot'};
  for(const fields of [{workspace:'D:\\effective-snapshot',artifact_root:'D:\\artifact-snapshot'},{artifact_root:'D:\\effective-snapshot'},{workspace:'D:\\effective-snapshot'}]){
    const run={id:'redirected',run_dir:'D:\\run',workspace_binding:binding,...fields};
    const h=harness({runs:[run]});
    assert.match(h.text(),/本次验收快照：D:\\effective-snapshot/);
    assert.doesNotMatch(h.text(),/本次验收快照：D:\\original-snapshot/);
    h.ui.renderWorkspaces();assert.match(h.get('view-content').textContent,/本次验收快照：D:\\effective-snapshot/);
  }
  const fallback=harness({runs:[{id:'fallback',workspace_binding:binding}]});
  assert.match(fallback.text(),/本次验收快照：D:\\original-snapshot/);
});

test('legacy run open-directory labels remain compatible',()=>{
  const h=harness({runs:[{id:'legacy',workspace:'D:\\legacy'}]});
  assert.ok(h.button('打开工作区目录'));
  assert.equal(h.button('打开验收快照目录'),undefined);
  h.ui.renderWorkspaces();assert.ok(h.buttonsIn('view-content').find(n=>n.textContent==='打开目录'));
});

test('failed catalog refresh keeps the selected source and reports the failure',async()=>{
  const h=harness({items:[alpha],choice:{mode:'existing',workspace_id:alpha.id}});
  h.ui.setApi(async()=>{throw new Error('暂时离线');});
  await h.button('刷新目录').listeners.click();
  assert.equal(h.get('assign-workspace-existing').value,alpha.id);
  assert.match(h.text(),/目录刷新失败.*暂时离线/);
  assert.equal(h.button('确认分配').disabled,false);
  assert.equal(h.submitted.length,0);
});

test('polling and mode changes retain workspace, model, effort and repository drafts',()=>{
  const h=harness({items:[alpha,beta]});
  h.change('assign-workspace-mode','new');h.input('assign-workspace-name','未提交的工作区');
  h.change('assign-workspace-mode','existing');h.change('assign-workspace-existing',beta.id);
  h.change('assign-repo','chosen');h.input('assign-model','model-a');h.change('assign-effort','high');
  h.task.revision++;h.render();
  assert.equal(h.get('assign-workspace-mode').value,'existing');
  assert.equal(h.get('assign-workspace-existing').value,beta.id);
  assert.equal(h.get('assign-workspace-name').value,'未提交的工作区');
  assert.equal(h.get('assign-repo').value,'chosen');assert.equal(h.get('assign-model').value,'model-a');assert.equal(h.get('assign-effort').value,'high');
  h.change('assign-workspace-mode','new');assert.equal(h.get('assign-workspace-name').value,'未提交的工作区');
  assert.equal(h.submitted.length,0);
});

test('manual catalog refresh preserves drafts and blocks a source that disappeared',async()=>{
  const h=harness({items:[alpha],choice:{mode:'existing',workspace_id:alpha.id}});
  h.input('assign-workspace-name','保留名称');h.state.workspace_catalog={items:[],warnings:['部分目录读取失败']};
  await h.button('刷新目录').listeners.click();
  assert.deepEqual(h.requests,['/api/workspaces']);
  assert.equal(h.get('assign-workspace-existing').value,alpha.id);
  assert.equal(h.get('assign-workspace-name').value,'保留名称');
  assert.ok(h.button('确认分配').disabled);
  assert.match(h.text(),/部分目录读取失败/);
  assert.equal(h.submitted.length,0);
});

test('unavailable sources and unsafe new names cannot be submitted',()=>{
  const h=harness({items:[blocked],choice:{mode:'existing',workspace_id:blocked.id}});
  assert.match(h.text(),/目录不存在/);assert.ok(h.button('确认分配').disabled);
  h.change('assign-workspace-mode','new');
  for(const name of ['', '../escape', 'NUL', 'COM1.txt', 'name.', ' bad', 'bad\u200bname']){
    h.input('assign-workspace-name',name);assert.ok(h.button('确认分配').disabled,JSON.stringify(name));
  }
  h.input('assign-workspace-name','课程总结');assert.equal(h.button('确认分配').disabled,false);
});

test('historical workspace bindings show recorded source and destination without current inference',()=>{
  const run={id:'old-run',workspace:'D:\\old',executor:'claude',workspace_binding:{mode:'existing',strategy:'copy',source:{id:alpha.id,path:'D:\\source-old'},workspace_path:'D:\\frozen-copy'}};
  const h=harness({runs:[run]});
  assert.match(h.text(),/D:\\source-old/);assert.match(h.text(),/D:\\frozen-copy/);
  h.task.workspace_binding={mode:'existing',strategy:'copy',source:{path:'D:\\current-source'},workspace_path:'D:\\current-copy'};
  delete run.workspace_binding;h.task.revision++;h.render();
  assert.match(h.text(),/未记录工作区来源/);
  assert.doesNotMatch(h.text(),/D:\\current-source/);
});

test('desktop collections group real parent paths and keep equal project names distinguishable',()=>{
  const contest={...alpha,label:'project',path:'D:\\collection\\bi_sai\\project',origin:'external',collection_label:'Codex',relative_path:'bi_sai/project',category:'bi_sai'};
  const course={...beta,label:'project',path:'D:\\collection\\courses\\project',origin:'managed',agents:['codex'],collection_label:'Codex',relative_path:'courses/project',category:'courses'};
  const nested={...alpha,id:'ws_dddddddddddddddddddddddd',path:'D:\\collection\\bi_sai\\alpha\\project',origin:'collection',collection_label:'Codex',relative_path:'bi_sai/alpha/project',category:'bi_sai'};
  const custom={...alpha,id:'ws_eeeeeeeeeeeeeeeeeeeeeeee',agents:[],path:'D:\\custom\\solo',origin:'collection',collection_label:'自定义收藏根（未接执行器）',relative_path:'solo',category:''};
  const h=harness({items:[custom,course,nested,contest],choice:{mode:'existing',workspace_id:contest.id}});
  const groups=h.get('assign-workspace-existing').children.filter(n=>n.tagName==='OPTGROUP');
  assert.deepEqual(groups.map(n=>n.label),['桌面汇总 · Codex / bi_sai','桌面汇总 · Codex / courses','桌面汇总 · 自定义收藏根（未接执行器） / 根目录项目']);
  assert.deepEqual(groups[0].children.map(n=>n.textContent),['alpha/project','project']);
  assert.equal(groups[1].children[0].textContent,'project');
  assert.equal(groups[2].children[0].textContent,'solo');
  assert.match(h.get('assign-workspace-preview').textContent,/桌面汇总 · Codex \/ bi_sai/);
  assert.match(h.get('assign-workspace-preview').textContent,/执行目录：D:\\collection\\bi_sai\\project/);
  assert.match(h.text(),/按桌面汇总目录分类；最近会话与历史记录作为补充/);
  h.change('assign-workspace-existing',course.id);
  assert.match(h.get('assign-workspace-preview').textContent,/执行目录：D:\\collection\\courses\\project/);
  assert.equal(h.submitted.length,0);
});

test('legacy catalog items keep native, managed and repository fallback groups',()=>{
  const repository={...alpha,id:'ws_dddddddddddddddddddddddd',origin:'repository',agents:[],label:'参考仓库',path:'D:\\repo'};
  const unknown={...alpha,id:'ws_eeeeeeeeeeeeeeeeeeeeeeee',origin:'collection',agents:[],label:'缺少分类元数据',path:'D:\\unknown'};
  const h=harness({items:[repository,beta,unknown,alpha],choice:{mode:'existing',workspace_id:alpha.id}});
  const groups=h.get('assign-workspace-existing').children.filter(n=>n.tagName==='OPTGROUP');
  assert.deepEqual(groups.map(n=>n.label),['已有会话补充 · codex','派工台历史 · claude','配置仓库','其他已有目录']);
  assert.ok(groups[0].children.some(n=>n.value===alpha.id));
  assert.equal(h.get('assign-workspace-existing').value,alpha.id);
  h.button('确认分配').listeners.click();
  assert.deepEqual(h.submitted[0].payload.workspace_choice,{mode:'existing',workspace_id:alpha.id});
});

test('agent group reordering and desktop enrichment preserve the saved selection and drafts',async()=>{
  const h=harness({items:[alpha,beta],choice:{mode:'existing',workspace_id:alpha.id}});
  h.input('assign-workspace-name','保持命名草稿');h.change('assign-repo','chosen');
  h.state.workspace_catalog={items:[{...alpha,collection_label:'Codex CLI',category:'研究',relative_path:'研究/课程资料'},
    {...beta,collection_label:'另一个根',category:'courses/term-2',relative_path:'courses/term-2/project'}],warnings:[]};
  await h.button('刷新目录').listeners.click();
  assert.equal(h.get('assign-workspace-existing').value,alpha.id);
  assert.equal(h.get('assign-workspace-name').value,'保持命名草稿');assert.equal(h.get('assign-repo').value,'chosen');
  const groupLabels=()=>h.get('assign-workspace-existing').children.filter(n=>n.tagName==='OPTGROUP').map(n=>n.label);
  assert.equal(groupLabels()[0],'桌面汇总 · Codex CLI / 研究');
  h.change('assign-executor','claude');
  assert.equal(groupLabels()[0],'桌面汇总 · 另一个根 / courses / term-2');
  assert.equal(h.get('assign-workspace-existing').value,alpha.id);
  h.input('assign-model','model-a');h.change('assign-effort','high');h.task.revision++;h.render();
  assert.equal(h.get('assign-workspace-existing').value,alpha.id);
  assert.equal(h.get('assign-workspace-name').value,'保持命名草稿');assert.equal(h.get('assign-repo').value,'chosen');
  assert.equal(h.get('assign-model').value,'model-a');assert.equal(h.get('assign-effort').value,'high');
  assert.equal(h.submitted.length,0);
});

test('desktop root ownership outranks visiting agents while old metadata remains compatible',()=>{
  const codex={...alpha,collection_label:'Codex',collection_agent:'codex',category:'研究',relative_path:'研究/project',agents:['codex','dsh']};
  const dsh={...beta,collection_label:'DSH',collection_agent:'dsh',category:'研究',relative_path:'研究/project',agents:['dsh']};
  const unconnected={...alpha,id:'ws_dddddddddddddddddddddddd',collection_label:'Buddy（未接执行器）',collection_agent:'',category:'研究',relative_path:'研究/notes',agents:['dsh']};
  const legacy={...alpha,id:'ws_eeeeeeeeeeeeeeeeeeeeeeee',collection_label:'旧版数据根',category:'研究',relative_path:'研究/legacy',agents:['dsh']};
  const h=harness({items:[codex,dsh],choice:{mode:'existing',workspace_id:codex.id}});
  h.state.agents.push({...h.state.agents[0],name:'dsh'});h.task.revision++;h.render();h.change('assign-executor','dsh');
  const groupLabels=()=>h.get('assign-workspace-existing').children.filter(n=>n.tagName==='OPTGROUP').map(n=>n.label);
  assert.equal(groupLabels()[0],'桌面汇总 · DSH / 研究');
  h.state.workspace_catalog.items.push(unconnected,legacy);h.render();
  const labels=groupLabels();
  assert.ok(labels.indexOf('桌面汇总 · 旧版数据根 / 研究')<labels.indexOf('桌面汇总 · Codex / 研究'));
  assert.ok(labels.indexOf('桌面汇总 · 旧版数据根 / 研究')<labels.indexOf('桌面汇总 · Buddy（未接执行器） / 研究'));
  assert.equal(h.get('assign-workspace-existing').value,codex.id);
  assert.equal(h.submitted.length,0);
});
