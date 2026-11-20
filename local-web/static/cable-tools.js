/* Use the monitoring workspace's current targets and the shared SSH session. */
(() => {
  const page = document.querySelector('[data-page="cable"]');
  const results = $('cableResult');
  const pairLabels = {ok:'정상', open:'단선·미연결', short:'단락', 'inter-short':'페어 간 단락', impedance:'임피던스 이상'};
  jobNames['cable.test']='케이블 진단';
  const stages={clear:'결과 초기화',test:'테스트 실행',show:'결과 조회'};
  const pairOrder=['1-2','3-6','4-5','7-8'];
  const cards=new Map();
  let displayedScope, activeRun, resumedRun;
  const reloadKey='aruba2930f.cable.pendingReload';
  const portKey=row=>endpoint(row)+'|'+row.interface;
  const groupKeys=groups=>new Set(groups.flatMap(group=>group.interfaces.map(port=>endpoint(group)+'|'+port)));

  // Carry only a pending job ID across this tab's reload. The server keeps the
  // actual measurements and can tell whether a later inspection superseded it.
  window.addEventListener('pagehide',()=>{
    const run=activeRun || (ui.jobId===resumedRun?.id ? resumedRun : null);
    if (!run || !ui.busy || (ui.jobId && ui.jobId!==run.id) ||
        run.scope!==window.portTools.cableScope().targets) return;
    try { sessionStorage.setItem(reloadKey,JSON.stringify({id:run.id,at:Date.now()})); } catch {}
  });
  function jobOnOpen(bootstrap) {
    let id=bootstrap.busyJob;
    try {
      const saved=sessionStorage.getItem(reloadKey);
      sessionStorage.removeItem(reloadKey);
      if (!id && saved && performance.getEntriesByType('navigation')[0]?.type==='reload') {
        const pending=JSON.parse(saved);
        const age=Date.now()-pending.at;
        if (age>=0 && age<120000 && pending.id===bootstrap.lastCableJob) id=pending.id;
      }
    } catch {}
    if (id && id===bootstrap.lastCableJob) {
      resumedRun={id,scope:window.portTools.cableScope().targets};
    }
    return id;
  }

  function describe(groups) {
    return groups.map(group=>endpoint(group)+' · 포트 '+group.interfaces.join(', ')).join('\n');
  }
  function resetResults(message) {
    cards.clear();
    $('cableResultTitle').textContent='진단 결과';
    $('cableResultCount').textContent='';
    results.replaceChildren(elem('p',message,'empty'));
  }
  function syncCable(restoring=false) {
    if (page.hidden && !restoring) return;
    const scope=window.portTools.cableScope();
    if (displayedScope!==scope.targets) {
      displayedScope=scope.targets;
      resetResults('대상 포트를 선택하고 케이블 진단을 실행하세요.');
    }
    const count=scope.groups?.reduce((sum,g)=>sum+g.interfaces.length,0);
    $('cableScope').textContent=scope.label+(count ? ' · '+scope.groups.length+'대 / '+count+'포트' : '');
    $('cableTargets').textContent=(scope.groups ? describe(scope.groups) : scope.targets) ||
      '포트·PoE 관리에서 스위치 주소와 포트를 지정하세요.';
    $('cableTest').disabled=ui.busy || !ui.token || !scope.targets;
  }
  function pairTable(row,pairs) {
    const table=elem('table'), headings=table.createTHead().insertRow();
    table.setAttribute('aria-label',endpoint(row)+' / 포트 '+row.interface+' 페어별 측정값');
    for (const title of ['페어','상태','길이 / 장애 거리']) {
      const th=elem('th',title); th.scope='col'; headings.append(th);
    }
    const body=table.createTBody();
    for (const pair of pairOrder) {
      const tr=body.insertRow(), item=pairs.get(pair);
      tr.insertCell().textContent=pair;
      const cell=tr.insertCell();
      if (!item) {
        cell.textContent='값 미수신';
        cell.colSpan=2;
        cell.className='cable-missing';
        continue;
      }
      const state=String(item.status || ''), key=state.toLowerCase();
      cell.textContent=pairLabels[key] ? pairLabels[key]+' ('+state+')' : state;
      if (key!=='ok' && pairLabels[key]) cell.className='cable-fault';
      tr.insertCell().textContent=item.distance ?? '—';
    }
    return table;
  }
  function updateReading(row,resumed) {
    const key=portKey(row), previous=cards.get(key);
    const card=elem('section',undefined,'cable-reading');
    card.dataset.portKey=key;
    card.dataset.stage=row.stage || '';
    card.dataset.status=row.status;
    const pairs=new Map((row.pairs || []).map(item=>[item.pair,item]));
    const header=elem('header');
    const label=cableStatusLabels[row.status] || '측정 결과 미확인';
    const showPairs=pairs.size>0 || (row.stage==='show' && ['partial','failed'].includes(row.status));
    const count=showPairs && row.status!=='measured' ? ' · '+pairs.size+'/4페어 확인' : '';
    header.append(elem('strong',endpoint(row)+' / 포트 '+row.interface),elem('span',label+count));
    card.append(header);
    const stage=stages[row.stage];
    if (row.message) card.append(elem('p',(stage && row.status!=='measured' ? stage+' · ' : '')+row.message));
    if (showPairs) card.append(pairTable(row,pairs));
    if (!resumed && row.status!=='measured' && row.responseExcerpt) {
      const details=elem('details',undefined,'cable-response');
      details.append(elem('summary','이번 응답 보기'),elem('pre',row.responseExcerpt));
      details.open=!!previous?.querySelector('details')?.open;
      card.append(details);
    }
    const restoreFocus=previous?.querySelector('summary')===document.activeElement;
    if (previous) previous.replaceWith(card);
    else if (!cards.size) results.replaceChildren(card);
    else results.append(card);
    cards.set(key,card);
    if (restoreFocus) card.querySelector('summary')?.focus({preventScroll:true});
  }
  function matchesResumedScope(rows) {
    const scope=window.portTools.cableScope();
    if (!scope.targets) return true;
    const keys=groupKeys(scope.groups || []);
    return rows.every(row=>keys.has(portKey(row)));
  }
  function showProgress(id,rows,resumed=false) {
    if (ui.jobId!==id || !Array.isArray(rows) || !rows.length) return;
    if (resumed) {
      if (!matchesResumedScope(rows)) return;
    } else if (activeRun?.id!==id || activeRun.scope!==window.portTools.cableScope().targets) return;
    const currentRows=resumed ? rows : rows.filter(row=>activeRun.ports.has(portKey(row)));
    for (const row of currentRows) updateReading(row,resumed);
    if (currentRows.length) {
      $('cableResultTitle').textContent='진단 진행 중';
      $('cableResultCount').textContent=cards.size+'포트';
    }
  }
  function showResult(result,cancelled,resumed=false) {
    const rows=result.rows || [];
    if (resumed && !matchesResumedScope(rows)) {
      resetResults('재연결한 진단의 대상과 현재 선택이 다릅니다. 현재 대상으로 진단을 실행하세요.');
      return;
    }
    const keys=new Set(rows.map(portKey));
    for (const [key,card] of cards) {
      if (!keys.has(key) && !cancelled) { card.remove(); cards.delete(key); }
    }
    for (const row of rows) updateReading(row,resumed);
    $('cableResultTitle').textContent=(resumed ? '재연결한 작업 · ' : '')+'이번 진단 결과'+(cancelled?' · 취소됨':'');
    $('cableResultCount').textContent=cards.size+'포트';
    if (!cards.size) results.replaceChildren(elem('p','표시할 측정값이 없습니다.','empty'));
  }
  function current(scope) {
    return !page.hidden && !ui.busy && ui.token && scope.targets===window.portTools.cableScope().targets;
  }
  function execute(scope,targets,groups) {
    withSshAccount(async ()=>{
      if (!current(scope)) return;
      const requestId=crypto.randomUUID().replaceAll('-','');
      activeRun={id:requestId,scope:scope.targets,ports:groupKeys(groups)};
      const body={targets,confirmed:true,requestId,responseKey:crypto.randomUUID().replaceAll('-','')};
      resetResults('대상 포트의 새 측정 결과를 기다리고 있습니다.');
      let received=false;
      await run('cable.test',body,(result,cancelled)=>{
        received=true;
        if (window.portTools.cableScope().targets===scope.targets) {
          const rows=(result.rows || []).filter(row=>activeRun.ports.has(portKey(row)));
          showResult({rows},cancelled);
        } else resetResults('진단 대상이 변경되었습니다. 현재 대상으로 진단을 실행하세요.');
      });
      activeRun=null;
      if (!received) {
        const message=$('statusText').textContent || '케이블 진단 결과를 확인할 수 없습니다.';
        if (cards.size && window.portTools.cableScope().targets===scope.targets) {
          $('cableResultTitle').textContent='이번 진단 결과 · 수신 중단';
          results.prepend(elem('p',message,'warning'));
        } else resetResults(message);
      }
    });
  }
  $('cableEdit').addEventListener('click',()=>{
    setQueryExpanded(true);
    requestAnimationFrame(()=>{ if (location.hash==='#ports') $('queryTargets').focus(); });
  });
  $('cableTest').onclick=async ()=>{
    const scope=window.portTools.cableScope();
    if (!current(scope) || !scope.targets) return;
    resetResults('이번 진단의 측정값이 없습니다.');
    let groups=scope.groups;
    // Use the same range/TSV parser as monitoring before showing the exact affected ports.
    if (!groups) await run('targets.preview',{targets:scope.targets},result=>{ groups=result.groups; });
    if (!groups?.length || !current(scope)) return;
    if (groups.some(group=>!group.interfaces.length || group.interfaces.some(port=>!controlPortSupported(port)))) {
      resetResults('포트·PoE 관리에서 진단할 숫자 포트를 지정하거나 조회 결과에서 선택하세요.');
      return;
    }
    const count=groups.reduce((sum,g)=>sum+g.interfaces.length,0);
    const targets=groups.map(g=>endpoint(g)+' '+g.interfaces.join(',')).join('\n');
    const confirm=button('확인 후 진단 실행',()=>{
      dialog.close();
      if (current(scope)) execute(scope,targets,groups);
    },'primary');
    confirm.id='cableConfirm';
    openDialog('케이블 진단 실행 확인',[
      elem('p','대상 스위치의 모든 포트에 저장된 이전 케이블 측정값을 지웁니다. 선택하지 않은 포트의 측정값도 지워집니다.'),
      elem('p','새로 측정하는 아래 '+count+'개 포트의 링크가 잠시 끊겨 연결된 장비와의 통신이 중단될 수 있습니다.','warning'),
      elem('pre',describe(groups),'cable-targets'),confirm
    ]);
  };
  window.cableTools={sync:syncCable,showProgress,showResult,jobOnOpen};
  syncCable();
})();
