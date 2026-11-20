// Case-independent operator queue. Scheduling never selects a dialogue intent or places a real call.
export function setupAgenda({$,element,request,caseFor,openCallQuality,call,locked,voiceReady}) {
  let items=[], editing=null, serial=0, saving=false;
  const stamp=(s)=>new Date(s*1000).toLocaleString('zh-CN',{hour12:false});
  const state=t=>t.status==='in_progress'?'busy':t.scheduled_at==null?'open':t.scheduled_at<=Date.now()/1000?'due':'planned';
  const labels={busy:'处理中',open:'未排期',due:'已到期',planned:'未到期'};
  const note=t=>{$('agenda-feedback').textContent=t;};
  function button(name,fn,off=false){const b=element('button','secondary',name); b.type='button';b.disabled=off;b.onclick=async()=>{try{await fn()}catch(e){note(e.message)}};return b;}
  function render(){const filter=$('agenda-filter').value;const counts={busy:0,open:0,due:0,planned:0};items.forEach(t=>counts[state(t)]++);
    $('agenda-summary').textContent=`共 ${items.length} 通 · 已到期 ${counts.due} · 未排期 ${counts.open} · 未到期 ${counts.planned} · 处理中 ${counts.busy}`;
    const nodes=items.filter(t=>filter==='all'||filter===state(t)).map(t=>{const row=element('div','agenda-row');row.dataset.ticket=t.id;
      const group=element('div');group.append(element('strong','',`${t.case_id} · ${caseFor(t.case_id)?.merchant || '商户'}`),element('div','subtle',t.id));
      const status=state(t), info=element('div');info.append(element('span','chip '+(status==='due'?'ok':'neutral'),labels[status]),element('div','subtle',t.scheduled_at==null?'未指定联系时间':stamp(t.scheduled_at)));
      const actions=element('div','agenda-actions');const busy=locked()||status==='busy'; const blocked=busy||status==='planned';
      actions.append(button('原通话',()=>openCallQuality(t.session_id)),button(t.scheduled_at==null?'安排时间':'改期',()=>open(t),busy));
      if(t.scheduled_at!=null)actions.append(button('取消排期',async()=>{if(!window.confirm('取消约定时间？工单仍保留在待办中。'))return;await save(t,null);},busy));
      actions.append(button('文字回联',()=>call(t,false),blocked),button('语音回联',()=>call(t,true),blocked||!voiceReady()));row.append(group,info,actions);return row;});
    $('agenda-items').replaceChildren(...(nodes.length?nodes:[element('p','agenda-note',items.length?'此筛选下没有工单。':'目前没有待回联工单。完成一通结果为「已登记回电」的模拟通话后，会出现在这里。')]));}
  async function refresh(){const n=++serial;try{const data=await request('/api/callback-agenda');if(n!==serial)return;items=data.items;render();}catch(e){if(n===serial){items=[];render();note(e.message);}}}
  function localDate(date){const shifted=new Date(date.getTime()-date.getTimezoneOffset()*60000);return shifted.toISOString().slice(0,16);}
  function open(t){editing=t;$('agenda-target').textContent=`${t.case_id} · ${t.id}`;$('agenda-error').textContent='';const next=new Date((Math.floor(Date.now()/60000)+1)*60000);$('agenda-time').min=localDate(next);$('agenda-time').max=localDate(new Date(Date.now()+365*86400000));$('agenda-time').value=t.scheduled_at && t.scheduled_at*1000>=next ? localDate(new Date(t.scheduled_at*1000)):'';$('agenda-dialog').showModal();
    const hint=$('agenda-utterance');hint.textContent='正在读取原通话…';
    request(`/api/calls/${encodeURIComponent(t.session_id)}/quality`).then(report=>{
      if(editing!==t)return;const last=[...(report.transcript??[])].reverse().find(line=>line.role==='user');
      hint.textContent=last?`商家最后一句（转写）：${last.text}`:'没有商家发言记录，请勿推定预约时间。';
    }).catch(()=>{if(editing===t)hint.textContent='未能读取转写，请核对原记录后再排期。';});}
  async function save(t,value){try{await request(`/api/work-items/${encodeURIComponent(t.id)}/schedule`,{scheduled_at:value,version:t.version});note(value==null?'已取消预约时间，回电工单仍保留。':`已保存回电时间：${stamp(value)}`);}finally{await refresh();}}
  $('agenda-form').onsubmit=async event=>{event.preventDefault();if(!editing||saving)return;const ts=Math.floor(new Date($('agenda-time').value).getTime()/1000);if(!Number.isFinite(ts))return;
    saving=true;$('agenda-save').disabled=true;try{await save(editing,ts);$('agenda-dialog').close();}catch(e){$('agenda-error').textContent=e.message+' 请返回并重新选择工单。';}finally{saving=false;$('agenda-save').disabled=false;}};
  $('agenda-cancel').onclick=()=>$('agenda-dialog').close();$('agenda-filter').onchange=render;$('agenda-refresh').onclick=()=>{note('');refresh();};
  setInterval(()=>{if(!$('agenda').hidden && !$('agenda-dialog').open)refresh();},30000);
  return {refresh};
}
