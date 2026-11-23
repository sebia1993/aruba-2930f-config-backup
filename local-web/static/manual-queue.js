'use strict';
(() => {
  const panel=$('manualQueue'), details=$('manualQueueDetails'), list=$('manualQueueItems');
  const notice=$('manualQueueNotice');
  const labels={queued:'예약 대기',running:'실행 중',done:'완료',failed:'실패',cancelled:'취소됨',uncertain:'결과 미확인'};
  const pending=item=>item.state==='queued' || item.state==='running';
  const title=item=>operationNames[item.operation] || jobNames[item.action] || '수동 작업';
  const keys=new Map();
  let conflictIds=new Set();
  let items=[], displayed='', timer, initialized=false, requestNumber=0;
  let workspaceGeneration=0, appliedGeneration=0, refreshingWorkspace=false;

  function message(text) {
    notice.textContent=text;
    notice.hidden=!text;
  }
  function targetText(targets) {
    const groups=new Map();
    for (const target of targets || []) {
      const address=target.ip+':'+(target.sshPort || 22);
      if (!groups.has(address)) groups.set(address,[]);
      groups.get(address).push(target.port);
    }
    return [...groups].map(([address,ports])=>address+' · 포트 '+ports.join(', ')).join('\n');
  }
  function hasWork() {
    return !!window.powerMonitoring?.isReading() || !!ui.manualForeground || items.some(pending);
  }
  async function cancel(id) {
    try {
      const response=await api('/api/jobs/'+encodeURIComponent(id)+'/cancel',{queuedOnly:true});
      if (response.requested===false) message('작업 상태가 바뀌었습니다. 현재 상태를 확인하세요.');
      else message('취소를 요청했습니다.');
    } catch (error) { message(error.message); }
    await refresh();
  }
  function row(item) {
    const li=elem('li');
    li.dataset.jobId=item.id;
    li.dataset.state=item.state;
    if (conflictIds.has(item.id) && pending(item)) li.dataset.conflict='true';
    const heading=elem('strong',title(item));
    const state=elem('span',labels[item.state] || item.state,'queue-state');
    const scope=elem('span',targetText(item.targets),'queue-targets');
    li.append(heading,state,scope);
    if (item.message) li.append(elem('span',item.message,'queue-message'));
    if (item.state==='queued') {
      const action=button('예약 취소',()=>cancel(item.id));
      action.dataset.queueAction='cancel'; li.append(action);
    } else if (!pending(item)) {
      const action=button('결과 보기',()=>showResult(item));
      action.dataset.queueAction='result'; li.append(action);
    }
    return li;
  }
  function paint() {
    const snapshot=JSON.stringify([items.map(item=>[item.id,item.state,item.message,item.operation,item.targets]),[...conflictIds]]);
    if (snapshot===displayed) return;
    displayed=snapshot;
    const focus=document.activeElement?.closest('[data-queue-action]');
    const focusId=focus?.closest('[data-job-id]')?.dataset.jobId;
    const active=items.filter(pending), finished=items.filter(item=>!pending(item)).reverse();
    list.replaceChildren(...active.map(row),...finished.map(row));
    if (focusId) list.querySelector('[data-job-id="'+CSS.escape(focusId)+'"] button')?.focus({preventScroll:true});
    const waiting=items.filter(item=>item.state==='queued').length;
    const running=items.filter(item=>item.state==='running').length;
    $('manualQueueCount').textContent='대기 '+waiting+' · 실행 '+running;
    panel.hidden=!items.some(item=>item.wasQueued) && notice.hidden;
  }
  async function showResult(item) {
    try {
      const key=keys.get(item.id);
      const headers=key ? {'X-Cable-Response-Key':key} : undefined;
      const job=await api('/api/jobs/'+encodeURIComponent(item.id),undefined,headers);
      const result=job.result || {};
      const children=[elem('pre',targetText(item.targets),'queue-targets'),elem('p',labels[item.state] || item.state)];
      if (job.error || result.message || item.message) children.push(elem('p',job.error || result.message || item.message));
      if (item.action==='cable.test') {
        for (const reading of result.rows || []) children.push(window.cableTools.readingCard(reading,!key));
      } else {
        const reports=[...(result.results || []),...(result.blocked || []),...(result.skipped || [])];
        if (reports.length) children.push(simpleTable([['IP','ip'],['SSH 포트','sshPort'],['포트','port'],
          ['상태',r=>stateNames[r.status] || r.status || ''],['결과',r=>r.detail || r.message || '',true]],reports));
        const readings=result.powerRefresh?.readings || [];
        if (readings.length) children.push(simpleTable([['IP','ip'],['포트','port'],['자동 재조회 전력',r=>isMeasuredReading(r)?wattsText(r.watts):'미측정']],readings));
      }
      openDialog(title(item)+' 결과',children);
    } catch (error) { message(error.message); }
  }
  // Apply the current server snapshot, never an old reservation's observations.
  async function repair() {
    if (workspaceGeneration===appliedGeneration || refreshingWorkspace || ui.busy || !ui.appReady) return;
    const generation=workspaceGeneration;
    refreshingWorkspace=true;
    try {
      const workspace=await api('/api/workspace');
      if (workspace.busy || ui.busy || generation!==workspaceGeneration) return;
      if (workspace.query?.rows && window.portTools.matchesInventory(workspace.groups || [])) {
        const rows=workspace.query.rows;
        receivePorts(workspace.query,true,new Set(rows.map(portKey)),true);
      }
      appliedGeneration=generation;
    } catch { /* Try the authoritative workspace again after reconnecting. */ }
    finally { refreshingWorkspace=false; }
  }
  async function refresh() {
    const ticket=++requestNumber;
    try {
      const snapshot=await api('/api/manual-queue');
      if (ticket!==requestNumber || !Array.isArray(snapshot.items)) return items;
      const next=snapshot.items, previous=new Map(items.map(item=>[item.id,item]));
      if (next.some(item=>!pending(item) && item.id!==ui.jobId &&
          (pending(previous.get(item.id) || {}) || (initialized && item.wasQueued && !previous.has(item.id))))) {
        ++workspaceGeneration;
      }
      const wasBusy=hasWork();
      items=next;
      conflictIds=new Set(next.filter(item=>pending(item) && conflictIds.has(item.id)).map(item=>item.id));
      if (wasBusy!==hasWork()) updateControlTarget();
      for (const id of keys.keys()) if (!items.some(item=>item.id===id)) keys.delete(id);
      paint();
      await repair();
    } catch (error) {
      if (ticket===requestNumber && !panel.hidden) message(error.message);
    }
    return items;
  }
  function wake(wait=0) {
    clearTimeout(timer);
    if (initialized && ui.token) timer=setTimeout(async ()=>{
      await refresh();
      wake(hasWork() ? 750 : 2000);
    },wait);
  }
  async function submit(action,body) {
    let submitted;
    try { submitted=await api('/api/jobs',{action,body}); }
    catch (error) {
      if (error.status && error.status<500) throw error;
      // An HTTP failure can follow acceptance. Only read the original ID.
      const job=await api('/api/jobs/'+encodeURIComponent(body.requestId)).catch(()=>{
        throw new Error('접수 여부를 확인할 수 없습니다. 다시 요청하기 전에 수동 작업 대기열을 확인하세요.');
      });
      submitted={id:job.id,state:job.state,wasQueued:job.wasQueued || job.state==='queued'};
    }
    ++requestNumber;
    if (action==='cable.test' && submitted.id===body.requestId) keys.set(submitted.id,body.responseKey);
    wake();
    return submitted;
  }
  function announce(submitted) {
    panel.hidden=false;
    details.open=true;
    const text=submitted.state==='queued'?'수동 작업이 예약되었습니다.':'작업의 현재 상태를 확인하세요.';
    message(text);
    if (!ui.jobId) notify(text);
    refresh();
  }
  function showConflict(error) {
    conflictIds=new Set(error.conflicts);
    panel.hidden=false;
    details.open=true;
    message(error.message);
    if (!ui.jobId) notify(error.message,true);
    paint();
    refresh().then(()=>{
      const first=list.querySelector('[data-conflict="true"]');
      if (first) list.scrollTop+=first.getBoundingClientRect().top-list.getBoundingClientRect().top;
    });
  }
  async function submitBackground(action,body) {
    try {
      const submitted=await submit(action,body);
      announce(submitted);
      return {background:true,submitted};
    } catch (error) {
      if (error.conflicts?.length) showConflict(error);
      else { panel.hidden=false; details.open=true; message(error.message); }
    }
    return {background:true};
  }
  function ready() { initialized=true; wake(); }
  document.addEventListener('visibilitychange',()=>{ if (!document.hidden) wake(); });
  window.addEventListener('aruba-server-stopped',()=>{initialized=false; clearTimeout(timer); keys.clear();});
  window.manualQueue={submit,announce,submitBackground,showConflict,refresh,ready,hasWork};
})();
