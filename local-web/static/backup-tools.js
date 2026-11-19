'use strict';

(() => {
  const form = $('backupForm');
  const portField = elem('label','기본 SSH 포트');
  const port = elem('input');
  Object.assign(port,{id:'backupSSHPort',type:'number',min:'1',max:'65535',value:'22',required:true});
  portField.append(port);
  form.querySelector('#concurrency').closest('label').before(portField);

  const choose = button('폴더 찾아보기', () => {
    run('backup.pick-directory', {}, r => { if (r.directory) $('backupDirectory').value = r.directory; });
  });
  choose.id = 'chooseBackupDirectory';
  choose.dataset.job = '';
  $('backupDirectory').closest('label').after(choose);
  $('backupDirectory').closest('label').querySelector('.hint').textContent =
    'Windows에서 선택하거나 드라이브·공유 폴더의 전체 경로를 입력하세요. 지정한 폴더가 없으면 백업 시 만듭니다.';

  const live = elem('details');
  live.id = 'backupDeviceProgress'; live.hidden = true; live.open = true;
  const table = elem('div'), pager = elem('div'); pager.className = 'bar';
  const pageSize = 100;
  let devices = [], first = 0;
  const previous = button('이전',()=>{first=Math.max(0,first-pageSize); drawDevices();});
  const next = button('다음',()=>{first+=pageSize; drawDevices();});
  const range = elem('span');
  pager.append(previous,range,next);
  live.append(elem('summary','장비별 진행 상황'),table,pager);
  $('backupResult').before(live);
  let lastDevices = '';

  function drawDevices() {
    const page = devices.slice(first,first+pageSize);
    const json = first+'|'+devices.length+'|'+JSON.stringify(page);
    if (lastDevices === json && !live.hidden) return;
    lastDevices = json; live.hidden = false;
    table.replaceChildren(simpleTable([
      ['스위치','endpoint'], ['단계',r=>r.phase === 'host_key' ? '지문 확인' : '구성 백업'],
      ['진행 상태','stage'], ['지문 시도','host_key_attempts'], ['백업 시도','attempts'], ['오류 코드','error_code']
    ],page));
    pager.hidden = devices.length <= pageSize;
    previous.disabled = first === 0; next.disabled = first + pageSize >= devices.length;
    range.textContent = (first+1)+'–'+(first+page.length)+' / '+devices.length+'대';
  }

  function onProgress(action, progress) {
    if (action !== 'backup.run' || !Array.isArray(progress?.devices)) return;
    devices = progress.devices;
    if (first >= devices.length) first = 0;
    drawDevices();
  }

  function showSummary(area, rows) {
    live.hidden = true; lastDevices = ''; devices = []; first = 0;
    const success = rows.filter(r=>r.status === 'success').length;
    const exhausted = rows.filter(r=>r.status === 'retry_exhausted').length;
    const cancelled = rows.filter(r=>r.status === 'cancelled').length;
    area.prepend(elem('p','성공 '+success+'대 · 재시도 소진 '+exhausted+'대 · 기타 실패 '+
      (rows.length-success-exhausted-cancelled)+'대 · 취소 '+cancelled+'대'));
    const counts = new Map();
    for (const row of rows) {
      const code = row.diagnostic_code;
      if (/^A3F1-[0-9A-HJKMNP-TV-Z]{8}-[0-9A-HJKMNP-TV-Z]$/.test(code || '')) counts.set(code,(counts.get(code)||0)+1);
    }
    if (!counts.size) return;
    const text = [...counts].sort(([a],[b])=>a.localeCompare(b)).map(([code,n])=>code+' × '+n).join('\n');
    const block = elem('details');
    block.id = 'backupDiagnostics'; block.open = true;
    block.append(elem('summary','진단 코드 '+counts.size+'종'),elem('pre',text));
    block.append(button('진단 코드 전체 복사',async()=>{
      try { await navigator.clipboard.writeText(text); notify('진단 코드와 발생 횟수를 복사했습니다.'); }
      catch { notify('아래 진단 코드를 선택해 복사하세요.'); }
    }));
    area.append(block);
  }

  window.backupTools = {onProgress,showSummary};
})();

