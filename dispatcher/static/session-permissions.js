/* Per-launch consent only. Never inherit full access from a previous run. */
(() => {
  'use strict';
  const make=(tag,css,text)=>{const n=document.createElement(tag);n.className=css||'';if(text!==undefined)n.textContent=text;return n;};
  function create({agent,profile='default',disabled=false,answer=false,onChange=()=>{}}={}){
    const node=make('div','session-permissions'),label=make('label','field'),select=make('select');
    select.id='assign-permission';label.htmlFor=select.id;label.append(make('span','','本次工具权限'),select);
    const info=make('p','field-help'),confirm=make('label','permission-confirm'),ack=make('input');
    ack.type='checkbox';ack.id='full-access-ack';confirm.htmlFor=ack.id;
    confirm.append(ack,make('span','','我确认仅为本次运行开放普通账户可访问的文件与命令，可能修改工作区外的内容。结果仍由我复审。'));
    node.append(label,info,confirm);
    function changed(){
      confirm.hidden=select.value!=='full';
      const caps=agent?.permission_capabilities;
      info.textContent=(answer?'纯文本交付固定只读。':caps?.descriptions?.[select.value]||caps?.note||'选择具体执行器后可查看支持的权限。')+' 已运行的进程不会热切换；更改在本次重新分配时生效。';
      onChange();
    }
    function refresh(nextAgent,nextAnswer=false,preferred='default'){
      agent=nextAgent;answer=nextAnswer;ack.checked=false;select.replaceChildren();
      const caps=agent?.permission_capabilities,profiles=caps?.profiles||['default'];
      for(const value of profiles){
        if(answer&&['auto','full'].includes(value))continue;
        select.add(new Option(caps?.labels?.[value]||'沿用安全默认',value));
      }
      select.value=Array.from(select.options).some(o=>o.value===preferred)?preferred:'default';
      select.disabled=disabled||profiles.length===1;ack.disabled=disabled;
      changed();
    }
    select.addEventListener('change',()=>{ack.checked=false;changed();});ack.addEventListener('change',onChange);
    refresh(agent,answer,profile);
    return {node,refresh,value:()=>({permission_profile:select.value,full_access_ack:select.value==='full'&&ack.checked===true}),valid:()=>select.value!=='full'||ack.checked===true};
  }
  globalThis.SessionPermissions={create};
})();
