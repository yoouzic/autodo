/* Render untrusted answers exclusively as DOM text. No HTML/remote images. */
(() => {
  'use strict';
  const node=(tag,css,text)=>{const n=document.createElement(tag);n.className=css||'';if(text!==undefined)n.textContent=String(text);return n;};
  function rich(tag,text){
    const out=node(tag);
    for(const part of String(text).split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/g)){
      if(part.startsWith('**')&&part.endsWith('**')&&part.length>4)out.append(node('strong','',part.slice(2,-2)));
      else if(part.startsWith('`')&&part.endsWith('`')&&part.length>2)out.append(node('code','',part.slice(1,-1)));
      else if(part)out.append(node('span','',part));
    }
    return out;
  }
  function body(text){
    const out=node('div','answer-body');
    // Small, deliberately non-executable Markdown subset; links and HTML
    // remain readable literal text. Lists and headings improve phone scanning.
    let list=null,code=null;
    const lines=String(text||'').split('\n');
    const cells=line=>line.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(x=>x.trim());
    for(let i=0;i<lines.length;i++){
      const line=lines[i];
      if(/^```/.test(line)){if(code){code=null;}else{code=node('pre','answer-code');out.append(code);}list=null;continue;}
      if(code){code.textContent+=(code.textContent?'\n':'')+line;continue;}
      if(line.includes('|')&&i+1<lines.length&&cells(lines[i+1]).length>1&&cells(lines[i+1]).every(x=>/^:?-{3,}:?$/.test(x))){
        const scroll=node('div','answer-table-scroll'),table=node('table','answer-table'),thead=node('thead'),head=node('tr');
        scroll.setAttribute('tabindex','0');scroll.setAttribute('aria-label','结果表格，可横向滚动');
        const columns=cells(line);for(const value of columns)head.append(rich('th',value));thead.append(head);table.append(thead);
        const tbody=node('tbody');i++;
        while(i+1<lines.length&&lines[i+1].includes('|')&&cells(lines[i+1]).length===columns.length){
          const row=node('tr');for(const value of cells(lines[++i]))row.append(rich('td',value));tbody.append(row);
        }
        table.append(tbody);scroll.append(table);out.append(scroll);list=null;continue;
      }
      const heading=line.match(/^#{1,6}\s+(.+)$/),item=line.match(/^\s*(?:[-*]|\d+[.)])\s+(.+)$/);
      if(item){if(!list){list=node('ul','answer-list');out.append(list);}list.append(rich('li',item[1]));continue;}
      list=null;if(heading)out.append(rich('h4',heading[1]));else if(/^>\s?/.test(line))out.append(rich('blockquote',line.replace(/^>\s?/,'')));else if(line.trim())out.append(rich('p',line));
    }
    return out;
  }
  function render(delivery,meta={}){
    const card=node('section','detail-section result-card');card.setAttribute('aria-label',meta.historical?'历史结果':'本次结果');
    card.append(node('span','result-eyebrow',(meta.historical?'历史结果':'本次结果')+(meta.round?' · 第 '+meta.round+' 轮':'')+' · 纯文本交付'));
    card.append(node('h3','result-conclusion',delivery.summary||'结果尚未返回'));
    if(meta.executor)card.append(node('p','field-help',`${meta.executor}${meta.model?' · 请求模型 '+meta.model:''}${meta.effort?' · '+meta.effort:''}${meta.time?' · '+meta.time:''}`));
    if(delivery.status!=='complete')card.append(node('p','notice',delivery.needs_input||'本轮尚未满足任务要求，请查看限制与运行记录。'));
    if(delivery.answer_markdown)card.append(body(delivery.answer_markdown));
    else card.append(node('p','field-help','正在读取完整答案…'));
    const changes=delivery.file_changes||{};
    card.append(node('p','answer-change-note',changes.status==='verified'&&changes.added===0&&changes.modified===0&&changes.deleted===0?'已核验：未改动业务文件':'文件变更未核验'));
    const refs=Array.isArray(delivery.evidence_refs)?delivery.evidence_refs:[],limits=Array.isArray(delivery.limitations)?delivery.limitations:[];
    const details=node('details','answer-evidence');details.append(node('summary','details-toggle','依据与限制'));
    for(const ref of refs){details.append(node('h4','',`${ref.kind==='task_input'?'用户提供':'执行器自述'} · ${ref.label}`),node('p','',ref.detail));}
    if(!refs.length)details.append(node('p','field-help','未提供外部查询依据。'));
    for(const limit of limits)details.append(node('p','',limit));
    details.append(node('p','field-help','自动检查核对给定内容，不代表已查询外部账户或服务。'));card.append(details);
    if(delivery.answer_markdown){
      const copy=node('button','button secondary','复制答案');copy.type='button';
      copy.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(delivery.answer_markdown);copy.textContent='已复制';}catch{copy.textContent='请长按答案复制';}});
      card.append(copy);
    }
    return card;
  }
  globalThis.DispatcherResultCard={render};
})();
