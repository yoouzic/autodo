/* Render-only board ordering checks: no service, filesystem mutation or executor. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const test=require('node:test');

function boardHarness(){
  class Element {
    constructor(tag,text=''){this.tagName=tag.toUpperCase();this.children=[];this._text=text;this.dataset={};this.style={setProperty(){}};this.listeners={};this.className='';}
    get textContent(){return this._text+this.children.map(n=>n.textContent).join('');}
    set textContent(value){this._text=String(value);this.children=[];}
    get childNodes(){return this.children;}
    append(...values){for(const value of values)this.children.push(typeof value==='string'?new Element('span',value):value);}
    replaceChildren(...values){this._text='';this.children=[];this.append(...values);}
    addEventListener(name,handler){this.listeners[name]=handler;}
    setAttribute(name,value){this[name]=value;}
  }
  const content=new Element('div');
  const document={getElementById:id=>id==='view-content'?content:null,createElement:tag=>new Element(tag)};
  const source=fs.readFileSync(path.join(__dirname,'../dispatcher/static/app.js'),'utf8');
  const context={document,Intl,console,Date:class extends Date{constructor(...args){super(...(args.length?args:['2026-11-07T00:00:00Z']));}}};
  vm.runInNewContext(source.slice(0,source.indexOf("  $('logout-button').addEventListener"))+`globalThis.ui={renderBoard,taskSchedule,setTasks(tasks){state={tasks};}};})();`,context);
  const descendants=node=>[node,...node.children.flatMap(descendants)];
  return {schedule:task=>context.ui.taskSchedule(task),render(tasks){
    content.replaceChildren();context.ui.setTasks(tasks);context.ui.renderBoard(tasks);
    return Object.fromEntries(descendants(content).filter(n=>n.className==='board-column').map(column=>[
      column['aria-label'],descendants(column).filter(n=>n.className==='task-card').map(card=>({
        id:card.dataset.focusKey.slice(5),text:card.textContent,label:card['aria-label']}))]));
  }};
}
const task=(id,fields={})=>({id,title:id,state:'ignored',stage:'backlog',time_zone:'Asia/Shanghai',priority:0,...fields});
const ids=column=>column.map(card=>card.id);

test('each board column uses real descending dates across months and years without mutating task order',()=>{
  const h=boardHarness();
  const dates=[['nov14','2026-11-14'],['feb','2026-02-01'],['nov20','2026-11-20'],['past','2025-12-31'],['nov16','2026-11-16'],['future','2027-01-01']];
  const tasks=['backlog','queued','running','review','approved','done'].flatMap(stage=>dates.map(([id,start_date])=>task(stage+'-'+id,{stage,start_date,priority:id==='past'?5:0})));
  const original=JSON.stringify(tasks),columns=h.render(tasks);
  for(const [label,stage] of [['待办','backlog'],['排队','queued'],['处理中','running'],['待复审','review'],['审核通过','approved'],['已完成','done']]){
    assert.deepEqual(ids(columns[label]),['future','nov20','nov16','nov14','feb','past'].map(id=>stage+'-'+id));
  }
  assert.equal(JSON.stringify(tasks),original);
  assert.match(columns['待办'][0].text,/2027\.1\.1/);
});

test('same displayed day orders high medium low none, then local time descending with untimed last',()=>{
  const h=boardHarness(),day='2026-11-20';
  const tasks=[task('none-late',{start_date:day+'T15:00:00Z'}),task('medium-late',{priority:3,start_date:day+'T14:00:00Z'}),
    task('high-untimed',{priority:5,start_date:day}),task('low-late',{priority:1,start_date:day+'T13:00:00Z'}),
    task('high-early',{priority:5,start_date:day+'T01:00:00Z'}),task('high-late',{priority:'high',start_date:day+'T06:00:00Z'}),
    task('urgent-middle',{priority:'urgent',start_date:day+'T03:00:00Z'})];
  assert.deepEqual(ids(h.render(tasks)['待办']),['high-late','urgent-middle','high-early','high-untimed','medium-late','low-late','none-late']);
});

test('all-day, date-only and due-only tasks sort using the same source-zone date shown on cards',()=>{
  const h=boardHarness();
  const tasks=[
    task('ny-previous-day',{priority:5,start_date:'2026-11-05T01:00:00Z',time_zone:'America/New_York'}),
    task('date-only-west',{priority:3,start_date:'2026-11-05',time_zone:'America/Los_Angeles',is_all_day:true}),
    task('all-day-shanghai',{priority:3,start_date:'2026-11-04T16:00:00.000+0000',is_all_day:true}),
    task('due-only',{priority:3,due_date:'2026-11-05T14:00:00Z'}),
    task('timed-shanghai',{priority:3,start_date:'2026-11-05T00:30:00Z'}),
  ];
  const cards=h.render(tasks)['待办'];
  assert.deepEqual(ids(cards),['due-only','timed-shanghai','date-only-west','all-day-shanghai','ny-previous-day']);
  assert.match(cards[0].text,/11\.5/);assert.match(cards[0].text,/截止 22:00/);
  assert.match(cards.at(-1).text,/11\.4/);assert.match(cards.at(-1).text,/20:00/);
  assert.doesNotMatch(cards.find(card=>card.id==='all-day-shanghai').text,/00:00/);
});

test('invalid or absent displayed dates stay last and do not borrow created timestamps',()=>{
  const h=boardHarness();
  const tasks=[task('no-date',{priority:5,created_at:'2099-01-01'}),task('invalid-date',{priority:5,start_date:'2026-02-30'}),
    task('bad-zone',{priority:5,start_date:'2027-01-01',time_zone:'Invalid/Timezone'}),
    task('dated',{priority:0,start_date:'2026-11-14'}),task('invalid-start-valid-due',{priority:0,start_date:'bad',due_date:'2026-11-16'})];
  assert.deepEqual(ids(h.render(tasks)['待办']),['invalid-start-valid-due','dated','no-date','invalid-date','bad-zone']);
});

test('ranges sort by their first displayed date and exact ties retain original order',()=>{
  const h=boardHarness();
  const tasks=[task('stable-z',{start_date:'2026-11-14',priority:3}),task('range',{start_date:'2026-12-31',due_date:'2027-01-02',is_all_day:true}),
    task('stable-a',{start_date:'2026-11-14',priority:3}),task('new-year',{start_date:'2027-01-01',priority:0}),
    task('midnight',{start_date:'2026-11-14T00:00:00+08:00',priority:3})];
  const cards=h.render(tasks)['待办'];
  assert.deepEqual(ids(cards),['new-year','range','midnight','stable-z','stable-a']);
  assert.match(cards[1].text,/12\.31–2027\.1\.2/);
});
