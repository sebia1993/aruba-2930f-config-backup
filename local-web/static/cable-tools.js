/* Use the monitoring workspace's current targets and the shared SSH session. */
(() => {
  const page = document.querySelector('[data-page="cable"]');
  const results = $('cableResult');
  const labels = {
    measured:'측정 완료', stored:'저장된 측정값', partial:'일부 페어만 확인', empty:'저장 결과 없음',
    pending:'테스트 진행 중', unsupported:'미지원', failed:'실패', uncertain:'측정 결과 미확인', cancelled:'취소됨', skipped:'건너뜀'
  };
  const pairLabels = {ok:'정상', open:'단선·미연결', short:'단락', 'inter-short':'페어 간 단락', impedance:'임피던스 이상'};
  Object.assign(jobNames, {'cable.show':'케이블 저장 결과 조회','cable.test':'케이블 테스트'});
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
      resetResults('저장 결과를 조회하거나 새 테스트를 실행하면 이곳에 표시됩니다.');
    }
    const count=scope.groups?.reduce((sum,g)=>sum+g.interfaces.length,0);
    $('cableScope').textContent=scope.label+(count ? ' · '+scope.groups.length+'대 / '+count+'포트' : '');
    $('cableTargets').textContent=(scope.groups ? describe(scope.groups) : scope.targets) ||
      '포트·PoE 관리에서 스위치 주소와 포트를 지정하세요.';
    for (const id of ['cableShow','cableTest']) $(id).disabled=ui.busy || !ui.token || !scope.targets;
  }
  function reading(row) {
    const card=elem('section',undefined,'cable-reading');
    const header=elem('header');
    header.append(elem('strong',endpoint(row)+' / 포트 '+row.interface),elem('span',labels[row.status] || row.status));
    card.append(header);
    if (row.message) card.append(elem('p',row.message));
    if (!row.pairs?.length) return card;
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
  function showResult(action,result,cancelled,resumed=false) {
    const rows=result.rows || [];
    $('cableResultTitle').textContent=(resumed ? '재연결한 작업 · ' : '')+
      (action==='cable.test' ? '테스트 실행 결과' : '스위치에 저장된 결과')+(cancelled?' · 취소됨':'');
    $('cableResultCount').textContent=rows.length+'포트';
    const fragment=document.createDocumentFragment();
    for (const row of rows) fragment.append(reading(row));
    if (!rows.length) fragment.append(elem('p','표시할 측정값이 없습니다.','empty'));
    results.replaceChildren(fragment);
  }
  function current(scope) {
    return !page.hidden && !ui.busy && ui.token && scope.targets===window.portTools.cableScope().targets;
  }
  function execute(action,scope,targets=scope.targets) {
    withSshAccount(async ()=>{
      if (!current(scope)) return;
      const body={targets};
      if (action==='cable.test') {
        body.confirmed=true;
        body.requestId=crypto.randomUUID().replaceAll('-','');
      }
      resetResults(action==='cable.test' ? '대상 포트를 테스트하고 있습니다.' : '스위치에 저장된 결과를 조회하고 있습니다.');
      let received=false;
      await run(action,body,(result,cancelled)=>{
        received=true;
        if (window.portTools.cableScope().targets===scope.targets) showResult(action,result,cancelled);
        else resetResults('진단 대상이 변경되었습니다. 현재 대상의 저장 결과를 조회하세요.');
      });
      if (!received) resetResults($('statusText').textContent || '케이블 진단 결과를 확인할 수 없습니다.');
    });
  }
  $('cableEdit').addEventListener('click',()=>{
    setQueryExpanded(true);
    requestAnimationFrame(()=>{ if (location.hash==='#ports') $('queryTargets').focus(); });
  });
  $('cableShow').onclick=()=>{
    const scope=window.portTools.cableScope();
    if (current(scope) && scope.targets) execute('cable.show',scope);
  };
  $('cableTest').onclick=async ()=>{
    const scope=window.portTools.cableScope();
    if (!current(scope) || !scope.targets) return;
    let groups=scope.groups;
    // Use the same range/TSV parser as monitoring before showing the exact affected ports.
    if (!groups) await run('targets.preview',{targets:scope.targets},result=>{ groups=result.groups; });
    if (!groups?.length || !current(scope)) return;
    const count=groups.reduce((sum,g)=>sum+g.interfaces.length,0);
    const targets=groups.map(g=>endpoint(g)+' '+g.interfaces.join(',')).join('\n');
    const confirm=button('확인 후 테스트 실행',()=>{
      dialog.close();
      if (current(scope)) execute('cable.test',scope,targets);
    },'primary');
    confirm.id='cableConfirm';
    openDialog('케이블 테스트 실행 확인',[
      elem('p','테스트 중 아래 '+count+'개 포트의 링크가 잠시 끊깁니다. 연결된 장비와의 통신이 중단될 수 있습니다.','warning'),
      elem('pre',describe(groups),'cable-targets'),confirm
    ]);
  };
  window.cableTools={sync:syncCable,showResult};
  syncCable();
})();
