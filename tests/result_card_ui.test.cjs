const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');

function load(){
  class Element{
    constructor(tag){this.tagName=tag;this.children=[];this._text='';this.listeners={};}
    append(...items){this.children.push(...items);}
    set textContent(value){this._text=String(value);}
    get textContent(){return this._text+this.children.map(x=>x.textContent||'').join('');}
    addEventListener(name,fn){this.listeners[name]=fn;}
    setAttribute(){}
  }
  const file=path.join(__dirname,'../dispatcher/static/result-card.js');
  assert.ok(fs.existsSync(file),'Result card module missing');
  const context={document:{createElement:tag=>new Element(tag)},navigator:{clipboard:{writeText:async()=>{}}}};
  vm.runInNewContext(fs.readFileSync(file,'utf8'),context);
  return context.DispatcherResultCard;
}
const delivery={delivery_type:'answer',status:'complete',summary:'已给出三点',answer_markdown:'# 借阅规则\n<script>alert(1)</script>\n- 一条规则',
  evidence_refs:[{kind:'task_input',label:'用户规则',detail:'限借两本'}],limitations:[],file_changes:{status:'verified',added:0,modified:0,deleted:0}};
test('answer card presents full text, explicit provenance and zero-file evidence safely',()=>{
  const node=load().render(delivery,{executor:'codex',model:'test',historical:false});
  assert.match(node.textContent,/借阅规则/);assert.match(node.textContent,/用户提供/);
  assert.match(node.textContent,/未改动业务文件/);
  const all=n=>[n,...n.children.flatMap(all)];
  assert.ok(!all(node).some(n=>['script','iframe','img'].includes(n.tagName)));
  assert.match(node.textContent,/<script>alert\(1\)<\/script>/);
});
test('history and unknown change counts are explicit, needs input not success',()=>{
  const node=load().render({...delivery,status:'needs_input',needs_input:'请补充来源',file_changes:{status:'unverified'}},{historical:true});
  assert.match(node.textContent,/历史结果/);assert.match(node.textContent,/请补充来源/);
  assert.match(node.textContent,/文件变更未核验/);assert.doesNotMatch(node.textContent,/未改动业务文件/);
});
test('Markdown tables become safe cells in a bounded scrolling region',()=>{
  const node=load().render({...delivery,answer_markdown:'| 方案 | 余额 |\n|---|---:|\n| 甲 | 10元 |\n| 乙 | <img src=x onerror=alert(1)> |'});
  const all=n=>[n,...n.children.flatMap(all)];
  assert.equal(all(node).filter(n=>n.tagName==='table').length,1);
  assert.equal(all(node).filter(n=>n.tagName==='td').length,4);
  assert.ok(!all(node).some(n=>n.tagName==='img'));
});
test('emphasis and quotes are readable without interpreting HTML',()=>{
  const card=load().render({...delivery,answer_markdown:'- 最多 **2** 本\n> 提醒：请归还。\n**<img src=x>**'});
  const all=n=>[n,...n.children.flatMap(all)];
  assert.equal(all(card).filter(n=>n.tagName==='strong').length,2);
  assert.equal(all(card).filter(n=>n.tagName==='blockquote').length,1);
  assert.ok(!all(card).some(n=>n.tagName==='img'));
});
