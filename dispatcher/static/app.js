/* Buildless dispatcher UI. Server state is authoritative; actions never move cards optimistically. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const STAGES = [
    ['backlog', '待办', '#969c9d', '等待分配'], ['queued', '排队', '#9b896b', '队列里暂无任务'],
    ['running', '处理中', '#465a70', '当前没有执行中的任务'], ['review', '待复审', '#9b7442', '等待新的交付'],
    ['approved', '审核通过', '#66826d', '人工批准后在此回写'], ['done', '已完成', '#7c8b7e', '完成的任务会留在这里']
  ];
  const VIEWS = {
    board: ['任务看板', '分配任务，跟进执行，在交付前做最后一次确认。', '任务流转'],
    review: ['待我复审', '查看产物与检查结果，确认后再完成滴答任务。', '人工确认'],
    workspaces: ['工作区', '每次运行的文件、产物与记录，都保留在自己的工作区。', '文件与交付'],
    history: ['运行记录', '回看执行过程、复审结论与每一次交付。', '执行档案']
  };
  const REVIEW_NAMES = {approved:'人工审核通过',pending:'待人工复审',rejected:'已退回',legacy:'历史自动验收'};
  let state = null, connected = false, refreshing = false, detailRefreshing = false, view = 'board';
  let query = '', agentFilter = '', secondaryFilter = '', mainSignature = '', agentSignature = '', dragTaskId = null;
  let selectedTaskId = null, selectedRunId = null, detailData = null, detailSignature = '', detailTicket = 0;
  let returnFocus = null, logFetching = false, artifactTicket = 0;
  let settingsExecutor = '', settingsSignature = '';
  let workspaceCatalogRefreshing = false, workspaceCatalogError = '';
  const settingsDrafts = new Map();
  const conversationDrafts = new Map();
  const pending = new Set();
  const el = (tag, cls, text) => { const n=document.createElement(tag); if(cls)n.className=cls; if(text!==undefined&&text!==null)n.textContent=String(text); return n; };
  const append = (node, ...children) => { children.filter(Boolean).forEach(c=>node.append(c)); return node; };
  const button = (text, cls, handler, disabled=false) => { const n=el('button',cls,text); n.type='button'; n.disabled=disabled; if(handler)n.addEventListener('click',handler); return n; };
  const badge = (text, tone='') => el('span',`badge ${tone}`,text);
  const list = (value) => Array.isArray(value)?value:[];
  const taskById = (id) => list(state?.tasks).find(t=>String(t.id)===String(id));
  const runById = (id) => list(state?.runs).find(r=>String(r.id)===String(id));
  const stageName = (stage) => STAGES.find(s=>s[0]===stage)?.[1] || ({ignored:'待办',failed:'执行失败',error:'执行异常',needs_input:'需要输入',revision_requested:'待修改',awaiting_review:'待人工复审',needs_review:'检查未通过',verifying:'自动检查中',writeback_pending:'正在回写滴答',cancelled:'已取消'})[stage] || stage || '未分类';
  const taskStageName = (task) => task.source_completed?'滴答已完成':stageName(task.stage);
  const reviewName = (item) => item.review_status==='legacy'&&item.state!=='done'?'历史运行':REVIEW_NAMES[item.review_status]||stageName(item.state);
  const archiveBlocked = (run) => !run.archived_at&&taskById(run.task_id)?.current_run_id===run.id&&['running','verifying','awaiting_review','approved','writeback_pending'].includes(run.state);
  const stringify = (value) => value==null?'—':typeof value==='object'?JSON.stringify(value,null,2):String(value);
  const shortId = (id) => String(id||'').slice(-8).toUpperCase();
  function dateLabel(value,seconds=false) { if(!value)return '未记录';const numeric=typeof value==='number'||/^\d+(\.\d+)?$/.test(String(value));const timestamp=numeric?Number(value):value;const d=new Date(numeric&&timestamp<1e12?timestamp*1000:timestamp);return Number.isNaN(d.getTime())?String(value):d.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',...(seconds?{second:'2-digit'}:{}),hour12:false}); }
  function sizeLabel(value) { const size=Number(value);if(!Number.isFinite(size))return '大小未记录';return size<1024?`${size} B`:size<1048576?`${(size/1024).toFixed(1)} KB`:`${(size/1048576).toFixed(1)} MB`; }
  function priorityLabel(value) { const labels={0:'无优先级',1:'低优先级',3:'中优先级',5:'高优先级',low:'低优先级',medium:'中优先级',high:'高优先级',urgent:'紧急'};return labels[value]|| (value==null?'无优先级':`优先级 ${value}`); }
  function priorityAppearance(value) {
    const tone=({5:'high',3:'medium',1:'low',high:'high',urgent:'high',medium:'medium',low:'low'})[value]||'none';
    return {tone,color:{high:'#bf5148',medium:'#b68a27',low:'#4b78b3',none:'#8b9299'}[tone]};
  }
  function taskSchedule(t) {
    const zone=t.time_zone||'Asia/Shanghai',hasDate=Boolean(t.start_date||t.due_date);
    let formatter,currentYear;
    try {
      formatter=new Intl.DateTimeFormat('en-GB',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
      currentYear=Number(formatter.formatToParts(new Date()).find(p=>p.type==='year').value);
    } catch {return {date:'',time:hasDate?'日期待确认':'未设日期'};}
    function parse(value) {
      if(typeof value!=='string'||value.length>64)return null;
      const calendar=/^(\d{4})-(\d{2})-(\d{2})(?:$|T)/.exec(value);
      if(!calendar)return null;
      const [,year,month,day]=calendar.map(Number),check=new Date(Date.UTC(year,month-1,day));
      if(year<1000||check.getUTCFullYear()!==year||check.getUTCMonth()!==month-1||check.getUTCDate()!==day)return null;
      let parts={year:String(year),month:String(month),day:String(day)},clock='';
      if(!/^\d{4}-\d{2}-\d{2}$/.test(value)) {
        if(!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:?\d{2})$/i.test(value))return null;
        const date=new Date(value.replace(/([+-]\d{2})(\d{2})$/,'$1:$2'));
        if(Number.isNaN(date.getTime()))return null;
        parts=Object.fromEntries(formatter.formatToParts(date).map(p=>[p.type,p.value]));
        if(!t.is_all_day)clock=`${parts.hour}:${parts.minute}`;
      }
      const label=`${Number(parts.year)!==currentYear?parts.year+'.':''}${Number(parts.month)}.${Number(parts.day)}`;
      return {label,clock,key:`${parts.year}-${String(parts.month).padStart(2,'0')}-${String(parts.day).padStart(2,'0')}`};
    }
    const start=parse(t.start_date),end=parse(t.due_date),first=start||end;
    if(!first)return {date:'',time:hasDate?'日期待确认':'未设日期'};
    const date=start&&end&&start.key!==end.key?`${start.label}–${end.label}`:first.label;
    let time=first.clock;
    if(start&&end&&start.clock&&end.clock&&(start.key!==end.key||start.clock!==end.clock))time=`${start.clock}–${end.clock}`;
    else if(!start&&end.clock)time=`截止 ${end.clock}`;
    return {date,time,sortDate:first.key,sortTime:first.clock};
  }
  function sortBoardTasks(tasks) {
    // Display order only: keep the server's task array and execution queue unchanged.
    return tasks.map((task,index)=>({task,index,schedule:taskSchedule(task),priority:({high:3,medium:2,low:1})[priorityAppearance(task.priority).tone]||0}))
      .sort((a,b)=>(b.schedule.sortDate||'').localeCompare(a.schedule.sortDate||'')||b.priority-a.priority||(b.schedule.sortTime||'').localeCompare(a.schedule.sortTime||'')||a.index-b.index)
      .map(item=>item.task);
  }
  function difficultyLabel(value) { const labels={easy:'简单',medium:'中等',hard:'复杂',low:'简单',high:'复杂'};return labels[value]||stringify(value); }
  function autoReviewLabel(value) { const labels={passed:'自动检查通过',pass:'自动检查通过',success:'自动检查通过',approved:'自动检查通过',failed:'自动检查未通过',fail:'自动检查未通过',rejected:'自动检查未通过',pending:'等待自动检查',running:'正在自动检查',not_run:'尚未检查',skipped:'未执行自动检查'};return labels[value]||(value?String(value):'暂无自动检查结果'); }
  function hasError(t) { return !needsInput(t)&&(Boolean(t.error)||['failed','error','blocked'].includes(t.state)); }
  function needsInput(t) { return /needs?[_ -]?input|awaiting[_ -]?input/.test(t.state||''); }
  function isPending(t) { return pending.has(`task:${t.id}`); }
  const canOpenWorkspace=()=>state?.client?.can_open_workspace!==false;
  const supportsWorkspaceChoice=()=>Boolean(state?.workspace_policy);
  const workspaceOpenLabel=(run,compact=false)=>run.workspace_binding?.strategy==='in_place'?'打开验收快照目录':compact?'打开目录':'打开工作区目录';
  function canAssign(t) { return connected&&t?.can_assign===true&&!isPending(t); }
  async function api(path, options={}) {
    const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),15000);
    try { const response=await fetch(path,{cache:'no-store',...options,signal:controller.signal});let data;try{data=await response.json();}catch{throw new Error(`服务器返回了无法读取的响应（${response.status}）`);}if(response.status===401&&data.login_url==='/auth/login')location.assign('/auth/login');if(!response.ok)throw new Error(data.error||data.message||`请求失败（${response.status}）`);return data; }
    catch(error){if(error.name==='AbortError')throw new Error('连接超时，请稍后重试');throw error;}finally{clearTimeout(timer);}
  }
  function toast(message, error=false) {const n=el('div',`toast${error?' error':''}`);append(n,el('span','',message),button('×','',()=>n.remove()));$('toast-region').append(n);setTimeout(()=>n.remove(),error?11000:6500);}
  function actionKey(action,payload) {return payload.task_id?`task:${payload.task_id}`:payload.run_id?`run:${payload.run_id}`:payload.executor?`agent:${payload.executor}`:action;}
  async function action(actionName,payload={},message='操作已完成') {
    if(!connected||!state?.csrf_token){toast('尚未连接调度器，请等待连接恢复。',true);return false;}
    const key=actionKey(actionName,payload);if(pending.has(key))return false;pending.add(key);invalidate();
    try {
      const command=await api('/api/actions',{method:'POST',headers:{'Content-Type':'application/json','X-Dispatcher-Token':state.csrf_token},body:JSON.stringify({action:actionName,request_id:crypto.randomUUID(),...payload})});
      if(!command.id)throw new Error('服务器未返回操作编号');
      let result=command;
      for(let attempt=0;attempt<400;attempt++) {
        if(result.status==='done')break;
        if(result.status==='failed')throw new Error(stringify(result.error||'操作失败'));
        await new Promise(resolve=>setTimeout(resolve,750));
        result=await api(`/api/commands/${encodeURIComponent(command.id)}`);
      }
      if(result.status==='failed')throw new Error(stringify(result.error||'操作失败'));
      if(result.status!=='done')throw new Error('操作已提交，但仍未确认完成。请查看任务状态后再决定是否重试。');
      toast(message);await refresh();if(selectedTaskId)await refreshDetail();return true;
    } catch(error){toast(error.message,true);return false;} finally{pending.delete(key);invalidate();}
  }
  function invalidate() {mainSignature='';agentSignature='';detailSignature='';render();if(detailData)renderDetail(detailData);}
  async function refresh() {
    if(refreshing)return;refreshing=true;
    try {const incoming=await api('/api/state');if(!incoming||!Array.isArray(incoming.tasks)||!Array.isArray(incoming.agents))throw new Error('调度器返回的状态不完整');state=incoming;connected=true;$('connection-error').hidden=true;render();if(selectedTaskId)refreshDetail();}
    catch(error){connected=false;$('connection-error-text').textContent=`无法连接调度器：${error.message}${state?'。当前显示上次读取的状态。':''}`;$('connection-error').hidden=false;render();if(detailData)renderDetail(detailData);}
    finally{refreshing=false;}
  }
  function render() {
    $('remote-note').hidden=state?.client?.mode!=='remote';
    $('logout-button').hidden=!(state?.client?.mode==='remote'&&state?.system?.remote_access?.mode==='password_proxy');
    const system=state?.system||{};$('system-status').textContent=!connected?(state?'连接已中断':'正在连接调度器'):system.running?'调度器运行中':'调度器已停止';
    $('system-dot').className=`status-dot ${connected&&system.running?'good':state&&!connected?'bad':''}`;
    const syncWarnings=list(system.sync_warnings),syncing=system.syncing||pending.has('sync');
    $('last-sync').textContent=(system.last_sync?`最近一轮滴答同步于 ${dateLabel(system.last_sync,true)}`:'尚未同步滴答')+(syncWarnings.length?' · 部分状态待确认':'');
    $('sync-button').disabled=!connected||syncing;$('sync-button').textContent=syncing?'正在同步…':'↻ 同步滴答';
    const pollSeconds=Number(system.poll_seconds),duration=system.last_sync_duration_seconds;
    $('sync-cadence').textContent=`界面每 2 秒刷新 · 滴答每 ${Number.isFinite(pollSeconds)&&pollSeconds>0?pollSeconds:20} 秒同步${typeof duration==='number'?` · 上轮耗时 ${duration.toFixed(1)} 秒`:''}`;
    const notices=[...(system.last_error?[`调度提示：${stringify(system.last_error)}`]:[]),...syncWarnings.map(stringify)];$('system-error').hidden=!notices.length;$('system-error').textContent=notices.join(' ');
    const reviewCount=list(state?.tasks).filter(t=>t.stage==='review').length;$('review-count').hidden=!reviewCount;$('review-count').textContent=reviewCount;
    renderAgents();renderAgentSettings();renderFilters();renderMain();
  }
  function renderAgents() {
    if(!state||dragTaskId)return;const signature=JSON.stringify([state.agents,connected,Array.from(pending)]);if(signature===agentSignature)return;agentSignature=signature;
    const rack=$('agent-rack');rack.replaceChildren();
    const targets=[{name:'auto',label:'自动分配',enabled:true},...list(state.agents).map(a=>({...a,label:a.name}))];
    for(const a of targets) {
      const n=button('',`agent-target${a.name==='auto'?' auto':''}`,()=>{toast('打开一张任务卡片即可选择执行器，也可以把可分配任务拖到这里。');},!connected||!a.enabled);
      n.dataset.executor=a.name;n.title=a.enabled?'拖入可分配任务':agentStatus(a);
      append(n,append(el('div','agent-head'),el('span','agent-name',a.label),el('span','agent-monogram',a.name==='auto'?'A↗':String(a.label).slice(0,2).toUpperCase())));
      if(a.name==='auto'){append(n,el('span','agent-status','由调度规则选择执行器'),el('span','agent-status','拖入任务进行分配'));}
      else {const detail=el('div','agent-detail');append(detail,append(el('span'),el('strong','',typeof a.busy==='number'?a.busy:a.busy?1:0),' 处理中'),append(el('span'),el('strong','',Number(a.queued)||0),' 排队'));append(n,detail,el('span','agent-status',agentStatus(a)));if(a.effective_defaults)n.append(el('span','agent-model-summary',a.capabilities?.model?`当前默认：${settingsLabel(a.effective_defaults)} · 可选 ${list(a.capabilities.models).length} 个模型`:settingsLabel(a.effective_defaults)));}
      n.addEventListener('dragover',e=>{const t=taskById(dragTaskId);if(a.enabled&&canAssign(t)){e.preventDefault();e.dataTransfer.dropEffect='move';n.classList.add('drop-ready');}});
      n.addEventListener('dragleave',()=>n.classList.remove('drop-ready'));
      n.addEventListener('drop',e=>{e.preventDefault();n.classList.remove('drop-ready');const id=dragTaskId||e.dataTransfer.getData('text/plain');const t=taskById(id);endDrag();if(!canAssign(t)){toast('这个任务目前不可重新分配，请查看最新状态。',true);return;}action('assign',{task_id:t.id,revision:t.revision,executor:a.name,...(Array.isArray(state?.delivery_modes)?{delivery_mode:t.delivery_mode||'auto'}:{}),...(supportsWorkspaceChoice()?{workspace_choice:t.workspace_choice||{mode:'default'}}:{})},a.name==='auto'?'任务已交给自动分配':'任务已分配给 '+a.label);});
      rack.append(n);
    }
  }
  function agentStatus(a) {const names={idle:'可接收任务',busy:'正在处理',running:'正在处理',disabled:'未启用',unavailable:'暂不可用',error:'执行器异常',offline:'未连接'};return names[a.status]|| (a.status?String(a.status):!a.enabled?'未启用':a.busy?'正在处理':'可接收任务');}
  const agentByName=(name)=>list(state?.agents).find(a=>a.name===name);
  const EFFORT_NAMES={minimal:'最少',low:'低',medium:'中',high:'高',xhigh:'很高',max:'最高',ultra:'极高',off:'关闭',on:'开启',auto:'自动',none:'无'};
  const effortLabel=(value)=>value?`${EFFORT_NAMES[value]||value} (${value})`:'默认强度';
  function settingsLabel(settings) {if(settings?.sources?.model==='native_session')return '客户端会话的模型与强度';return `${settings?.model||'默认模型'} · ${settings?.sources?.effort==='difficulty'?'按任务难度':settings?.effort?effortLabel(settings.effort):'默认强度'}`;}
  function modelCapabilities(agent,model) {return list(agent?.capabilities?.models).find(item=>item.id===model);}
  function availableEfforts(agent,model) {const selected=String(model||'').trim()||agent?.effective_defaults?.model;return list(modelCapabilities(agent,selected)?.efforts);}
  function modelPicker(agent,input,id,onChange) {
    const select=el('select'),models=list(agent?.capabilities?.models);select.id=id;select.className='model-picker';
    select.setAttribute('aria-label','完整模型列表');select.title='完整列表不按已填写的模型 ID 筛选；也可以直接填写自定义 ID。';
    const placeholder=new Option(`选择模型（全部 ${models.length} 个）`,'');placeholder.disabled=true;select.add(placeholder);
    select.add(new Option('沿用默认模型','__inherit__'));
    for(const model of models)select.add(new Option(model.label&&model.label!==model.id?`${model.label} · ${model.id}`:model.id,model.id));
    select.value='';select.disabled=input.disabled||!models.length;
    select.addEventListener('change',()=>{if(!select.value)return;input.value=select.value==='__inherit__'?'':select.value;onChange();select.value='';});
    return select;
  }
  function populateEfforts(select,agent,model,value,disabled=false) {
    const choices=availableEfforts(agent,model);select.replaceChildren(new Option('沿用默认强度',''));for(const effort of choices)select.add(new Option(effortLabel(effort),effort));
    const valid=!value||choices.includes(value);if(value&&!valid){const o=new Option(`${effortLabel(value)} · 请重新选择`,value);o.disabled=true;select.add(o);}select.value=value||'';select.disabled=disabled||(!choices.length&&!value);return valid;
  }
  function modelHelp(agent,model) {if(!agent)return '选择具体执行器后，可为这一次派工覆盖配置。';const effective=String(model||'').trim()||agent.effective_defaults?.model,capability=modelCapabilities(agent,effective);if(effective&&!capability)return '自定义模型 ID：可保存模型；其强度能力尚未确认，请沿用默认强度。';if(capability&&!list(capability.efforts).length)return '此模型没有已确认的可调强度，沿用厂商默认。';return agent.capabilities?.note||'留空继承默认设置；单次派工的覆盖值不会修改其他会话。';}
  function collectSettings(model,effort) {const value={};if(model.trim())value.model=model.trim();if(effort)value.effort=effort;return value;}
  function settingsDraft(agent) {
    let draft=settingsDrafts.get(agent.name);
    if(!draft||(!draft.dirty&&!pending.has(`agent:${agent.name}`)&&draft.revision!==agent.settings_revision)){
      draft={model:agent.settings?.model||'',effort:agent.settings?.effort||'',revision:agent.settings_revision,dirty:false};settingsDrafts.set(agent.name,draft);
    }
    return draft;
  }
  function renderAgentSettings() {
    const container=$('agent-settings-editor'),agents=list(state?.agents);if(!container||!agents.length)return;
    if(!agents.some(a=>a.name===settingsExecutor))settingsExecutor=agents[0].name;
    const agent=agentByName(settingsExecutor),draft=settingsDraft(agent),busy=pending.has(`agent:${agent.name}`);
    const signature=JSON.stringify([settingsExecutor,agents,connected,busy]);if(settingsSignature===signature)return;settingsSignature=signature;
    const focused=container.contains(document.activeElement)?document.activeElement.id:null,selection=focused?[document.activeElement.selectionStart,document.activeElement.selectionEnd]:null;container.replaceChildren();
    const grid=el('div','agent-settings-grid'),executor=el('select'),model=el('input'),effort=el('select');
    for(const a of agents)executor.add(new Option(a.name+(a.enabled?'':'（未启用）'),a.name));executor.value=agent.name;executor.disabled=busy;
    model.type='text';model.autocomplete='off';model.maxLength=128;model.placeholder='填写模型 ID，或从下方完整列表选择';model.value=draft.model;model.disabled=!connected||busy||agent.capabilities?.model!==true;
    let validEffort=populateEfforts(effort,agent,model.value,draft.effort,!connected||busy);
    const modelField=append(el('div','field'),field('默认模型','settings-model',model));modelField.append(modelPicker(agent,model,'settings-model-picker',modelChanged));
    append(grid,field('执行器默认配置','settings-executor',executor),modelField,field('默认思考强度','settings-effort',effort));
    const help=el('p','field-help',modelHelp(agent,model.value));help.id='settings-model-help';model.setAttribute('aria-describedby',help.id);effort.setAttribute('aria-describedby',help.id);
    const stale=draft.revision!==agent.settings_revision;
    const status=el('p','settings-draft-status',stale?'默认值已在其他页面更新；你的编辑已保留。请先加载已保存值再修改。':draft.dirty?'有未保存的修改':'默认值已保存');status.setAttribute('role','status');
    const save=button(busy?'保存中…':'保存此执行器默认值','button secondary',async()=>{
      if(!validEffort){toast('所选模型不支持当前强度，请重新选择。',true);return;}
      const settings=collectSettings(draft.model,draft.effort);
      const saved=await action('configure_agent',{executor:agent.name,revision:draft.revision,settings},`${agent.name} 默认配置已保存，仅影响后续启动的任务`);
      if(saved){settingsDrafts.delete(agent.name);settingsSignature='';renderAgentSettings();}
    },!connected||busy||stale||!validEffort||typeof draft.revision!=='number');
    const reload=button('加载已保存值','text-button',()=>{settingsDrafts.delete(agent.name);settingsSignature='';renderAgentSettings();},busy);
    function edited(){draft.dirty=true;status.textContent=stale?'默认值已在其他页面更新；你的编辑已保留。请先加载已保存值再修改。':'有未保存的修改';save.disabled=!connected||busy||stale||!validEffort||typeof draft.revision!=='number';}
    function modelChanged(){draft.model=model.value;const allowed=availableEfforts(agent,model.value.trim());if(draft.effort&&!allowed.includes(draft.effort))draft.effort='';validEffort=populateEfforts(effort,agent,model.value.trim(),draft.effort,!connected||busy);help.textContent=modelHelp(agent,model.value.trim());edited();}
    model.addEventListener('input',modelChanged);
    effort.addEventListener('change',()=>{draft.effort=effort.value;validEffort=true;edited();});
    executor.addEventListener('change',()=>{settingsExecutor=executor.value;settingsSignature='';renderAgentSettings();});
    append(container,grid,help,el('p','field-help',`当前生效：${settingsLabel(agent.effective_defaults)}。留空后沿用项目或厂商默认。`),append(el('div','settings-actions'),save,reload,status),el('p','field-help','只影响之后启动的任务，正在运行的会话与独立验收配置保持原样。这里只保存配置，不会派发任务。'));
    if(focused&&$(focused)){$(focused).focus({preventScroll:true});if(selection&&typeof selection[0]==='number')$(focused).setSelectionRange(...selection);}
  }
  function renderFilters() {
    const select=$('agent-filter');const signature=JSON.stringify(list(state?.agents).map(a=>a.name));if(select.dataset.signature===signature)return;select.dataset.signature=signature;
    select.replaceChildren(new Option('全部执行器',''));for(const a of list(state?.agents))select.add(new Option(a.name,a.name));select.value=agentFilter;
  }
  function setView(next) {
    view=VIEWS[next]?next:'board';secondaryFilter=view==='review'?'pending':view==='workspaces'?'active':'';mainSignature='';
    document.querySelectorAll('[data-view]').forEach(n=>{const active=n.dataset.view===view;n.classList.toggle('active',active);if(active)n.setAttribute('aria-current','page');else n.removeAttribute('aria-current');});
    const [title,description,label]=VIEWS[view];$('page-title').textContent=title;$('breadcrumb-current').textContent=title;$('page-description').textContent=description;$('page-label').textContent=label;document.title=`${title} · AuToDo`;$('agents-section').hidden=view!=='board';
    const secondary=$('secondary-filter');secondary.replaceChildren();let options=[];
    if(view==='board')options=[['','全部任务'],['attention','需我处理']];
    if(view==='review')options=[['pending','待我复审'],['approved','人工审核通过'],['legacy','历史运行'],['all','全部复审记录']];
    if(view==='workspaces')options=[['active','未归档'],['archived','已归档'],['all','全部工作区']];
    if(view==='history')options=[['','全部记录'],['approved','人工审核通过'],['legacy','历史运行'],['rejected','已退回']];
    options.forEach(([value,label])=>secondary.add(new Option(label,value)));secondary.value=secondaryFilter;$('secondary-filter-label').hidden=!options.length;renderMain();
  }
  function matches(item) {return (!agentFilter||item.executor===agentFilter)&&(!query||[item.title,item.description,item.id,item.task_id,item.workspace,item.run_dir,item.executor,item.repo_alias,taskSchedule(item).date].some(x=>String(x||'').toLocaleLowerCase().includes(query)));}
  function renderMain() {
    if(dragTaskId)return;
    const sig=JSON.stringify([view,query,agentFilter,secondaryFilter,state?.tasks,state?.runs,connected,Array.from(pending)]);if(sig===mainSignature)return;mainSignature=sig;
    const content=$('view-content');const focused=content.contains(document.activeElement)?document.activeElement.dataset.focusKey:null;const scroll=content.querySelector('.board-scroll')?.scrollLeft||0;content.replaceChildren();content.setAttribute('aria-busy',String(!state));
    if(!state){$('result-count').textContent='等待连接';renderBoard([],true);return;}
    if(view==='board'){const tasks=list(state.tasks).filter(matches).filter(t=>secondaryFilter!=='attention'||['needs_input','needs_review','failed','revision_requested','awaiting_review'].includes(t.state));$('result-count').textContent=`${tasks.length} 项任务`;renderBoard(tasks);}
    else if(view==='review')renderReview();
    else renderRuns();
    const board=content.querySelector('.board-scroll');if(board)board.scrollLeft=scroll;if(focused){const target=Array.from(content.querySelectorAll('[data-focus-key]')).find(n=>n.dataset.focusKey===focused);target?.focus({preventScroll:true});}
  }
  function renderBoard(tasks,loading=false) {
    const wrap=el('div','board-scroll'),board=el('div','board');
    for(const [key,label,color,emptyText] of STAGES){const column=el('section','board-column');column.style.setProperty('--column-color',color);column.setAttribute('aria-label',label);const items=sortBoardTasks(tasks.filter(t=>t.stage===key));append(column,append(el('header','column-header'),el('span','column-dot'),el('h2','',label),el('span','column-count',loading?'—':items.length)));const cards=el('div','column-list');if(loading)cards.append(el('div','skeleton'));else if(!items.length)cards.append(append(el('div','column-empty'),el('span','',key==='done'?'✓':'·'),query||agentFilter?'没有符合筛选的任务':emptyText));else items.forEach(t=>cards.append(taskCard(t)));append(column,cards);board.append(column);}
    append(wrap,board);$('view-content').append(wrap);
    const hint=el('p','board-hint',!loading&&!list(state?.tasks).length?'还没有任务。点击「同步滴答」读取任务；同步结果会显示在这里。':'列位置由实际执行进度决定。拖拽用于选择执行器，不会改变验收结论。');$('view-content').append(hint);
  }
  function taskCard(t) {
    const schedule=taskSchedule(t),appearance=priorityAppearance(t.priority),title=t.title||'未命名任务';
    const displayTitle=(schedule.date?schedule.date+' ':'')+title;
    const n=button('','task-card',()=>openTask(t.id));n.dataset.focusKey=`task-${t.id}`;n.style.setProperty('--task-color',appearance.color);n.setAttribute('aria-label',`${displayTitle}，${taskStageName(t)}，${t.executor||(t.source_completed?'未派工':'未分配')}`);n.draggable=canAssign(t);
    const heading=el('h3');if(schedule.date)heading.append(el('span','task-date',schedule.date+' '));heading.append(el('span','task-title',title));
    append(n,append(el('div','card-heading'),heading,n.draggable?el('span','drag-grip','⠿'):null));
    if(schedule.time)n.append(el('p','card-schedule',schedule.time));
    if(t.description)n.append(el('p','card-description',t.description));const badges=el('div','badge-row');
    if(t.priority!==undefined&&t.priority!==null&&t.priority!==0)badges.append(badge(priorityLabel(t.priority),'priority-'+appearance.tone));
    if(t.difficulty)badges.append(badge(difficultyLabel(t.difficulty)));
    if(needsInput(t))badges.append(badge('需要输入','amber'));else if(hasError(t))badges.append(badge('执行异常','red'));
    if(t.source_completed)badges.append(badge('滴答已完成','blue'));
    else if(t.review_status==='legacy')badges.append(badge(reviewName(t)));
    else if(t.review_status==='approved')badges.append(badge('人工审核通过','green'));
    else if(t.stage==='review')badges.append(badge(autoReviewLabel(t.auto_review_status),/pass|success|approved/.test(t.auto_review_status||'')?'green':'amber'));
    if(isPending(t))badges.append(badge('操作处理中','amber'));if(badges.childNodes.length)n.append(badges);
    append(n,append(el('div','card-footer'),el('span','card-executor',t.executor||(t.source_completed?'未派工':t.recommended_executor?`建议 ${t.recommended_executor}`:'待分配')),el('span','card-arrow','↗')));
    n.addEventListener('dragstart',e=>{if(!canAssign(taskById(t.id))){e.preventDefault();return;}dragTaskId=String(t.id);e.dataTransfer.effectAllowed='move';e.dataTransfer.setData('text/plain',String(t.id));n.classList.add('dragging');document.body.classList.add('is-dragging');});n.addEventListener('dragend',endDrag);return n;
  }
  function endDrag(){dragTaskId=null;document.body.classList.remove('is-dragging');document.querySelectorAll('.dragging,.drop-ready').forEach(n=>n.classList.remove('dragging','drop-ready'));render();}
  function empty(title,description){return append(el('div','empty-state'),el('span','empty-symbol','▱'),el('h2','',title),el('p','',description));}
  function renderReview() {
    let entries=[];
    if(['pending','all'].includes(secondaryFilter))entries.push(...list(state.tasks).filter(t=>t.stage==='review').map(t=>({task:t,title:t.title,executor:t.executor,review_status:t.review_status||'pending',...t})));
    if(secondaryFilter!=='pending')entries.push(...list(state.runs).filter(r=>secondaryFilter==='all'?['approved','legacy','rejected'].includes(r.review_status):r.review_status===secondaryFilter).map(r=>({...r,run:r})));
    entries=entries.filter(matches);$('result-count').textContent=`${entries.length} 项记录`;
    if(!entries.length){$('view-content').append(empty(secondaryFilter==='pending'?'目前没有待复审任务':'还没有匹配的复审记录',secondaryFilter==='pending'?'执行与自动检查结束后，交付会在这里等你确认。':'已通过的人工复审会保留记录；历史运行仅在完成后标为历史自动验收。'));return;}
    const container=el('div','review-list');for(const entry of entries){const row=el('article','review-row'),copy=el('div');append(copy,el('h3','',entry.title||'未命名任务'));const meta=el('div','review-meta');append(meta,badge(entry.task?'待人工复审':reviewName(entry),entry.review_status==='approved'?'green':entry.task?'amber':''),el('span','',entry.executor||'未分配'),entry.created_at?el('span','',dateLabel(entry.created_at)):null);copy.append(meta);if(entry.review_note)copy.append(el('p','review-note',entry.review_note));else if(entry.task)copy.append(el('p','review-note',autoReviewLabel(entry.auto_review_status)));append(row,copy,button(entry.task?'查看交付并复审':'查看记录','button '+(entry.task?'primary':'secondary'),()=>entry.task?openTask(entry.id):openRun(entry.run)));container.append(row);}$('view-content').append(container);
  }
  function renderRuns() {
    let runs=list(state.runs).filter(matches);
    if(view==='workspaces'){runs=runs.filter(r=>r.workspace||r.run_dir);if(secondaryFilter==='active')runs=runs.filter(r=>!r.archived_at);if(secondaryFilter==='archived')runs=runs.filter(r=>r.archived_at);}
    else if(secondaryFilter)runs=runs.filter(r=>r.review_status===secondaryFilter);
    runs.sort((a,b)=>String(b.created_at||'').localeCompare(String(a.created_at||'')));$('result-count').textContent=`${runs.length} 项${view==='workspaces'?'工作区':'记录'}`;
    if(!runs.length){$('view-content').append(empty(view==='workspaces'?'这里还没有工作区':'这里还没有运行记录',query||agentFilter?'试试其他关键词或执行器筛选。':'任务开始执行后，会自动建立运行记录和工作区。'));return;}
    const wrap=el('div','table-wrap'),table=el('table'),head=el('thead'),headRow=el('tr');for(const label of [view==='workspaces'?'任务与工作区':'任务 / 运行编号','执行器','状态','创建时间','操作'])headRow.append(el('th','',label));append(head,headRow);table.append(head);const body=el('tbody');
    for(const r of runs){const row=el('tr'),name=el('td');const link=button(r.title||r.task_id||'未命名任务','table-link',()=>openRun(r));link.dataset.focusKey=`run-${r.id}`;append(name,link,el('p','table-subtitle mono',view==='workspaces'&&!r.workspace_binding?r.workspace||r.run_dir:`#${r.id}`));if(r.workspace_binding)name.append(workspaceRecord(r.workspace_binding,r.workspace,r.artifact_root));const status=el('td');status.append(badge(reviewName(r),r.review_status==='approved'?'green':r.state==='failed'?'red':''));if(r.archived_at)status.append(el('p','table-subtitle','已归档'));const actions=el('div','table-actions');if(view==='workspaces'){append(actions,canOpenWorkspace()?button(workspaceOpenLabel(r,true),'text-button',()=>action('open_workspace',{run_id:r.id},`已请求${workspaceOpenLabel(r)}`),!connected||pending.has(`run:${r.id}`)):null,button(r.archived_at?'恢复':'归档','text-button',()=>action(r.archived_at?'restore':'archive',{run_id:r.id},r.archived_at?'工作区已恢复':'工作区已归档'),!connected||pending.has(`run:${r.id}`)||archiveBlocked(r)));}else actions.append(button('查看详情','text-button',()=>openRun(r)));append(row,name,el('td','',r.executor||'—'),status,el('td','table-date',dateLabel(r.created_at)),append(el('td'),actions));body.append(row);}
    append(table,body);append(wrap,table);$('view-content').append(wrap);
  }
  function showDrawer() {returnFocus=document.activeElement;$('task-drawer').hidden=false;$('drawer-backdrop').hidden=false;document.body.classList.add('drawer-open');$('app-shell').inert=true;$('drawer-close').focus();}
  async function openTask(id,runId=null) {
    const alreadyOpen=!$('task-drawer').hidden;selectedTaskId=String(id);selectedRunId=runId;detailData=null;detailSignature='';artifactTicket++;detailTicket++;
    $('drawer-title').textContent=taskById(id)?.title||'正在读取任务…';$('drawer-content').replaceChildren(el('div','loading-line','正在读取任务详情…'));$('drawer-scroll').scrollTop=0;if(!alreadyOpen)showDrawer();await refreshDetail();
  }
  function openRun(run){if(taskById(run.task_id)){openTask(run.task_id,run.id);return;}const alreadyOpen=!$('task-drawer').hidden;selectedTaskId=null;selectedRunId=run.id;detailTicket++;artifactTicket++;detailData={task:null,runs:[run],events:[]};detailSignature='';$('drawer-title').textContent=run.title||'运行记录';$('drawer-scroll').scrollTop=0;if(!alreadyOpen)showDrawer();renderDetail(detailData);}
  function closeDrawer() {selectedTaskId=null;selectedRunId=null;detailData=null;detailSignature='';detailTicket++;artifactTicket++;$('task-drawer').hidden=true;$('drawer-backdrop').hidden=true;document.body.classList.remove('drawer-open');$('app-shell').inert=false;if(returnFocus?.isConnected)returnFocus.focus();else document.querySelector('.nav-link.active')?.focus();}
  async function refreshDetail() {
    if(!selectedTaskId||detailRefreshing)return;detailRefreshing=true;const id=selectedTaskId,ticket=detailTicket,requestedRun=selectedRunId;
    try {const data=await api(`/api/task?id=${encodeURIComponent(id)}${selectedRunId?'&run_id='+encodeURIComponent(selectedRunId):''}`);if(ticket!==detailTicket||id!==selectedTaskId||requestedRun!==selectedRunId)return;detailData=data;renderDetail(data);if($('log-details')?.open)loadLog(true);}
    catch(error){if(ticket===detailTicket&&!detailData)$('drawer-content').replaceChildren(append(el('div','detail-section'),el('p','inline-empty',`读取失败：${error.message}`),button('重新读取','button secondary',refreshDetail)));}
    finally{detailRefreshing=false;}
  }
  function section(title,id) {const n=el('section','detail-section');if(id)n.id=id;if(title)n.append(el('h3','',title));return n;}
  function renderDetail(data) {
    if($('task-drawer').hidden)return;
    const t=data.task;const runs=list(data.runs);const chosen=runs.find(r=>String(r.id)===String(selectedRunId))||runs.find(r=>String(r.id)===String(t?.current_run_id))||runs[0];
    const signature=JSON.stringify([t,runs,selectedRunId,data.input_draft,state?.agents,state?.repositories,state?.workspace_catalog,state?.workspace_policy,workspaceCatalogRefreshing,workspaceCatalogError,connected,Array.from(pending)]);
    if(signature!==detailSignature){
      const old={permissionProfile:$('assign-permission')?.value,delivery:$('assign-delivery')?.value,executor:$('assign-executor')?.value,repo:$('assign-repo')?.value,model:$('assign-model')?.value,effort:$('assign-effort')?.value,workspaceMode:$('assign-workspace-mode')?.value,workspaceName:$('assign-workspace-name')?.value,workspaceId:$('assign-workspace-existing')?.value,note:$('review-note')?.value,logOpen:$('log-details')?.open,historyOpen:$('history-details')?.open,logRun:$('log-run')?.value,phase:$('log-phase')?.value,stream:$('log-stream')?.value,logText:$('log-text')?.textContent,scroll:$('drawer-scroll').scrollTop};
      const focused=document.activeElement?.id,selection=[document.activeElement?.selectionStart,document.activeElement?.selectionEnd];detailSignature=signature;$('drawer-content').replaceChildren();$('drawer-title').textContent=t?.title||chosen?.title||'运行记录';
      const historical=Boolean(chosen&&t&&String(chosen.id)!==String(t.current_run_id));
      const delivery=chosen?.delivery||(!historical?t?.delivery:null);
      if(delivery&&globalThis.DispatcherResultCard){
        const settings=chosen?.execution_settings||(!historical?t?.execution_settings:null)||{};
        $('drawer-content').append(DispatcherResultCard.render(delivery,{historical,round:chosen?.round_number,executor:chosen?.executor||t?.executor,model:settings.model,effort:settings.effort,time:chosen?.created_at?dateLabel(chosen.created_at,true):''}));
      }
      if(chosen&&globalThis.TaskConversation){const input=TaskConversation.roundInput(chosen);if(input)$('drawer-content').append(input);}
      if(t){renderTaskConversation(t,data,historical);if(delivery){renderReviewControls(t,old);renderTaskSummary(t);renderAssignment(t,old);}else{renderTaskSummary(t);renderAssignment(t,old);renderReviewControls(t,old);}}
      if(chosen||t?.workspace_binding)renderWorkspaceBinding(t,chosen);
      if(chosen)renderExecutionSettings(t,chosen);
      if(chosen&&delivery?.delivery_type!=='answer')renderArtifacts(chosen);else if(!chosen&&!delivery)$('drawer-content').append(append(section('交付产物'),el('p','inline-empty','还没有运行产物。')));
      renderLog(runs,chosen,old);renderRunHistory(runs,old);$('drawer-content').append(section('动态记录','event-section'));
      $('drawer-scroll').scrollTop=old.scroll;if(focused&&$(focused)){$(focused).focus({preventScroll:true});if(typeof selection[0]==='number')$(focused).setSelectionRange(...selection);}
    }
    renderEvents(list(data.events));
  }
  function renderTaskConversation(t,data,historical){
    if(!globalThis.TaskConversation||!t.continuation?.available)return;
    const key=t.id+':'+t.current_run_id;
    if(!conversationDrafts.has(key))conversationDrafts.set(key,{revision:data.input_draft?.revision||0,message:data.input_draft?.message||'',answers:{...(data.input_draft?.answers||{})}});
    const draft=conversationDrafts.get(key);
    const send=async(name,value)=>{
      const payload={task_id:t.id,revision:t.revision,run_id:t.current_run_id,fingerprint:t.fingerprint,message:value.message,answers:value.answers};
      payload.draft_revision=value.revision;
      const signature=JSON.stringify([name,payload]);
      if(draft.intent?.signature!==signature)draft.intent={signature,id:crypto.randomUUID()};
      payload.request_id=draft.intent.id;
      const ok=await action(name,payload,name==='save_input_draft'?'草稿已保存，尚未派工':'已提交新一轮处理，上一轮答案和复审记录已保留');
      if(ok){conversationDrafts.delete(key);if(name!=='save_input_draft')selectedRunId=null;detailSignature='';if(detailData)renderDetail(detailData);}
    };
    $('drawer-content').append(TaskConversation.render(t,draft,{historical,busy:isPending(t),disconnected:!connected,
      onChange:value=>{Object.assign(draft,value);draft.intent=null;},onSave:value=>send('save_input_draft',value),onSubmit:value=>send(t.continuation.action,value)}));
  }
  function renderTaskSummary(t) {
    const summary=section();const badges=el('div','badge-row');badges.style.marginTop='0';append(badges,badge(taskStageName(t),'blue'),t.review_status&&!t.source_completed?badge(t.state==='cancelled'?`上次复审：${reviewName(t)}`:reviewName(t),t.review_status==='approved'?'green':''):null,needsInput(t)?badge('需要输入','amber'):null,hasError(t)?badge('执行异常','red'):null);summary.append(badges);
    if(t.source_writeback_allowed===false)summary.append(el('p','notice','原滴答任务已完成；当前轮次仅处理关联追问，批准不会再次回写。'));
    if(t.source_completed)summary.append(el('p','notice','这项任务已在滴答中完成，不代表自动检查或人工复审通过。派工已停止，历史运行和产物仍保留。'));
    if(t.source_status===0&&t.state==='cancelled')summary.append(el('p','notice','滴答已重新打开这项任务。派工仍暂停，需要你重新分配后才会继续。'));
    if(t.source_checked_at)summary.append(el('p','field-help',`滴答状态确认于 ${dateLabel(t.source_checked_at,true)}`));
    const schedule=taskSchedule(t),scheduleText=[schedule.date,schedule.time].filter(Boolean).join(' ');
    const dl=el('dl','detail-meta');for(const [label,value]of [['任务日期',scheduleText],['优先级',priorityLabel(t.priority)],['难度',difficultyLabel(t.difficulty)],['执行器',t.executor||'尚未分配'],['任务类型',t.kind||'未指定'],['任务编号',t.id],['分配方式',t.manual_executor?'手动指定':'自动规则']]){append(dl,append(el('div'),el('dt','',label),el('dd','',value)));}summary.append(dl);if(t.runtime_progress)summary.append(el('p','notice',`${t.runtime_progress.label} · 最后活跃 ${dateLabel(t.runtime_progress.last_activity_at)}。这里只显示运行阶段，不展示思考正文。`));if(t.workspace)summary.append(el('p','detail-path',`工作区：${t.workspace}`));$('drawer-content').append(summary);
    const description=section('原始说明');description.append(el('p','description-text',t.description||'这项任务没有附加说明。'));if(t.error)description.append(el('p','notice error',stringify(t.error)));$('drawer-content').append(description);
  }
  function field(label,id,node){node.id=id;const f=el('label','field');f.htmlFor=id;append(f,el('span','',label),node);return f;}
  function workspaceNameError(name) {
    if(!name||Array.from(name).length>64)return '请填写 1–64 个字符的工作区名称。';
    for(const value of [name,name.normalize('NFKC')]){
      if(value!==value.trim()||value==='.'||value==='..'||value.endsWith('.')||/[<>:"/\\|?*\p{Cc}\p{Cf}\p{Cs}]/u.test(value)||/^(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9]|LPT[1-9])$/i.test(value.split('.')[0].trimEnd()))return '请使用单个文件夹名称，不含路径、首尾空格或系统保留名称。';
    }
    return '';
  }
  async function refreshWorkspaceCatalog() {
    if(workspaceCatalogRefreshing||!connected)return;
    workspaceCatalogRefreshing=true;workspaceCatalogError='';if(detailData)renderDetail(detailData);
    try{const catalog=await api('/api/workspaces');if(!catalog||!Array.isArray(catalog.items))throw new Error('目录列表格式不完整');if(state)state.workspace_catalog=catalog;}
    catch(error){workspaceCatalogError=`目录刷新失败：${error.message}。当前保留上次列表。`;}
    finally{workspaceCatalogRefreshing=false;if(detailData)renderDetail(detailData);}
  }
  function workspaceGroup(item) {
    const collection=typeof item.collection_label==='string'?item.collection_label.trim():'',category=typeof item.category==='string'?item.category:'';
    if(collection)return {key:JSON.stringify(['collection',collection,category]),label:`桌面汇总 · ${collection} / ${category?category.split('/').join(' / '):'根目录项目'}`,rank:0,desktop:true,category};
    const [label,rank]=({managed:['派工台历史',1],external:['已有会话补充',2],repository:['配置仓库',3]})[item.origin]||['其他已有目录',4];
    const agents=[...list(item.agents)].sort().join(' / ');
    return {key:JSON.stringify([label,agents]),label:`${label}${agents?' · '+agents:''}`,rank,desktop:false,category:''};
  }
  function workspaceOptionName(item,group) {
    const relative=typeof item.relative_path==='string'?item.relative_path:'';
    if(group.desktop&&relative)return group.category&&relative.startsWith(group.category+'/')?relative.slice(group.category.length+1):relative;
    return item.label||item.path;
  }
  function workspaceSelector(t,old,executor,repo,disabled,onChange) {
    const panel=el('div','workspace-selector'),grid=el('div','assignment-grid');
    const mode=el('select'),name=el('input'),existing=el('select'),choice=t.workspace_choice||{mode:'default'};
    for(const [value,label] of [['default','任务默认工作区'],['new','新建并命名工作区'],['existing','使用已有目录（直接修改）']])mode.add(new Option(label,value));
    mode.value=old.workspaceMode??choice.mode??'default';mode.disabled=disabled;
    name.type='text';name.autocomplete='off';name.maxLength=128;name.placeholder='例如：课程复习资料';name.value=old.workspaceName??choice.name??t.workspace_suggested_name??'';
    const modeField=field('本次工作区','assign-workspace-mode',mode),nameField=field('新工作区名称','assign-workspace-name',name),existingField=field('已有目录','assign-workspace-existing',existing);
    modeField.className+=' workspace-wide';existingField.className+=' workspace-wide';nameField.className+=' workspace-wide';
    append(grid,modeField,nameField,existingField);
    const help=el('p','field-help'),preview=el('div','workspace-preview'),error=el('p','field-help workspace-error'),warnings=el('div','workspace-warnings');
    help.id='assign-workspace-help';preview.id='assign-workspace-preview';error.id='assign-workspace-error';error.setAttribute('role','status');
    for(const node of [mode,name,existing])node.setAttribute('aria-describedby',`${help.id} ${error.id}`);
    const refresh=button(workspaceCatalogRefreshing?'正在检测…':'刷新目录','text-button',refreshWorkspaceCatalog,!connected||workspaceCatalogRefreshing);
    const refreshRow=append(el('div','workspace-refresh'),el('span','field-help','按桌面汇总目录分类；最近会话与历史记录作为补充'),refresh);
    append(panel,grid,help,preview,error,refreshRow,warnings);
    let selectedSource=null,currentError='';
    function populate(selected=existing.value){
      const agent=executor.value==='auto'?t.recommended_executor||t.executor:executor.value;
      const items=list(state?.workspace_catalog?.items),groups=new Map();
      existing.replaceChildren(new Option(items.length?'请选择已有目录':'尚未检测到目录',''));
      for(const item of items){
        const group=workspaceGroup(item);
        if(!groups.has(group.key))groups.set(group.key,{...group,items:[],matchesAgent:false});
        const matchesAgent=group.desktop&&Object.prototype.hasOwnProperty.call(item,'collection_agent')?Boolean(agent)&&item.collection_agent===agent:list(item.agents).includes(agent);
        const target=groups.get(group.key);target.items.push(item);target.matchesAgent=target.matchesAgent||matchesAgent;
      }
      const ordered=[...groups.values()].sort((a,b)=>Number(b.matchesAgent)-Number(a.matchesAgent)||a.rank-b.rank||a.label.localeCompare(b.label,'zh-CN',{numeric:true}));
      for(const group of ordered){
        const node=el('optgroup');node.label=group.label;
        group.items.sort((a,b)=>Number(list(b.agents).includes(agent))-Number(list(a.agents).includes(agent))||String(workspaceOptionName(a,group)).localeCompare(String(workspaceOptionName(b,group)),'zh-CN',{numeric:true}));
        for(const item of group.items){const option=new Option(`${workspaceOptionName(item,group)}${item.available?'':' · '+(item.reason||'不可用')}`,item.id);option.disabled=item.available!==true;node.append(option);}
        existing.append(node);
      }
      if(selected&&!items.some(item=>item.id===selected)){const missing=new Option('原选目录已不在列表中，请重新选择',selected);missing.disabled=true;existing.add(missing);}
      existing.value=selected;update();
    }
    function update(){
      const isNew=mode.value==='new',isExisting=mode.value==='existing';nameField.hidden=!isNew;existingField.hidden=!isExisting;name.disabled=disabled||!isNew;existing.disabled=disabled||!isExisting;
      selectedSource=list(state?.workspace_catalog?.items).find(item=>item.id===existing.value);
      currentError=isNew?workspaceNameError(name.value):isExisting?(workspaceCatalogRefreshing?'正在重新检测目录，请稍候。':!selectedSource?'请选择列表中可用的已有目录。':selectedSource.available!==true?selectedSource.reason||'此目录当前不可用，请重新选择。':''):'';
      name.setAttribute('aria-invalid',String(isNew&&Boolean(currentError)));existing.setAttribute('aria-invalid',String(isExisting&&Boolean(currentError)));
      error.textContent=currentError;error.hidden=!currentError;preview.replaceChildren();preview.hidden=!isExisting;
      if(isExisting){
        help.textContent='本次任务会直接修改所选目录；日志与验收快照单独保存。原有运行和复审记录仍保留。';
        if(selectedSource){
          const group=workspaceGroup(selectedSource),origin=({managed:'派工台运行',repository:'配置仓库',external:'执行器已有会话',collection:'桌面汇总'})[selectedSource.origin]||'已有目录';
          append(preview,el('p','workspace-source-label',`来源：${group.desktop?group.label:origin}${list(selectedSource.agents).length?' · '+selectedSource.agents.join(' / '):''}`),el('p','detail-path mono',`执行目录：${selectedSource.path}`));
          if(selectedSource.reason)preview.append(el('p','field-help',selectedSource.reason));
          if(selectedSource.repo_alias){
            preview.append(el('p','field-help',`来源关联仓库：${selectedSource.repo_alias}${repo.value&&repo.value!==selectedSource.repo_alias?'；当前目标仓库保持为 '+repo.value:''}`));
            if(!repo.value&&list(state?.repositories).some(item=>item.alias===selectedSource.repo_alias))preview.append(button('使用来源关联仓库','text-button',()=>{repo.value=selectedSource.repo_alias;update();},disabled));
          }
        }
      }else if(isNew)help.textContent=`以此名称建立独立工作区，按任务与目标仓库配置准备初始文件。${state?.workspace_policy?.managed_root?' 保存于 '+state.workspace_policy.managed_root+'。':''}`;
      else help.textContent='沿用任务默认规则创建本次独立工作区；选择目标仓库时，从该仓库准备初始文件。';
      warnings.replaceChildren();if(workspaceCatalogError)warnings.append(el('p','field-help workspace-error',workspaceCatalogError));
      for(const warning of list(state?.workspace_catalog?.warnings))warnings.append(el('p','field-help',stringify(warning)));
      onChange();
    }
    mode.addEventListener('change',update);name.addEventListener('input',update);existing.addEventListener('change',update);repo.addEventListener('change',update);
    populate(old.workspaceId??choice.workspace_id??'');
    return {node:panel,refresh:populate,valid:()=>!currentError,value:()=>mode.value==='new'?{mode:'new',name:name.value}:mode.value==='existing'?{mode:'existing',workspace_id:existing.value}:{mode:'default'}};
  }
  function renderAssignment(t,old) {
    if(t.source_completed)return;
    const s=section('任务分配');const grid=el('div','assignment-grid'),executor=el('select'),repo=el('select'),model=el('input'),effort=el('select');executor.add(new Option('自动分配','auto'));for(const a of list(state?.agents)){const o=new Option(a.name+(a.enabled?'':'（未启用）'),a.name);o.disabled=!a.enabled;executor.add(o);}const selected=old.executor||t.manual_executor||'auto';executor.value=Array.from(executor.options).some(o=>o.value===selected)?selected:'auto';repo.add(new Option('任务默认仓库',''));for(const r of list(state?.repositories)){repo.add(new Option(r.alias,r.alias));}repo.value=old.repo!==undefined?old.repo:t.repo_alias||'';const disabled=!canAssign(t);executor.disabled=disabled;repo.disabled=disabled;
    model.type='text';model.autocomplete='off';model.maxLength=128;model.placeholder='留空沿用默认，或从下方完整列表选择';model.value=old.model!==undefined?old.model:t.execution_overrides?.model||'';const effortValue=old.effort!==undefined?old.effort:t.execution_overrides?.effort||'';
    let currentAgent=agentByName(executor.value),validEffort=true;const help=el('p','field-help');help.id='assign-settings-help';model.setAttribute('aria-describedby',help.id);effort.setAttribute('aria-describedby',help.id);
    const modelField=append(el('div','field'),field('本次模型（可选）','assign-model',model));let picker=modelPicker(currentAgent,model,'assign-model-picker',modelChanged);modelField.append(picker);
    append(grid,field('执行器','assign-executor',executor),field('目标仓库','assign-repo',repo),modelField,field('本次思考强度（可选）','assign-effort',effort));append(s,grid,help);
    let delivery;
    if(Array.isArray(state?.delivery_modes)){
      delivery=el('select');for(const [value,label] of [['auto','自动判断'],['answer','纯文本回答（不改文件）'],['files','文件交付']])delivery.add(new Option(label,value));
      delivery.value=old.delivery!==undefined?old.delivery:t.delivery_mode||'auto';delivery.disabled=disabled;
      grid.append(field('本次交付形式','assign-delivery',delivery));
      s.append(el('p','field-help','纯文本回答支持 Codex、CodeBuddy；其他执行器继续使用文件交付。'));
    }
    let workspace,permission;
    const controls=el('div','detail-actions');const assign=button(isPending(t)?'操作处理中…':t.state==='cancelled'?'重新分配':'确认分配','button primary',()=>{if(disabled||(workspace&&!workspace.valid()))return;if(!validEffort){toast('所选模型不支持当前强度，请重新选择。',true);return;}const p={task_id:t.id,revision:t.revision,executor:executor.value,execution_overrides:executor.value==='auto'?{}:collectSettings(model.value,effort.value)};if(permission){if(!permission.valid()){toast("请先确认本次完全访问的范围。",true);return;}Object.assign(p,permission.value());}if(delivery)p.delivery_mode=delivery.value;if(workspace&&supportsWorkspaceChoice())p.workspace_choice=workspace.value();if(repo.value)p.repo_alias=repo.value;action('assign',p,'任务分配已更新');},disabled);controls.append(assign);
    function updateSubmit(){assign.disabled=disabled||!validEffort||(workspace?!workspace.valid():false)||(permission?!permission.valid():false);}
    if(supportsWorkspaceChoice()){workspace=workspaceSelector(t,old,executor,repo,disabled,updateSubmit);s.append(workspace.node);}
    function updateChoices(value,reset=false){currentAgent=agentByName(executor.value);const auto=executor.value==='auto';if(reset||auto){model.value='';value='';}model.disabled=disabled||auto||currentAgent?.capabilities?.model!==true;picker.disabled=model.disabled||!list(currentAgent?.capabilities?.models).length;validEffort=populateEfforts(effort,currentAgent,model.value.trim(),value,disabled||auto);help.textContent=auto?'自动分配会使用最终执行器的默认配置。选择具体执行器后，可为本次会话单独设置。':`${modelHelp(currentAgent,model.value.trim())} 当前执行器默认：${settingsLabel(currentAgent?.effective_defaults)}。`;updateSubmit();}
    if(globalThis.SessionPermissions){permission=SessionPermissions.create({agent:currentAgent,profile:old.permissionProfile||t.permission_profile||"default",disabled,answer:delivery?.value==="answer",onChange:updateSubmit});s.append(permission.node);delivery?.addEventListener("change",()=>permission.refresh(agentByName(executor.value),delivery.value==="answer"));}
    updateChoices(effortValue);
    executor.addEventListener('change',()=>{updateChoices('',true);const replacement=modelPicker(currentAgent,model,'assign-model-picker',modelChanged);picker.replaceWith(replacement);picker=replacement;workspace?.refresh();permission?.refresh(agentByName(executor.value),delivery?.value==='answer');});
    function modelChanged(){const allowed=availableEfforts(currentAgent,model.value.trim());updateChoices(allowed.includes(effort.value)?effort.value:'');}
    model.addEventListener('input',modelChanged);effort.addEventListener('change',()=>{validEffort=true;updateSubmit();});
    if(t.can_withdraw)controls.append(button('撤回待办','button secondary',()=>action('withdraw',{task_id:t.id,revision:t.revision},'任务已撤回待办'),!connected||isPending(t)));s.append(controls);s.append(el('p','field-help',disabled?'当前阶段不能重新分配。任务进入可分配状态后，这里会自动开放。':'分配提交后，由调度器按队列执行。仓库列表来自本地配置。'));if(t.route_reason)s.append(el('p','field-help',`分配依据：${stringify(t.route_reason)}`));$('drawer-content').append(s);
  }
  function workspaceRecord(binding,path,artifactRoot) {
    const record=el('div','workspace-record');
    if(!binding||!Object.keys(binding).length){record.append(el('p','field-help','本次运行未记录工作区来源，不能用当前选择推定。'));if(path)record.append(el('p','detail-path mono',`记录的工作区：${path}`));return record;}
    const label=({default:'任务默认工作区',new:'新建命名工作区',existing:'从已有目录继续'})[binding.mode]||'已记录工作区';
    const inPlace=binding.strategy==='in_place',snapshotPath=inPlace||binding.snapshot_path?path||artifactRoot||binding.snapshot_path:null;
    record.append(el('p','field-help',`${label}${inPlace?' · 直接修改所选目录':binding.strategy==='copy'?' · 独立副本，原目录保留':''}`));
    if(binding.source?.path&&(!inPlace||binding.source.path!==binding.workspace_path))record.append(el('p','detail-path mono',`来源目录：${binding.source.path}`));
    if(binding.workspace_path||(!inPlace&&path))record.append(el('p','detail-path mono',`${inPlace?'执行目录':'本次工作区'}：${binding.workspace_path||path}`));
    if(snapshotPath)record.append(el('p','detail-path mono',`本次验收快照：${snapshotPath}`));
    if(inPlace)record.append(el('p','field-help',snapshotPath?'历史产物以本次验收快照为准；执行目录当前内容可能已变更。':'本次运行尚未记录验收快照；执行目录当前内容不代表历史交付。'));
    return record;
  }
  function renderWorkspaceBinding(task,run) {
    const isCurrent=run&&String(run.id)===String(task?.current_run_id),binding=run?(run.workspace_binding||(isCurrent?task?.workspace_binding:null)):task?.workspace_binding;
    const s=section('本次工作区');s.append(workspaceRecord(binding,run?.workspace||(!run?task?.workspace:null),run?.artifact_root));$('drawer-content').append(s);
  }
  function renderExecutionSettings(task,run) {
    const s=section('本次启动配置');if(run.permissions){s.append(el('p','field-help',`工具权限：${run.permissions.label} · 文件范围：${({workspace:'工作区', 'read-only':'只读',full:'普通账户完整访问'})[run.permissions.filesystem]||run.permissions.filesystem} · 任务结果由你复审`));}else{s.append(el('p','field-help','本轮未记录权限快照；不能用当前选项推定历史权限。'));}if(run.execution_limits){const limits=run.execution_limits,parts=[`本轮最长 ${limits.total_seconds/60} 分钟`];if(limits.first_token_seconds)parts.push(`首输出等待 ${limits.first_token_seconds/60} 分钟`);if(limits.stream_idle_seconds)parts.push(`流空闲等待 ${limits.stream_idle_seconds/60} 分钟`);if(limits.http_request_seconds)parts.push(`HTTP 请求上限 ${limits.http_request_seconds/60} 分钟`);s.append(el('p','field-help',parts.join(' · ')+'。按本轮记录执行，不会自动换模型。'));}const isCurrent=String(run.id)===String(task?.current_run_id),settings=run.execution_settings||(isCurrent?task?.execution_settings:null);
    if(!settings||!Object.keys(settings).length){s.append(el('p','inline-empty','这次历史运行未记录模型与思考强度，不能用当前默认值推定。'));}
    else {const dl=el('dl','detail-meta execution-settings-meta'),values=[['执行器',settings.executor||run.executor||'未记录'],['模型',settings.model||'沿用厂商默认'],['思考强度',settings.effort?effortLabel(settings.effort):'沿用默认强度']];if(settings.cli_model&&settings.cli_model!==settings.model)values.push(['提交模型标识',settings.cli_model]);for(const [label,value] of values)append(dl,append(el('div'),el('dt','',label),el('dd','',value)));append(s,dl,el('p','field-help',`运行 #${shortId(run.id)} 启动时记录的提交参数；修改默认配置不会改变这条记录。未显式指定的值由厂商决定。`));}
    $('drawer-content').append(s);
  }
  function renderReviewControls(t,old) {
    const s=section(t.source_completed?'历史检查与复审记录':'自动检查与人工复审');const historical=selectedRunId&&String(selectedRunId)!==String(t.current_run_id);
    if(historical){const historicalRun=list(detailData?.runs).find(r=>String(r.id)===String(selectedRunId));if(historicalRun?.review_status)s.append(badge(reviewName(historicalRun),historicalRun.review_status==='approved'?'green':''));if(historicalRun?.review_note)s.append(el('p','field-help',historicalRun.review_note));if(historicalRun?.summary)s.append(el('p','description-text',historicalRun.summary));if(list(historicalRun?.checks).length){const checks=el('ul','check-list');historicalRun.checks.forEach(check=>checks.append(el('li','',stringify(check))));s.append(checks);}append(s,el('p','notice warning','正在查看历史运行。请返回当前交付后再进行批准或退回。'),button('返回当前交付','button secondary',()=>{selectedRunId=null;detailSignature='';renderDetail(detailData);refreshDetail();}));$('drawer-content').append(s);return;}
    if(t.source_completed){s.append(el('p','field-help','滴答已完成，当前无需批准或退回。以下保留上次运行的记录，来源完成状态不会更改原复审结论。'));if(t.review_status)s.append(badge(`上次复审：${reviewName(t)}`));if(t.summary)s.append(el('p','description-text',stringify(t.summary)));if(list(t.checks).length){const checks=el('ul','check-list');t.checks.forEach(check=>checks.append(el('li','',stringify(check))));s.append(checks);}if(t.review_note)s.append(el('p','field-help',`原复审记录：${t.review_note}`));if(t.feedback&&t.feedback!==t.review_note)s.append(el('p','field-help',`原退回反馈：${stringify(t.feedback)}`));$('drawer-content').append(s);return;}
    if(t.runtime_warning)s.append(el('p','notice warning',stringify(t.runtime_warning)));
    const good=/^(passed|pass|success|approved)$/.test(t.auto_review_status||''),bad=/^(failed|fail|rejected)$/.test(t.auto_review_status||'');s.append(el('div',`auto-result${good?' good':bad?' bad':''}`,autoReviewLabel(t.auto_review_status)));if(t.summary)s.append(el('p','description-text',stringify(t.summary)));if(list(t.checks).length){const checks=el('ul','check-list');t.checks.forEach(check=>checks.append(el('li','',stringify(check))));s.append(checks);}if(t.review_note)s.append(el('p','field-help',`复审记录：${t.review_note}`));if(t.feedback&&t.feedback!==t.review_note)s.append(el('p','field-help',`退回反馈：${stringify(t.feedback)}`));
    if(t.stage==='review'||t.can_approve||t.can_reject){s.append(el('p','review-instruction',t.source_writeback_allowed===false?'请复审本轮解释答复。批准仅确认本轮答案，不会修改原滴答任务。':'请先查看本次答案或文件及检查依据。批准后调度器才会把任务标为滴答已完成；需要调整时请填写退回说明。'));const note=el('textarea');note.value=old.note||'';note.placeholder='说明需要补充、修改或重新验证的内容';note.maxLength=10000;note.disabled=!connected||isPending(t);s.append(field('退回说明（退回时必填）','review-note',note));const controls=el('div','detail-actions');const payload=()=>({task_id:t.id,revision:t.revision,run_id:t.current_run_id,fingerprint:t.fingerprint,manifest_digest:t.manifest_digest});controls.append(button(t.source_writeback_allowed===false?'仅批准本轮答复':'批准并完成滴答','button primary',()=>action('approve',payload(),t.source_writeback_allowed===false?'本轮答复已批准，原滴答任务保持不变':'复审已批准，调度器将回写滴答完成状态'),!connected||!t.can_approve||isPending(t)),button('退回修改','button danger',()=>{if(!note.value.trim()){toast('请先填写具体的退回说明。',true);note.focus();return;}action('reject',{...payload(),note:note.value.trim()},'任务已退回，反馈已保存');},!connected||!t.can_reject||isPending(t)));s.append(controls);if(!t.can_approve)s.append(el('p','field-help','当前结果尚不满足批准条件，请检查自动检查结果和任务状态。'));}
    else s.append(el('p','field-help',t.review_status==='legacy'?(t.state==='done'?'这条记录来自历史自动验收，不代表人工审核通过。':'这是一条尚未完成的历史运行，不代表验收通过。'):t.review_status==='approved'?'本次交付已经人工审核通过。':'执行完成后，你可以在这里查看结果并进行人工复审。'));$('drawer-content').append(s);
  }
  function renderArtifacts(run) {
    const s=section('交付产物');const id=run.id;if(selectedRunId)s.append(el('p','field-help',`正在查看运行 #${id}`));const artifacts=list(run.artifacts);if(!artifacts.length)s.append(el('p','inline-empty','这次运行还没有可展示的产物。'));else{const container=el('div','artifact-list');for(const a of artifacts){const row=el('div','artifact-row'),copy=el('div');append(copy,el('div','artifact-name',a.path),el('div','artifact-size',sizeLabel(a.size)));const actions=el('div','artifact-actions'),preview=button('预览','text-button',()=>previewArtifact(id,a.path));const download=el('a','text-button','下载');download.href=`/api/artifact?${new URLSearchParams({run_id:id,path:a.path,download:'1'})}`;download.setAttribute('download','');append(actions,preview,download);append(row,copy,actions);container.append(row);}s.append(container);}const preview=el('div');preview.id='artifact-preview';s.append(preview);if(canOpenWorkspace()&&(run.workspace||run.run_dir))s.append(append(el('div','detail-actions'),button(workspaceOpenLabel(run),'button secondary',()=>action('open_workspace',{run_id:id},`已请求${workspaceOpenLabel(run)}`),!connected||pending.has(`run:${id}`))));$('drawer-content').append(s);
  }
  async function previewArtifact(runId,path) {
    const ticket=++artifactTicket;const container=$('artifact-preview');container.replaceChildren(el('p','field-help','正在读取产物…'));
    try{const data=await api(`/api/artifact?${new URLSearchParams({run_id:runId,path})}`);if(ticket!==artifactTicket||!container.isConnected)return;container.replaceChildren();append(container,append(el('div','preview-heading'),el('strong','',data.name||path),button('收起','text-button',()=>container.replaceChildren())));if(data.binary)container.append(el('p','field-help','此文件无法以文本预览，请下载后查看。'));else {const pre=el('pre','text-preview',data.text||'（空文件）');pre.tabIndex=0;container.append(pre);if(data.truncated)container.append(el('p','field-help','预览已截断，下载可查看完整文件。'));}}
    catch(error){if(ticket===artifactTicket&&container.isConnected)container.replaceChildren(el('p','notice error',`无法预览：${error.message}`));}
  }
  function renderLog(runs,chosen,old) {
    const s=section(),details=el('details');details.id='log-details';details.open=Boolean(old.logOpen);details.append(el('summary','details-toggle','执行与检查日志'));if(!runs.length){details.append(el('p','field-help','暂无运行日志。'));append(s,details);$('drawer-content').append(s);return;}
    const controls=el('div','log-controls'),run=el('select'),phase=el('select'),stream=el('select');run.id='log-run';run.setAttribute('aria-label','日志所属运行');for(const r of runs)run.add(new Option(`#${shortId(r.id)} · ${r.executor||'执行器'}`,r.id));run.value=old.logRun&&runs.some(r=>String(r.id)===old.logRun)?old.logRun:chosen?.id||runs[0].id;phase.id='log-phase';phase.setAttribute('aria-label','日志阶段');phase.add(new Option('执行日志','execution'));phase.add(new Option('自动检查','verification'));phase.value=old.phase||'execution';stream.id='log-stream';stream.setAttribute('aria-label','日志类型');stream.add(new Option('事件输出','events'));stream.add(new Option('错误输出','stderr'));stream.value=old.stream||'events';const pre=el('pre','text-preview',old.logText||'展开后读取日志。');pre.id='log-text';pre.tabIndex=0;
    append(controls,run,phase,stream,button('刷新','text-button',()=>loadLog()));append(details,controls,pre);const note=el('p','log-note','日志按服务端返回内容展示。');note.id='log-note';details.append(note);[run,phase,stream].forEach(n=>n.addEventListener('change',()=>loadLog()));details.addEventListener('toggle',()=>{if(details.open)loadLog();});append(s,details);$('drawer-content').append(s);
  }
  async function loadLog(quiet=false) {
    const output=$('log-text'),run=$('log-run');if(!output||!run||logFetching)return;logFetching=true;const ticket=detailTicket,runId=run.value,phase=$('log-phase').value,stream=$('log-stream').value;if(!quiet)output.textContent='正在读取日志…';
    try {const data=await api(`/api/log?${new URLSearchParams({run_id:runId,phase,stream})}`);if(ticket!==detailTicket||!output.isConnected||$('log-run')?.value!==runId||$('log-phase')?.value!==phase||$('log-stream')?.value!==stream)return;const pinned=output.scrollHeight-output.scrollTop-output.clientHeight<30;output.textContent=data.text||'暂无日志输出。';if(pinned)output.scrollTop=output.scrollHeight;$('log-note').textContent=data.truncated?'日志较长，仅显示服务端截取的内容。':'日志按服务端返回内容展示。';}
    catch(error){if(ticket===detailTicket&&output.isConnected)output.textContent=`日志读取失败：${error.message}`;}finally{logFetching=false;}
  }
  function renderRunHistory(runs,old) {
    const s=section(),details=el('details');details.id='history-details';details.open=Boolean(old.historyOpen);details.append(el('summary','details-toggle',`运行历史 · ${runs.length}`));if(!runs.length)details.append(el('p','field-help','任务尚未运行。'));
    for(const r of runs){const item=el('div','run-history-item');append(item,append(el('div','run-history-head'),el('span','mono',`第 ${r.round_number||'?'} 轮 · #${shortId(r.id)}`),badge(reviewName(r),r.review_status==='approved'?'green':'')),el('p','run-history-meta',`${r.executor||'未记录执行器'} · ${dateLabel(r.created_at)}${r.archived_at?' · 已归档':''}`));if(r.review_note)item.append(el('p','field-help',r.review_note));item.append(workspaceRecord(r.workspace_binding,r.workspace,r.artifact_root));item.append(button('查看本次结果','text-button',()=>{selectedRunId=r.id;detailSignature='';renderDetail(detailData);refreshDetail();}));details.append(item);}append(s,details);$('drawer-content').append(s);
  }
  function renderEvents(events) {
    const s=$('event-section');if(!s)return;const signature=JSON.stringify(events);if(s.dataset.signature===signature)return;s.dataset.signature=signature;s.replaceChildren(el('h3','','动态记录'));if(!events.length){s.append(el('p','inline-empty','暂无动态记录。'));return;}const timeline=el('ol','timeline');for(const event of events.slice(0,30).reverse()){const time=el('time','',dateLabel(event.created_at));if(event.created_at)time.dateTime=event.created_at;timeline.append(append(el('li'),el('p','',event.message||'状态已更新'),time));}s.append(timeline);
  }
  $('logout-button').addEventListener('click',async()=>{const button=$('logout-button');button.disabled=true;try{await api('/auth/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});location.replace('/auth/login');}catch(error){toast(error.message,true);button.disabled=false;}});
  $('search-input').addEventListener('input',e=>{query=e.target.value.trim().toLocaleLowerCase();renderMain();});$('agent-filter').addEventListener('change',e=>{agentFilter=e.target.value;renderMain();});$('secondary-filter').addEventListener('change',e=>{secondaryFilter=e.target.value;renderMain();});$('sync-button').addEventListener('click',()=>action('sync',{},'滴答同步已完成'));$('retry-button').addEventListener('click',refresh);$('drawer-close').addEventListener('click',closeDrawer);$('drawer-backdrop').addEventListener('click',closeDrawer);window.addEventListener('hashchange',()=>setView(location.hash.slice(1)));window.addEventListener('online',refresh);
  document.addEventListener('keydown',e=>{
    if(e.key==='Escape'&&!$('task-drawer').hidden){closeDrawer();return;}
    if(e.key==='/'&&$('task-drawer').hidden&&!/INPUT|TEXTAREA|SELECT/.test(e.target.tagName)){e.preventDefault();$('search-input').focus();}
    if(e.key==='Tab'&&!$('task-drawer').hidden){const focusable=Array.from($('task-drawer').querySelectorAll('button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled),textarea:not(:disabled),summary,[tabindex="0"]')).filter(n=>n.getClientRects().length);const first=focusable[0],last=focusable[focusable.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus();}}
  });
  setView(location.hash.slice(1));refresh();setInterval(()=>{if(!document.hidden)refresh();},2000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
})();
