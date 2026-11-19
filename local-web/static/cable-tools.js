/* Use the monitoring workspace's current targets and the shared SSH session. */
(() => {
  const page = document.querySelector('[data-page="cable"]');
  const results = $('cableResult');
  const labels = {
    measured:'측정 완료', partial:'측정값 불완전', empty:'측정값 없음',
    pending:'테스트 진행 중', unsupported:'미지원', failed:'실패', uncertain:'측정 결과 미확인', cancelled:'취소됨', skipped:'건너뜀'
  };
  const pairLabels = {ok:'정상', open:'단선·미연결', short:'단락', 'inter-short':'페어 간 단락', impedance:'임피던스 이상'};
  jobNames['cable.test']='케이블 진단';
  let displayedScope;

  function describe(groups) {
    return groups.map(group=>endpoint(group)+' · 포트 '+group.interfaces.join(', ')).join('\n');
  }
  function resetResults(message) {
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
  function reading(row) {
    const card=elem('section',undefined,'cable-reading');
    const header=elem('header');
    header.append(elem('strong',endpoint(row)+' / 포트 '+row.interface),elem('span',labels[row.status] || '측정 결과 미확인'));
    card.append(header);
    if (row.message && labels[row.status]) card.append(elem('p',row.message));
    if (row.status!=='measured' || !row.pairs?.length) return card;
    const table=elem('table'), headings=table.createTHead().insertRow();
    table.setAttribute('aria-label',header.textContent+' 페어별 측정값');
    for (const title of ['페어','상태','길이 / 장애 거리']) {
      const th=elem('th',title); th.scope='col'; headings.append(th);
    }
    const body=table.createTBody();
    for (const item of row.pairs) {
      const tr=body.insertRow(), state=String(item.status || ''), key=state.toLowerCase();
      tr.insertCell().textContent=item.pair;
      const cell=tr.insertCell();
      cell.textContent=pairLabels[key] ? pairLabels[key]+' ('+state+')' : state;
      if (key!=='ok' && pairLabels[key]) cell.className='cable-fault';
      tr.insertCell().textContent=item.distance || '—';
    }
    card.append(table);
    return card;
  }
  function showResult(result,cancelled,resumed=false) {
    const rows=result.rows || [];
    if (resumed) {
      const scope=window.portTools.cableScope();
      const currentPorts=new Set((scope.groups || []).flatMap(group=>
        group.interfaces.map(port=>endpoint(group)+'|'+port)));
      if (scope.targets && rows.some(row=>!currentPorts.has(endpoint(row)+'|'+row.interface))) {
        resetResults('재연결한 진단의 대상과 현재 선택이 다릅니다. 현재 대상으로 진단을 실행하세요.');
        return;
      }
    }
    $('cableResultTitle').textContent=(resumed ? '재연결한 작업 · ' : '')+'이번 진단 결과'+(cancelled?' · 취소됨':'');
    $('cableResultCount').textContent=rows.length+'포트';
    const fragment=document.createDocumentFragment();
    for (const row of rows) fragment.append(reading(row));
    if (!rows.length) fragment.append(elem('p','표시할 측정값이 없습니다.','empty'));
    results.replaceChildren(fragment);
  }
  function current(scope) {
    return !page.hidden && !ui.busy && ui.token && scope.targets===window.portTools.cableScope().targets;
  }
  function execute(scope,targets) {
    withSshAccount(async ()=>{
      if (!current(scope)) return;
      const body={targets,confirmed:true,requestId:crypto.randomUUID().replaceAll('-','')};
      resetResults('대상 포트의 새 측정 결과를 기다리고 있습니다.');
      let received=false;
      await run('cable.test',body,(result,cancelled)=>{
        received=true;
        if (window.portTools.cableScope().targets===scope.targets) showResult(result,cancelled);
        else resetResults('진단 대상이 변경되었습니다. 현재 대상으로 진단을 실행하세요.');
      });
      if (!received) resetResults($('statusText').textContent || '케이블 진단 결과를 확인할 수 없습니다.');
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
    const count=groups.reduce((sum,g)=>sum+g.interfaces.length,0);
    const targets=groups.map(g=>endpoint(g)+' '+g.interfaces.join(',')).join('\n');
    const confirm=button('확인 후 진단 실행',()=>{
      dialog.close();
      if (current(scope)) execute(scope,targets);
    },'primary');
    confirm.id='cableConfirm';
    openDialog('케이블 진단 실행 확인',[
      elem('p','대상 스위치의 모든 포트에 저장된 이전 케이블 측정값을 지웁니다. 선택하지 않은 포트의 측정값도 지워집니다.'),
      elem('p','새로 측정하는 아래 '+count+'개 포트의 링크가 잠시 끊겨 연결된 장비와의 통신이 중단될 수 있습니다.','warning'),
      elem('pre',describe(groups),'cable-targets'),confirm
    ]);
  };
  window.cableTools={sync:syncCable,showResult};
  syncCable();
})();
