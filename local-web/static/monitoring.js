/* The local server owns the schedule. This client only changes it on an explicit click. */
(() => {
  'use strict';

  const toggle=$('monitorToggle'), interval=$('monitorInterval'), statusLine=$('monitorStatus');
  const clock=new Intl.DateTimeFormat('ko-KR',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
  let state={active:false}, appliedJob=null, startText='', firstView=false, stagedJob=null;
  let timer, generation=0, checking=false, changing=false, initialized=false, notice='', actionError='';
  function time(value) {
    const millis=Date.parse(value);
    return Number.isFinite(millis) ? clock.format(millis) : '';
  }
  function syncState() {
    const active=!!state.active;
    const label=active?'모니터링 중지':'모니터링 시작';
    toggle.disabled=!ui.token || changing || (!active && ui.busy);
    toggle.setAttribute('aria-label',label);
    toggle.setAttribute('aria-pressed',String(active));
    toggle.title='조회 대상 전체 '+label;
    $('monitorStartLabel').hidden=active;
    $('monitorStopLabel').hidden=!active;
    interval.disabled=changing || active;
    if (active) interval.value=state.intervalMinutes;
    const info=[];
    if (active) {
      const scope=Number.isInteger(state.portCount) ? '전체 '+state.portCount+'포트' : (state.jobId ? '전체 포트 수 확인 중' : '전체 포트 수 미확인');
      info.push(scope+' · '+state.intervalMinutes+'분 간격');
      if (state.jobId) info.push('조회 중 · 수동 작업은 예약 가능');
      if (state.lastSuccess) info.push('마지막 성공 '+time(state.lastSuccess));
      if (!state.jobId && state.nextAt) info.push('다음 '+time(state.nextAt));
      if (state.error) info.push((state.suspended?'모니터링 일시 중지':'갱신 지연')+' · '+state.error);
    }
    if (actionError || notice) info.push(actionError || notice);
    const message=info.join(' · ');
    if (statusLine.textContent!==message) statusLine.textContent=message;
    statusLine.hidden=!message;
    statusLine.dataset.error=String(Boolean(actionError || notice || state.error));
  }
  function accept(next) {
    if (next.monitorId!==state.monitorId) {
      appliedJob=stagedJob=null;
      firstView=true;
      startText='';
    }
    const changedReading=!!state.jobId!==!!next.jobId;
    state=next;
    if (changedReading && window.manualQueue) updateControlTarget();
    notice='';
    syncState();
  }
  function wake(wait=0) {
    clearTimeout(timer);
    if (initialized && ui.token) timer=setTimeout(check,wait);
  }
  function applyWorkspace(workspace) {
    const query=workspace.query, groups=workspace.groups;
    if (!Array.isArray(query?.rows) || !Array.isArray(groups) || !groups.length) return false;
    const same=window.portTools.matchesInventory(groups);
    const sameScope=same || window.portTools.matchesInventory(groups,true);
    if (!same) {
      if (!sameScope) clearPorts();
      window.portTools.restoreInventory(groups,workspace.targetText ?? (startText || undefined),true);
    }
    const preserve=!!ui.liveId && sameScope;
    receivePorts(query,preserve,new Set(query.rows.map(portKey)),preserve);
    const success=query.rows.some(portObserved);
    const first=firstView;
    firstView=false;
    if (first) monitor()?.setPresentation('matrix');
    const matrixFirst=$('matrixWorkspace').closest('[data-page]').classList.contains('matrix-first');
    if (!success || first || (!matrixFirst && $('queryTargets').value===startText)) setQueryWorkspace(success);
    return true;
  }
  async function check() {
    if (!ui.token || !initialized) return;
    if (checking || changing || ui.busy) { wake(1000); return; }
    checking=true;
    const ticket=generation;
    try {
      const next=await api('/api/monitor');
      if (ticket!==generation || !ui.token) return;
      accept(next);
      if (next.session && (ui.session.active!==next.session.active || ui.session.username!==next.session.username)) {
        const changedAccount=ui.session.active && ui.session.username!==next.session.username;
        ui.session=next.session;
        if (!ui.session.active || changedAccount) window.portTools.clearSshObservations();
        sync();
      }
      if (next.active && firstView && next.jobId && !next.lastJobId &&
          (next.jobId!==stagedJob || window.portTools.awaitingDiscovery())) {
        const job=await api('/api/jobs/'+encodeURIComponent(next.jobId));
        if (ticket!==generation || ui.busy) return;
        if (job.progress?.targets?.length) {
          const text=job.progress.targetText || startText;
          if (text && !$('queryTargets').value) $('queryTargets').value=text;
          if (next.jobId!==stagedJob) window.portTools.showStartingMatrix(job.progress.targets,text);
          else if (!window.portTools.matchesInventory(job.progress.targets)) {
            window.portTools.restoreInventory(job.progress.targets,text,true);
          }
          showDiscoveryErrors(job.progress.discoveryErrors);
          stagedJob=next.jobId;
        }
      }
      if (!next.active || !next.lastJobId || next.lastJobId===appliedJob || next.jobId) return;
      const workspace=await api('/api/workspace');
      if (workspace.busy) return;
      // A manual query, stop or target replacement can finish while retrieving the workspace.
      const latest=await api('/api/monitor');
      if (ticket!==generation || ui.busy || !ui.token) return;
      accept(latest);
      if (!latest.active || latest.monitorId!==next.monitorId || latest.lastJobId!==next.lastJobId || latest.jobId) return;
      if (applyWorkspace(workspace)) appliedJob=latest.lastJobId;
      else if (latest.error) {
        setQueryWorkspace(false);
        appliedJob=latest.lastJobId;
      }
    } catch (error) {
      if (ticket===generation && state.active) {
        notice='모니터링 상태 연결 지연 · '+error.message;
        syncState();
      }
    } finally {
      checking=false;
      wake(ticket!==generation ? 300 : state.active ? 1500 : 10000);
    }
  }
  async function change(start,targets,minutes) {
    if (changing || !ui.token) return;
    const stoppedInitial=!start && firstView;
    generation++;
    changing=true;
    actionError='';
    syncState();
    try {
      const next=start ? await api('/api/monitor/start',{targets,intervalMinutes:minutes})
                       : await api('/api/monitor/stop',{monitorId:state.monitorId});
      accept(next);
      if (start) {
        startText=targets;
        if (next.groups?.length && !ui.busy) {
          window.portTools.showStartingMatrix(next.groups,targets);
          stagedJob=next.jobId;
        }
        if (!ui.busy) notify('');
      } else if (stoppedInitial && !ui.rows.some(portObserved)) setQueryWorkspace(false);
    } catch (error) {
      actionError=error.message;
    } finally {
      changing=false;
      syncState();
      wake(300);
    }
  }
  toggle.onclick=()=>{
    if (state.active) return change(false);
    if (!interval.reportValidity()) return;
    const input=$('queryTargets'), targets=input.value;
    if (!targets.trim()) {
      setQueryExpanded(true);
      input.value='';
      input.reportValidity();
      return;
    }
    const minutes=interval.valueAsNumber;
    withSshAccount(()=>change(true,targets,minutes));
  };
  for (const input of [interval,$('queryTargets')]) input.addEventListener('input',()=>{
    if (actionError) { actionError=''; syncState(); }
  });
  function ready() {
    initialized=true;
    wake();
  }
  window.powerMonitoring={
    sync:syncState,connect:snapshot=>{if (snapshot) accept(snapshot);},
    ready,wake,isReading:()=>!!state.jobId,foregroundStarted:()=>{generation++;},
  };
  document.addEventListener('visibilitychange',()=>{if (!document.hidden) wake();});
  window.addEventListener('aruba-server-stopped',()=>{
    generation++;
    initialized=false;
    clearTimeout(timer);
    accept({active:false});
  });
  if (ui.appReady) {
    if (ui.bootMonitor) accept(ui.bootMonitor);
    ready();
  }
  syncState();
})();
