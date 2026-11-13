const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),test=require('node:test');
function harness(){
  class Element{
    constructor(tag){this.tagName=tag;this.children=[];this.listeners={};this.value='';this._text='';this.disabled=false;}
    append(...nodes){this.children.push(...nodes);}
    set textContent(v){this._text=String(v);}get textContent(){return this._text+this.children.map(n=>n.textContent).join('');}
    setAttribute(k,v){this[k]=v;}addEventListener(k,fn){this.listeners[k]=fn;}
  }
  const source=path.join(__dirname,'../dispatcher/static/task-conversation.js');assert.ok(fs.existsSync(source),'Conversation editor missing');
  const context={document:{createElement:tag=>new Element(tag)}};vm.runInNewContext(fs.readFileSync(source,'utf8'),context);
  return {api:context.TaskConversation,all:n=>[n,...n.children.flatMap(function all(c){return [c,...c.children.flatMap(all)];})]};
}
const task={id:'t',current_run_id:'r',continuation:{available:true,ready:true,executor:'codebuddy',action:'respond_input',settings:{model:'hy4free',effort:'high'},questions:[{id:'budget',prompt:'预算是多少？',options:['300','350'],required:true}]}};
test('required answers gate continuation while drafts never dispatch',()=>{
  const h=harness(),calls=[];let latest;
  const n=h.api.render(task,{revision:0,message:'',answers:{}},{onChange:v=>latest=v,onSave:v=>calls.push(['save',v]),onSubmit:v=>calls.push(['submit',v])});
  const all=h.all(n),send=all.find(n=>n.textContent==='补充并继续'&&n.tagName==='button'),save=all.find(n=>n.textContent==='保存草稿'&&n.tagName==='button');
  assert.equal(send.disabled,true);save.listeners.click();assert.equal(calls[0][0],'save');
  const field=all.find(n=>n.id==='followup-answer-budget');field.value='350';field.listeners.input();
  assert.equal(send.disabled,false);assert.equal(latest.answers.budget,'350');send.listeners.click();assert.equal(calls[1][0],'submit');
});
test('explanation and historical rounds are explicit and contain no unsafe elements',()=>{
  const h=harness();const n=h.api.render({...task,continuation:{...task.continuation,explanation_only:true,action:'follow_up',questions:[]}},{message:'<script>bad</script>',answers:{},revision:0},{historical:true});
  assert.match(n.textContent,/历史/);assert.ok(!h.all(n).some(n=>n.tagName==='button'||n.tagName==='script'));
  const live=h.api.render({...task,continuation:{...task.continuation,explanation_only:true,action:'follow_up',questions:[]}},{message:'解释',answers:{},revision:0},{});
  assert.match(live.textContent,/不会改变滴答/);
});
