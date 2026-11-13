/* Phone input editor: drafts are data; only explicit submit queues a round. */
(() => {
  'use strict';
  const node=(tag,css,text)=>{const n=document.createElement(tag);n.className=css||'';if(text!==undefined)n.textContent=String(text);return n;};
  function render(task,draft={},options={}){
    const info=task.continuation||{},section=node('section','detail-section task-conversation');
    section.append(node('h3','','补充与追问'));
    if(options.historical){section.append(node('p','field-help','正在查看历史轮次。返回当前结果后，再补充或追问。'));return section;}
    if(!info.available){section.append(node('p','field-help','本轮处理结束后，可在这里补充或追问。'));return section;}
    section.append(node('p','field-help',`继续交给 ${info.executor} · ${info.settings?.model||'原模型'}${info.settings?.effort?' · '+info.settings.effort:''}。每次提交生成新轮次，保留旧答案与复审记录。`));
    if(info.explanation_only)section.append(node('p','notice','原任务已完成，本次只解释或补充答案，不会改变滴答完成状态。'));
    section.append(node('p','field-help','本次追问为纯文本交付，固定只读；不继承上一轮完全访问权限，答案仍由你复审。'));
    const questions=Array.isArray(info.questions)?info.questions:[],fields=new Map(),message=node('textarea');
    message.id='followup-message';message.maxLength=8000;message.rows=4;message.value=draft.message||'';
    message.placeholder=info.action==='respond_input'?'补充其他要求（可选）':'例如：预算改为350元，请更新计算，并用一句话说明变化。';
    const label=node('label','field');label.htmlFor=message.id;label.append(node('span','',info.action==='respond_input'?'补充说明（可选）':'补充要求或追问'),message);section.append(label);
    for(const q of questions){
      const field=node('label','field'),input=node('textarea');input.id='followup-answer-'+q.id;input.rows=2;input.maxLength=4000;input.value=draft.answers?.[q.id]||'';field.htmlFor=input.id;
      field.append(node('span','',q.prompt+(q.required?'（必填）':'')),input);
      if(q.options?.length)field.append(node('span','field-help','可参考：'+q.options.join(' / ')+'；也可以自行填写。'));
      section.append(field);fields.set(q.id,input);
    }
    section.append(node('p','field-help','请勿填写密码、验证码或密钥。保存草稿不会开始执行。'));
    const values=()=>({revision:draft.revision||0,message:message.value,answers:Object.fromEntries([...fields].map(([key,input])=>[key,input.value]))});
    const actions=node('div','detail-actions'),save=node('button','button secondary','保存草稿'),send=node('button','button primary',info.action==='respond_input'?'补充并继续':info.explanation_only?'提交解释追问':'提交追问并继续');
    save.type=send.type='button';
    const blocked=Boolean(options.busy||options.disconnected||!info.ready);
    function update(){
      const value=values();const requiredMissing=questions.some(q=>q.required&&!value.answers[q.id]?.trim());
      send.disabled=blocked||requiredMissing||!(value.message.trim()||Object.values(value.answers).some(v=>v.trim()))||(info.action==='respond_input'&&!questions.length);
      save.disabled=Boolean(options.busy||options.disconnected);
    }
    for(const field of [message,...fields.values()]){
      field.disabled=Boolean(options.busy);field.addEventListener('input',()=>{update();options.onChange?.(values());});
    }
    save.addEventListener('click',()=>{if(!save.disabled)options.onSave?.(values());});
    send.addEventListener('click',()=>{if(!send.disabled)options.onSubmit?.(values());});
    update();actions.append(save,send);section.append(actions);
    if(draft.revision)section.append(node('p','field-help',`已保存草稿 · 版本 ${draft.revision}`));
    if(!info.ready)section.append(node('p','notice','原执行器暂不可用，恢复后再继续；草稿仍可保存。'));
    if(info.action==='respond_input'&&!questions.length)section.append(node('p','notice','当前需要在官方入口完成授权，请勿在聊天中提交凭据。'));
    return section;
  }
  function roundInput(run){
    if(!run?.round_input)return null;
    const input=run.round_input,section=node('section','detail-section round-input');section.append(node('h3','',`第 ${run.round_number||run.input_version} 轮 · 你的补充`));
    if(input.message)section.append(node('p','description-text',input.message));
    const prompts=Object.fromEntries((input.questions||[]).map(q=>[q.id,q.prompt]));
    for(const [key,value] of Object.entries(input.answers||{}))section.append(node('h4','',prompts[key]||key),node('p','description-text',value));
    return section;
  }
  globalThis.TaskConversation={render,roundInput};
})();
