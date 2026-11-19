/* The local server owns the schedule. This client only changes it on an explicit click. */
(() => {
  'use strict';

  const toggle=$('monitorToggle'), interval=$('monitorInterval'), statusLine=$('monitorStatus');
  const clock=new Intl.DateTimeFormat('ko-KR',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
  let state={active:false}, appliedJob=null, startText='', firstView=false;
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
      info.push('전체 '+state.portCount+'포트 · '+state.intervalMinutes+'분 간격');
      if (state.jobId) info.push('조회 중');
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
      appliedJob=null;
      firstView=true;
      startText='';
    }
    state=next;
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
    if (!same) {
      clearPorts();
      window.portTools.restoreInventory(groups,startText || undefined,true);
    }
    const preserve=!!ui.liveId && same;
    receivePorts(query,preserve,new Set(query.rows.map(portKey)),preserve);
    if (firstView) {
      monitor()?.setPresentation('matrix');
      if (query.rows.some(row=>row.status==='ok')) {
        $('editQueryTargets').hidden=false;
        setQueryExpanded(false);
      }
      firstView=false;
    }
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
        if (!ui.session.active || changedAccount) clearPorts();
        sync();
      }
      if (!next.active || !next.lastJobId || next.lastJobId===appliedJob || next.jobId) return;
      const workspace=await api('/api/workspace');
      if (workspace.busy) return;
      // A manual query, stop or target replacement can finish while retrieving the workspace.
      const latest=await api('/api/monitor');
      if (ticket!==generation || ui.busy || !ui.token) return;
      accept(latest);
      if (!latest.active || latest.monitorId!==next.monitorId || latest.lastJobId!==next.lastJobId || latest.jobId) return;
      if (applyWorkspace(workspace) || latest.error) appliedJob=latest.lastJobId;
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
        if (!ui.busy) notify('');
      }
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
    const input=$('queryTargets'), targets=input.value.trim();
    if (!targets) {
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
    ready,wake,foregroundStarted:()=>{generation++;},
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
