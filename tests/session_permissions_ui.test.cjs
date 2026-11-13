const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),test=require('node:test');
function harness(){
 class Element {
  constructor(tag){this.tagName=tag;this.children=[];this.listeners={};this.value='';this.checked=false;this._text='';}
  append(...n){this.children.push(...n);}add(n){this.append(n);}replaceChildren(){this.children=[];}
  get options(){return this.children;}set textContent(t){this._text=t;}get textContent(){return this._text+this.children.map(x=>x.textContent).join('');}
  addEventListener(k,f){this.listeners[k]=f;}
 }
 const context={document:{createElement:t=>new Element(t)},Option:function(label,value){this.textContent=label;this.value=value;this.children=[];}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../dispatcher/static/session-permissions.js'),'utf8'),context);
 return {create:context.SessionPermissions.create,all:function all(n){return [n,...n.children.flatMap(all)];}};
}
const agent={permission_capabilities:{profiles:['default','workspace','auto','full'],labels:{default:'默认',workspace:'工作区',auto:'自动审核',full:'完全访问'},note:'结果需复审'}};
test('full access requires fresh consent and changing profile clears it',()=>{
 const h=harness(),p=h.create({agent}),all=h.all(p.node),select=all.find(n=>n.id==='assign-permission'),ack=all.find(n=>n.id==='full-access-ack');
 assert.equal(p.valid(),true);select.value='full';select.listeners.change();assert.equal(p.valid(),false);
 ack.checked=true;ack.listeners.change();assert.equal(p.valid(),true);assert.equal(p.value().full_access_ack,true);
 select.value='workspace';select.listeners.change();select.value='full';select.listeners.change();assert.equal(p.valid(),false);
 p.refresh(agent,false,'full');assert.equal(p.valid(),false);
});
test('text tasks and unsupported agents cannot choose elevated profiles',()=>{
 const h=harness(),p=h.create({agent,answer:true,profile:'full'}),select=h.all(p.node).find(n=>n.id==='assign-permission');
 assert.deepEqual(Array.from(select.options,x=>x.value),['default','workspace']);assert.equal(p.value().permission_profile,'default');
 p.refresh({permission_capabilities:{profiles:['default']}},false,'full');assert.equal(select.disabled,true);assert.equal(p.value().permission_profile,'default');
});
test('disabled current run never appears hot-switchable',()=>{
 const h=harness(),p=h.create({agent,disabled:true});assert.equal(h.all(p.node).find(n=>n.id==='assign-permission').disabled,true);assert.match(p.node.textContent,/不会热切换/);
});