// Static support metadata. Request text is built from this catalog, never from input values or table contents.
(() => {
  const catalog = [];
  function register(screen, source, items, frame = '') {
    for (const [id, title, purpose] of items) catalog.push({screen,source,id,title,purpose,frame});
  }
  register('구성 백업','static/index.html',[
    ['backupForm','백업 입력','대상과 백업 옵션을 입력합니다.'],['backupTargets','백업 대상','스위치 주소 목록을 입력합니다.'],
    ['backupDirectory','저장 폴더','추가로 저장할 로컬 폴더를 입력합니다.'],['fileMode','파일 이름','장비 이름과 IP 조합을 선택합니다.'],
    ['concurrency','동시 접속','동시에 접속할 장비 수를 정합니다.'],['attempts','최대 시도','장비별 접속 시도 횟수를 정합니다.'],
    ['backupResult','백업 결과','장비별 결과와 진단 코드를 확인합니다.'],['backupFiles','백업 파일','생성한 설정과 보고서를 다운로드합니다.']
  ]);
  register('구성 백업','static/backup-tools.js',[
    ['backupSSHPort','기본 SSH 포트','포트가 생략된 대상에 공통 포트를 적용합니다.'],
    ['chooseBackupDirectory','폴더 찾아보기','Windows에서 저장할 폴더를 선택합니다.'],
    ['backupDeviceProgress','장비별 진행 상황','SSH 키 확인과 설정 수집 단계를 표시합니다.'],
    ['backupDiagnostics','진단 코드 모음','실패 진단 코드와 발생 횟수를 복사합니다.']
  ]);
  register('SSH 접속 설정','static/index.html',[
    ['loginForm','접속 계정','세션에서 사용할 SSH 계정을 적용합니다.'],['username','사용자 이름','SSH 사용자 이름을 입력합니다.'],
    ['password','비밀번호','SSH 비밀번호를 입력합니다.'],['enableSecret','Enable 암호','선택적으로 Enable 암호를 입력합니다.'],
    ['logout','세션 해제','메모리에 보관 중인 접속 정보를 해제합니다.'],
    ['trustTargets','지문 조회 대상','검토할 스위치 주소를 입력합니다.'],['trustResult','지문 검토','관측한 지문과 승인 상태를 확인합니다.'],
    ['knownKeys','저장된 지문','등록된 호스트 키를 조회하고 제거합니다.']
  ]);
  register('관제·제어','static/index.html',[
    ['portMonitorTab','관제','포트별 관측값을 확인하고 제어할 포트를 선택합니다.'],
    ['portReviewTab','우선 확인','전력 상태를 점검할 포트를 확인합니다.'],
    ['queryForm','실시간 조회','스위치 주소와 포트를 입력하고 관제 화면에서 조회를 시작합니다.'],['queryTargets','조회 대상','여러 스위치의 포트 범위를 입력합니다.'],
    ['targetOptions','대상 목록 관리','조회 대상을 편집하거나 프로필로 불러오고 저장합니다.'],
    ['controlMode','제어 방식','단일 또는 일괄 제어를 선택합니다.'],
    ['operation','제어 동작','PoE 또는 인터페이스 상태 변경을 선택합니다.'],['selectionScope','선택 범위','대상 선택 범위를 정합니다.'],
    ['preview','명령 검토','명령 실행 전 대상과 현재 상태를 검토합니다.'],['controlResult','명령 실행 결과','검증된 포트 변경 결과를 표시합니다.']
  ]);
  register('AP 로그 관제','static/logs.html',[
    ['navAttention','우선 점검','점검할 포트만 표시합니다.'],['openFiles','로그 파일 열기','스위치 로그 파일을 읽습니다.'],
    ['pasteText','로그 붙여넣기','스위치 출력 텍스트를 입력합니다.'],['cards','세 가지 상태','전력 및 링크 상태별 수량을 표시합니다.'],
    ['rows','포트별 상태','포트별 마지막 전력 및 링크 상태를 표시합니다.'],['detail','포트 이벤트 상세','선택한 포트의 마지막 근거 이벤트를 확인합니다.']
  ],'logViewer');
  register('관제·제어','static/matrix.html',[
    ['search','결과 검색','장비나 포트 또는 AP 이름으로 관측값을 찾습니다.'],
    ['device','스위치 필터','한 스위치의 포트로 결과를 좁힙니다.'],['status','포트 결과 필터','전력 상태와 조회·설정 조건으로 포트를 좁힙니다.'],
    ['matrix','포트 매트릭스','포트별 상태를 한눈에 표시합니다.'],['tbody','포트별 조회 결과','포트별 관측값을 보고 실시간 제어 대상을 선택합니다.'],
    ['detail','선택 포트 상세','선택한 포트의 판정 근거를 표시합니다.'],
    ['controlObservedBatch','선택 포트 제어','체크한 실시간 포트의 제어 도구를 엽니다.']
  ],'matrixViewer');

  const dock = elem('aside'); dock.id = 'uiInspectorDock'; dock.hidden = true;
  const modal = elem('dialog'); modal.id = 'uiInspectorDialog';
  const title = elem('h2','화면 요소 식별'); modal.setAttribute('aria-label','화면 요소 식별');
  const select = elem('select'); select.setAttribute('aria-label','등록된 화면 요소 목록');
  function updateCatalog() {
    const previous=select.value;
    select.replaceChildren();
    for (const [index,item] of catalog.entries()) {
      if (item.screen==='SSH 접속 설정' && location.hash!=='#connect') continue;
      select.add(new Option(item.screen+' · '+item.title,String(index)));
    }
    if ([...select.options].some(option=>option.value===previous)) select.value=previous;
  }
  updateCatalog();
  window.addEventListener('hashchange',()=>{
    updateCatalog();
    if (modal.open) showItem(Number(select.value));
  });
  const preview = elem('pre'); preview.id = 'uiRequestPreview';
  let selecting = false, highlighted = null;
  function clearHighlight() {
    if (highlighted) highlighted.classList.remove('ui-inspected');
    highlighted = null;
  }
  function showItem(index) {
    const item = catalog[index]; if (!item) return;
    clearHighlight();
    select.value = String(index);
    const doc = item.frame ? $(item.frame)?.contentDocument : document;
    highlighted = doc?.getElementById(item.id);
    highlighted?.classList.add('ui-inspected');
    const match = /^v\d+\.\d+\.\d+$/.exec($('version').textContent.trim());
    preview.textContent = ['프로그램 버전: '+(match?.[0] || '로컬 웹'),
      '화면 위치: '+item.screen, '요소 이름: '+item.title,
      'UI 식별자: '+(item.frame ? item.frame+'.' : '')+item.id,
      '소스 위치: '+item.source, '용도: '+item.purpose, '', '현재 현상:', '원하는 변경:'].join('\n');
  }
  function setSelecting(value) {
    selecting = value; picker.setAttribute('aria-pressed',String(value));
    picker.textContent = value ? '요소 선택 중 · 취소' : '요소 선택';
  }
  function display(index) {
    setSelecting(false); showItem(index);
    if (!modal.open) modal.showModal();
  }
  function toggle() {
    dock.hidden = !dock.hidden;
    if (dock.hidden) { modal.close(); setSelecting(false); clearHighlight(); }
  }
  const picker = button('요소 선택',()=>setSelecting(!selecting));
  const launch = button('화면 요소 식별',toggle,'textbtn'); launch.id = 'uiInspectorOpen';
  launch.title = '화면 요소의 이름과 고정 식별자로 작업 요청 작성 (F12)';
  $('shutdown').after(launch);
  dock.append(elem('strong','화면 요소 식별'),picker,button('요소 목록',()=>display(Number(select.value))),button('식별 종료',toggle));
  select.onchange = ()=>showItem(Number(select.value));
  modal.append(title,select,preview,button('작업 요청 복사',async()=>{
    try { await navigator.clipboard.writeText(preview.textContent); }
    catch { const range=document.createRange(); range.selectNodeContents(preview); const selection=getSelection(); selection.removeAllRanges(); selection.addRange(range); }
  }),button('닫기',()=>modal.close()));
  modal.addEventListener('close',clearHighlight);
  document.body.append(dock,modal);
  function bind(doc, frame) {
    doc.addEventListener('keydown',e=>{
      if (e.key === 'F12' && !e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
        e.preventDefault(); if (!e.repeat) toggle();
      }
      if (e.key === 'Escape' && selecting) { e.preventDefault(); setSelecting(false); }
    },true);
    doc.addEventListener('click',e=>{
      if (!selecting || (!frame && (dock.contains(e.target) || modal.contains(e.target) || launch.contains(e.target)))) return;
      // Selection consumes the click even on an unregistered control.
      e.preventDefault(); e.stopImmediatePropagation();
      for (let node=e.target; node && node !== doc; node=node.parentElement) {
        const index = catalog.findIndex(item=>item.frame === frame && item.id === node.id);
        if (index !== -1) { display(index); break; }
      }
    },true);
  }
  bind(document,'');
  const embedded = new WeakSet();
  for (const frame of [$('logViewer'),$('matrixViewer')]) {
    function attach() {
      const doc = frame.contentDocument;
      if (doc && !embedded.has(doc)) { embedded.add(doc); bind(doc,frame.id); }
    }
    attach(); frame.addEventListener('load',attach);
  }
})();
