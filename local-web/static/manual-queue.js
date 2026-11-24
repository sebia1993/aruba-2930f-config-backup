'use strict';
(() => {
  const panel=$('manualQueue'), details=$('manualQueueDetails'), list=$('manualQueueItems');
  const notice=$('manualQueueNotice'), receiptActions=$('manualReceiptActions');
  const storageKey='aruba.manualReceipt.v1';
  const receiptId=id=>typeof id==='string' && /^[0-9a-f]{32}$/.test(id);
  const receiptStates=new Set(['queued','running','done','failed','cancelled','uncertain','not_accepted']);
  let outstanding=null, receiptBusy=false;
  try {
    const saved=JSON.parse(sessionStorage.getItem(storageKey) || 'null');
    if (receiptId(saved?.id)) outstanding={id:saved.id};
  } catch { /* Browser storage is optional; keep the in-memory lock. */ }
  const labels={queued:'예약 대기',running:'실행 중',done:'완료',failed:'실패',cancelled:'취소됨',uncertain:'결과 미확인',not_accepted:'미접수 확정'};
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
      li.append(elem('span','대기 10분을 넘기면 자동 취소됩니다.','queue-message'));
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
    panel.hidden=!outstanding && !items.some(item=>item.wasQueued) && notice.hidden;
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
      if (ticket===requestNumber && !panel.hidden && !outstanding) message(error.message);
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
  function remember(id) {
    outstanding=id ? {id} : null;
    try {
      if (id) sessionStorage.setItem(storageKey,JSON.stringify(outstanding));
      else sessionStorage.removeItem(storageKey);
    } catch { /* An unavailable store must not permit another in-memory submission. */ }
  }
  function validReceipt(receipt, recovered=false) {
    return receiptId(receipt?.id) && receiptStates.has(receipt.state) &&
      (!recovered || (typeof receipt.accepted==='boolean' &&
        (receipt.accepted ? receipt.state!=='not_accepted' : receipt.state==='not_accepted')));
  }
  function receiptNotice(text) {
    panel.hidden=false;
    details.open=true;
    message(text);
    receiptActions.hidden=!outstanding || receiptBusy;
    receiptActions.replaceChildren();
    if (outstanding && !receiptBusy) {
      receiptActions.append(
        button('접수 상태 다시 확인',()=>resumeReceipt(false)),
        button('미접수 요청 종료',()=>resumeReceipt(true)),
        elem('span','기록이 없을 때만 이전 요청을 종료합니다. 이미 접수된 작업은 유지됩니다.','queue-message')
      );
    }
  }
  function pendingError() {
    const error=new Error('이전 요청의 접수 상태를 확인 중입니다. 수동 작업 대기열에서 먼저 확인하세요.');
    error.receiptPending=true;
    return error;
  }
  function completeReceipt(receipt,body=null) {
    ++requestNumber;
    if (body?.responseKey && receipt.id===body.requestId) keys.set(receipt.id,body.responseKey);
    if (receipt.recovered) ++workspaceGeneration;
    remember(null);
    receiptActions.hidden=true;
    receiptActions.replaceChildren();
    panel.hidden=!items.some(item=>item.wasQueued) && notice.hidden;
    wake();
    return receipt;
  }
  function receiptErrorReason(error) {
    if (error?.code==='receipt_storage_unavailable') return '작업 이력 DB 오류 (HTTP 503)';
    if (error?.code==='receipt_check_required') return '접수 처리 오류 (HTTP 503)';
    if (error?.code==='receipt_not_found') return '접수 기록 없음 (늦게 도착할 수 있음)';
    return error?.status?'HTTP '+error.status:'서버 응답 오류';
  }
  async function lookupReceipt(id,initialError=null) {
    const path='/api/manual-receipts/'+encodeURIComponent(id);
    let lastError;
    for (let attempt=1; attempt<=5; ++attempt) {
      receiptNotice('접수 상태 자동 확인 중 ('+attempt+'/5) · 요청 ID '+id);
      try {
        const receipt=await api(path,undefined,undefined,6000);
        if (!validReceipt(receipt,true)) throw new Error('접수 응답 형식 오류');
        return {...receipt,recovered:true};
      } catch (error) {
        lastError=error;
        if (attempt<5) await delay(250*2**(attempt-1));
      }
    }
    const reason=receiptErrorReason(lastError);
    const first=initialError ? receiptErrorReason(initialError) : '';
    const context=first && first!==reason ? '최초 접수: '+first+' · ' : '';
    const error=new Error(context+reason+' · 접수 미확인 · 요청 ID '+id);
    error.receiptPending=true;
    throw error;
  }
  async function resumeReceipt(closeMissing=false) {
    if (!outstanding || receiptBusy) return;
    const id=outstanding.id;
    receiptBusy=true;
    try {
      receiptNotice('접수 상태 확인 중 · 요청 ID '+id);
      let receipt;
      if (closeMissing) {
        try {
          receipt=await api('/api/manual-receipts/'+encodeURIComponent(id)+'/close',{},undefined,10000);
          if (!validReceipt(receipt,true)) throw new Error('접수 응답 형식 오류');
          receipt={...receipt,recovered:true};
        } catch {
          // Closing can also lose its acknowledgement. Read only; never replay a write.
          receipt=await lookupReceipt(id);
        }
      } else receipt=await lookupReceipt(id);
      announce(completeReceipt(receipt));
    } catch (error) {
      receiptBusy=false;
      receiptNotice(error.message+' · 기존 요청은 자동 재전송하지 않습니다.');
    } finally {
      receiptBusy=false;
    }
  }
  async function submit(action,body) {
    if (outstanding || receiptBusy) {
      if (outstanding && !receiptBusy) receiptNotice('이전 요청의 접수 상태를 먼저 확인하세요. · 요청 ID '+outstanding.id);
      throw pendingError();
    }
    if (!receiptId(body.requestId)) throw new Error('작업 요청 식별자가 올바르지 않습니다.');
    remember(body.requestId);
    receiptBusy=true;
    let submitted;
    try {
      try {
        submitted=await api('/api/jobs',{action,body},undefined,10000);
        if (!validReceipt(submitted)) throw new Error('접수 응답 형식 오류');
      } catch (error) {
        // Only an explicit JSON rejection can prove the ordinary submission failed.
        if (error.serverRejection && [400,403,409,413,415].includes(error.status)) {
          remember(null);
          receiptActions.hidden=true;
          message(error.message);
          throw error;
        }
        submitted=await lookupReceipt(body.requestId,error);
      }
      if (!submitted.recovered) message('');
      return completeReceipt(submitted,body);
    } catch (error) {
      if (outstanding) {
        receiptBusy=false;
        receiptNotice(error.message+' · 기존 요청은 자동 재전송하지 않습니다.');
        error.receiptPending=true;
      }
      throw error;
    } finally {
      receiptBusy=false;
    }
  }
  function announce(submitted) {
    panel.hidden=false;
    details.open=true;
    const text=submitted.recovered
      ? (submitted.accepted?'접수 확인 완료 · ':'')+(labels[submitted.state] || submitted.state)+
        (submitted.message?' · '+submitted.message:'')+
        (submitted.historyOnly && submitted.accepted?' · 상세 결과는 작업 이력 또는 포트 재조회에서 확인하세요.':'')
      : submitted.state==='queued'?'수동 작업이 예약되었습니다.':'작업의 현재 상태를 확인하세요.';
    message(text);
    if (!ui.jobId) notify(text,submitted.state==='uncertain' || submitted.state==='failed');
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
      else if (!error.receiptPending) { panel.hidden=false; details.open=true; message(error.message); }
    }
    return {background:true};
  }
  function ready() {
    initialized=true;
    wake();
    if (outstanding) void resumeReceipt(false);
  }
  document.addEventListener('visibilitychange',()=>{ if (!document.hidden) wake(); });
  window.addEventListener('aruba-server-stopped',()=>{initialized=false; clearTimeout(timer); keys.clear();});
  window.manualQueue={submit,announce,submitBackground,showConflict,refresh,ready,hasWork};
})();
