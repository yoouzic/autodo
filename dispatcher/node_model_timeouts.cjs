/* DSH process only. Keep the current (possibly proxy-aware) dispatcher, TLS,
   authentication, signals, and routes. Change only model HTTP wait timers. */
'use strict';
const {createRequire}=require('node:module');
const milliseconds=Number(process.env.DISPATCHER_STREAM_IDLE_MS);
if(!Number.isInteger(milliseconds)||milliseconds<=0||milliseconds>2147483647)throw new Error('Invalid dispatcher model HTTP timeout');
const vendorRequire=createRequire(process.argv[1]);
const {getGlobalDispatcher,Dispatcher1Wrapper}=vendorRequire('undici');
const originalFetch=globalThis.fetch;
globalThis.fetch=function(input,init){
  let url;
  try{url=new URL(typeof input==='string'?input:input instanceof URL?input.href:input.url);}catch{return originalFetch(input,init);}
  if(!/\/(messages|chat\/completions|responses)\/?$/.test(url.pathname))return originalFetch(input,init);
  // Resolve on every call, after the vendor has installed its network policy.
  let delegate=init?.dispatcher;
  if(!delegate){
    delegate=getGlobalDispatcher();
    // DSH currently ships Undici 8 while Node 24 fetch speaks dispatcher v1.
    // Use the vendor's public compatibility wrapper, never internal symbols.
    if(Number(process.versions?.undici?.split('.')[0]||7)<8&&typeof Dispatcher1Wrapper==='function')delegate=new Dispatcher1Wrapper(delegate);
  }
  const dispatcher={dispatch(options,handler){
    return delegate.dispatch({...options,headersTimeout:milliseconds,bodyTimeout:milliseconds},handler);
  }};
  return originalFetch(input,{...init,dispatcher});
};
