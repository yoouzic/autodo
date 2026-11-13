const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../dispatcher/node_model_timeouts.cjs'),'utf8');
function harness(value='3600000'){
 let current,seen=[];
 const context={URL,process:{argv:['node','/vendor/dsh.js'],env:{DISPATCHER_STREAM_IDLE_MS:value}},fetch:(input,init)=>{seen.push({input,init});return Promise.resolve('ok');},require:name=>{
  assert.equal(name,'node:module');return {createRequire:entry=>{assert.equal(entry,'/vendor/dsh.js');return module=>{assert.equal(module,'undici');return {getGlobalDispatcher:()=>current};};}};
 }};
 vm.runInNewContext(source,context);
 return {context,seen,set:value=>current=value};
}
test('model timeout wrapper preserves routes, credentials, cancellation, and late proxy installation',async()=>{
 const h=harness(),forwarded=[];const first={dispatch:(opts,handler)=>{forwarded.push([1,opts,handler]);return true;}},second={dispatch:(opts,handler)=>{forwarded.push([2,opts,handler]);return true;}};
 h.set(first);const signal={},headers={Authorization:'PRIVATE_TEST_ONLY'},body='unchanged';
 await h.context.fetch('https://example.invalid/messages',{method:'POST',signal,headers,body});
 const call=h.seen[0];assert.equal(call.init.headers,headers);assert.equal(call.init.signal,signal);assert.equal(call.init.body,body);
 const handler={};call.init.dispatcher.dispatch({origin:'https://example.invalid',headersTimeout:300000,bodyTimeout:300000,path:'/messages'},handler);
 assert.equal(forwarded[0][1].headersTimeout,3600000);assert.equal(forwarded[0][1].bodyTimeout,3600000);assert.equal(forwarded[0][2],handler);
 h.set(second);await h.context.fetch('https://example.invalid/messages');h.seen[1].init.dispatcher.dispatch({},handler);assert.equal(forwarded[1][0],2);
 const custom={dispatch:opts=>{assert.equal(opts.bodyTimeout,3600000);return 'custom';}};
 await h.context.fetch('https://example.invalid/messages',{dispatcher:custom});assert.equal(h.seen[2].init.dispatcher.dispatch({},handler),'custom');
});
test('non-model calls are untouched and timer overflow is rejected',async()=>{
 const h=harness(),init={method:'GET',signal:{}};await h.context.fetch('https://example.invalid/models',init);assert.equal(h.seen[0].init,init);
 for(const v of ['0','-1','NaN','2147483648'])assert.throws(()=>harness(v),/Invalid/);
});
