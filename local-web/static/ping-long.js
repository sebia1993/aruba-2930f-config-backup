'use strict';
(() => {
  const mount = document.getElementById('pingLong');
  if (!mount) return;
  mount.innerHTML = `<header><h2>장시간 Ping 테스트</h2></header>
    <div class="panelbody"><p>위에서 선택한 장비와 목적지, Source, Timeout, Data Size를 사용합니다. 각 조합에서 Ping 1회씩 목표 횟수까지 반복합니다.</p>
    <div class="formgrid">
      <label>조합당 목표 횟수<input id="pingLongCount" type="number" value="10000" min="1" max="10000"></label>
      <label>목표 주기<select id="pingLongInterval"><option value="1">1초</option><option value="3">3초</option><option value="5" selected>5초</option><option value="10">10초</option></select></label>
      <label>전체 실행 시간 한도 (시간)<input id="pingLongHours" type="number" min="1" max="48" value="48"></label>
    </div>
    <label><input type="checkbox" id="pingLongTrace"> 3회 연속 확인된 손실에서 제한된 경로 진단 실행</label>
    <details><summary>측정 주기와 종료 정책</summary><p>Timeout은 스위치가 응답을 기다리는 시간입니다. 목표 주기는 같은 조합의 명령을 시작하려는 간격입니다. SSH 준비, 다른 작업과의 경합, 목적지별 전송 제한으로 간격과 ETA가 늘어날 수 있습니다. 관측 시각은 수집 PC의 명령 처리 시각입니다. 일시정지·미측정은 손실에서 제외하며 실제 경과 시간 한도는 유지합니다. 마지막 브라우저 탭을 닫으면 측정이 중단될 수 있습니다.</p></details>
    <div class="bar"><button id="pingLongPreview" type="button">예상시간 계산</button><button id="pingLongStart" class="primary" type="button" disabled>검토한 설정으로 장시간 측정 시작</button></div>
    <p id="pingLongEstimate" class="hint" role="status">대상과 목표 횟수를 선택한 후 예상시간을 확인하세요.</p>
    <div class="bar"><button id="pingLongPause" type="button" disabled>일시정지</button><button id="pingLongResume" type="button" disabled>재개</button><button id="pingLongCancel" type="button" disabled>측정 중지</button><button id="pingLongRefresh" type="button">상태 확인</button></div>
    <p id="pingLongStatus" role="status"></p><progress id="pingLongProgress" aria-label="장시간 Ping 진행률" max="1" value="0"></progress>
    <p id="pingLongTime"></p><details id="pingLongDetail"><summary>장비별·목적지별 진행</summary><div class="bar"><label>진행 검색<input type="search" id="pingLongSearch" maxlength="100" placeholder="스위치 또는 목적지"></label><label>진행 상태<select id="pingLongFilter"><option value="">전체</option><option value="waiting">시작 전</option><option value="active">시작됨</option><option value="done">완료</option></select></label></div><div id="pingLongDevices"></div><div class="bar"><button type="button" id="pingLongPrev">이전</button><span id="pingLongPage"></span><button type="button" id="pingLongNext">다음</button></div></details></div>`;
  const el = id => document.getElementById('pingLong' + id);
  const base = '/api/ping/long';
  const storageKey = 'ping.long.run.id.v1';
  const terminal = new Set(['done','cancelled','failed','interrupted']);
  const labels = {running:'측정 중', pausing:'일시정지 정리 중', paused:'일시정지', stopping:'중지 정리 중',
    done:'목표 완료',cancelled:'부분 종료',failed:'오류로 종료',interrupted:'서버 재시작으로 중단'};
  const actions = {Pause:'pause',Resume:'resume',Cancel:'cancel'};
  const time = value => {
    if (!Number.isFinite(value)) return '—';
    const sec = Math.max(0,Math.ceil(value));
    return [Math.floor(sec/3600),Math.floor(sec/60)%60,sec%60].map(n=>String(n).padStart(2,'0')).join(':');
  };
  const kst = value => value ? new Date(value).toLocaleString('ko-KR',{timeZone:'Asia/Seoul',hour12:false})+' KST' : '—';
  const settings = () => ({plan:window.readGeneralPingPlan(),count:el('Count').valueAsNumber,
    interval:Number(el('Interval').value), maxHours:el('Hours').valueAsNumber, autoTrace:el('Trace').checked});
  let known = null, checked = null, revision = 0, working = false, discovered = false, last = null, fresh = false, at = 0;
  const phase = {DONE:'완료',IN_FLIGHT:'응답 대기',SSH_PREPARATION:'SSH 준비',PAUSING:'정리 중',PAUSED:'일시정지',
    STOPPING:'중지 중',CANCELLED:'부분 종료',FAILED:'오류',INTERRUPTED:'중단',OTHER_OPERATION:'다른 작업에 양보',
    RECONNECT:'재연결 대기',DEVICE_BUSY:'장비 사용 중',INTERVAL:'다음 예정 시각 대기',QUEUED:'실행 대기',BLOCKED:'조합 중단'};
  let storageAvailable = true, freezeAt = null, lastStatusText = '';
  try { known = sessionStorage.getItem(storageKey); }
  catch { storageAvailable = false; }
  function buttons() {
    const live = last && !terminal.has(last.state);
    el('Start').disabled = working || !storageAvailable || !discovered || !checked || !!live || (!!known && !last);
    el('Pause').disabled = working || last?.state !== 'running';
    el('Resume').disabled = working || last?.state !== 'paused';
    el('Cancel').disabled = working || !live || last?.state === 'stopping';
  }
  let previewTimer = null;
  function clear() {
    revision++;
    checked = null;
    el('Estimate').textContent = '설정이 바뀌었습니다. 예상시간을 다시 계산하세요.';
    clearTimeout(previewTimer);
    const hasInput = window.pingInventory.active() ? !$('pingInventoryReview').hidden :
      $('pingTargets').value.trim() && $('pingDestinations').value.trim();
    if (!mount.hidden && !document.hidden && location.hash === '#ping' && ui.token && hasInput) {
      previewTimer = setTimeout(() => el('Preview').click(),400);
    }
    buttons();
  }
  for (const type of ['input','change']) {
    mount.addEventListener(type, e => {
      if (['pingLongCount','pingLongInterval','pingLongHours','pingLongTrace'].includes(e.target.id)) clear();
    });
    document.getElementById('pingForm').addEventListener(type,clear);
  }
  document.getElementById('pingForm').addEventListener('click', e => { if (e.target.closest('[data-ping-preset]')) clear(); });
  document.querySelector('[data-ping-pane="long"]')?.addEventListener('click', () => queueMicrotask(clear));
  el('Preview').onclick = async () => {
    clearTimeout(previewTimer);
    const version = ++revision;
    checked = null; buttons();
    try {
      const request = settings();
      const p = await api(base+'/preview',request,undefined,15000);
      if (revision !== version) return;
      checked = request;
      el('Estimate').textContent = p.pairs+'조합 × '+p.count+'회 = 총 '+p.total+'회 · 실제 동시 SSH '+p.effectiveConcurrency+
        '개 · 최초 예상 '+time(p.initialEta)+' · 매회 Timeout 가정 '+time(p.timeoutEta)+' · 예상 종료 '+kst(p.estimatedFinish)+' · 한도 '+p.maxHours+'시간'+
        (p.overDeadline ? ' · 예상시간이 실행 한도를 초과하여 목표 전에 끝날 수 있습니다.' : '');
    } catch (err) { if (revision === version) el('Estimate').textContent = err.message; }
    buttons();
  };
  function countdown() {
    if (!last) return;
    const since = Math.floor((performance.now()-at)/1000);
    if (since>15 && freezeAt===null) freezeAt=15;
    const delayed = !fresh || since > 15;
    const eta = last.etaSeconds === null ? '산정 불가' :
      (last.state === 'running' && last.etaSeconds <= since && !delayed ? '재계산 중' :
      time(last.etaSeconds - (last.state !== 'running' ? 0 : freezeAt ?? since)));
    el('Time').textContent = (delayed && !terminal.has(last.state) ? '갱신 지연 · ' : '')+
      '최초 예상 '+time(last.initialEta)+' · 경과 '+time(last.elapsed)+' · 남은 시간 '+eta+
      ' · 예상 종료 '+kst(last.estimatedFinish)+(Number.isFinite(last.activeSeconds) ? ' · 측정 '+time(last.activeSeconds)+' / 대기 '+time(last.waitingSeconds)+' / 일시정지 '+time(last.pausedSeconds) : '')+' · 마지막 갱신 '+kst(last.estimatedAt);
  }
  function render(result) {
    last = result; fresh = true; freezeAt=null; at = performance.now();
    el('Status').textContent = (last.staleControl ? '다른 화면에서 상태가 변경되어 요청을 적용하지 않았습니다. · ' : '')+(labels[last.state] || last.state)+' · '+last.completed+'/'+last.target+
      '회 처리 · 성공 '+last.success+' · 확인된 손실 '+last.loss+' · 그 외 처리 '+(last.completed-last.success-last.loss)+
      ' · 미측정 '+last.gaps+(last.wait ? ' · 다른 장비 작업에 양보 중' : '')+
      (last.reason ? ' · '+last.reason : '')+(last.clockUncertain ? ' · 수집 PC 시각 변동 감지' : '');
    lastStatusText=el('Status').textContent;
    el('Progress').max = Math.max(1,last.target);
    el('Progress').value = last.completed;
    countdown();
    displayDetails(); buttons();
  }
  let page = 0;
  function displayDetails() {
    if (!el('Detail').open || !last) return;
    const list = document.createDocumentFragment();
    const grouped = new Map();
    const term = el('Search').value.trim().toLowerCase(), filter = el('Filter').value;
    const inventory = last.settings?.inventoryRows;
    const roleSessions = last.settings?.inventoryRoleSessions === true;
    const inventoryBySwitch = new Map((inventory || []).map(row => [row.swIp + ':22', row]));
    const roleNames = window.pingInventory.roleNames;
    const narrowed = last.series.filter(row => {
      const device = inventoryBySwitch.get(row.switch);
      const names = roleSessions ? roleNames[row.inventoryRole] : device ?
        last.settings.inventoryRoles.filter(key => device[key] === row.destination).map(key => roleNames[key]).join(' ') : '';
      return (!term || [row.switch,row.destination,device?.hostname,names].join(' ').toLowerCase().includes(term)) &&
        (!filter || filter === (row.completed === row.target ? 'done' : row.started > 0 ? 'active' : 'waiting'));
    });
    if (inventory) {
      const bySwitch = new Map();
      for (const row of narrowed) {
        if (!bySwitch.has(row.switch)) bySwitch.set(row.switch, new Map());
        bySwitch.get(row.switch).set(roleSessions ? row.inventoryRole : row.destination, row);
      }
      const devices = inventory.filter(row => !term && !filter || bySwitch.has(row.swIp + ':22'));
      const pages = Math.max(1, Math.ceil(devices.length/100));
      page = Math.max(0,Math.min(pages-1,page));
      el('Prev').disabled = !page; el('Next').disabled = page + 1 >= pages;
      el('Page').textContent = (page+1)+' / '+pages+' · '+narrowed.length+'조합';
      const columns = [['장비 Hostname', 'hostname'], ['SW IP', 'swIp']];
      for (const [key, name] of Object.entries(roleNames)) {
        columns.push([name + ' 결과', device => {
          if (!device[key]) return '대상 없음';
          if (!last.settings.inventoryRoles.includes(key)) return '미선택';
          const series = bySwitch.get(device.swIp + ':22')?.get(roleSessions ? key : device[key]);
          if (!series) return '필터 제외';
          return device[key]+' · '+series.completed+'/'+series.target+'회 · 성공 '+series.success+
            ' · 손실 '+series.loss+' · 미측정 '+series.gaps+
            ' · 판정 불가 '+series.unknown+' · SSH 실패 '+series.sshFailures+
            (Number.isFinite(series.avgRttMs) ? ' · 평균 RTT '+series.avgRttMs+' ms' : '')+
            (Number.isFinite(series.actualInterval) ? ' · 실제 주기 '+series.actualInterval.toFixed(1)+'초' : '')+
            (Number.isFinite(series.etaSeconds) ? ' · 남은 시간 '+time(series.etaSeconds) : '')+
            (series.blockReason ? ' · '+series.blockReason : series.wait ? ' · '+(phase[series.wait] || series.wait) : '');
        }]);
      }
      el('Devices').replaceChildren(simpleTable(columns, devices.slice(page*100, page*100+100)));
      return;
    }
    const pages = Math.max(1, Math.ceil(narrowed.length/100));
    page = Math.max(0,Math.min(pages-1,page));
    el('Prev').disabled = page === 0;
    el('Next').disabled = page + 1 >= pages;
    el('Page').textContent = (page+1)+' / '+pages+' · '+narrowed.length+'조합';
    for (const row of narrowed.slice(page*100,page*100+100)) {
      if (!grouped.has(row.switch)) grouped.set(row.switch,[]);
      grouped.get(row.switch).push(row);
    }
    const devices = new Map(last.devices.map(d => [d.switch, d]));
    for (const [name,rows] of grouped) {
      const device = devices.get(name);
      const heading = document.createElement('strong');
      heading.textContent = name+(device ? ' · '+device.completed+'/'+device.target+'회 · 남은 시간 '+time(device.etaSeconds) : '');
      list.append(heading);
      for (const row of rows) {
        const line = document.createElement('p');
        line.textContent = row.destination+(row.wait ? ' · '+(phase[row.wait] || row.wait) : '')+(row.blockReason ? ' · '+row.blockReason : '')+' · '+row.completed+'/'+row.target+'회 · 성공 '+row.success+' · 손실 '+row.loss+
          ' · 미측정 '+row.gaps+' · 남은 시간 '+time(row.etaSeconds)+' · 실제 주기 '+
          (Number.isFinite(row.actualInterval) ? row.actualInterval.toFixed(1)+'초' : '산정 중');
        list.append(line);
      }
    }
    el('Devices').replaceChildren(list);
  }
  for (const name of ['Search','Filter']) {
    el(name).oninput = () => { page=0; displayDetails(); };
  }
  el('Prev').onclick = () => { page--; displayDetails(); };
  el('Next').onclick = () => { page++; displayDetails(); };
  el('Detail').ontoggle = displayDetails;
  function remember(key) {
    if (key) sessionStorage.setItem(storageKey,key);
    else sessionStorage.removeItem(storageKey);
    known = key;
  }
  async function read(force = false) {
    if (!known || terminal.has(last?.state)) {
      const current = await api(base+'/runs',undefined,undefined,10000);
      if (current.id && current.id !== known) { remember(current.id); last=null; }
      else if (!force && terminal.has(last?.state)) return;
      discovered = true;
    }
    if (known) {
      try { render(await api(base+'/runs/'+known,undefined,undefined,10000)); }
      catch (err) {
        if (err.status === 404) {
          remember(null); last=null; discovered=false;
          el('Status').textContent = '기존 ID의 접수 기록을 찾지 못했습니다. 현재 작업을 다시 확인하세요.';
        }
        throw err;
      }
    } else {
      el('Status').textContent = '실행 중인 장시간 작업이 없습니다.';
    }
  }
  async function perform(fn) {
    if (working || !ui.token) return;
    working = true; buttons();
    try { await fn(); }
    catch (err) { if (fresh) freezeAt=Math.min(15,Math.floor((performance.now()-at)/1000)); fresh=false; countdown(); el('Status').textContent = '상태 확인 필요 · '+err.message+(lastStatusText ? ' · 마지막 확인 '+lastStatusText : ''); }
    finally { working=false; buttons(); }
  }
  el('Start').onclick = () => perform(async () => {
    if (!checked || JSON.stringify(settings()) !== JSON.stringify(checked)) { clear(); return; }
    const id = Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,'0')).join('');
    remember(id); last=null;
    await api(base+'/runs',{requestId:id,settings:checked},undefined,15000);
    await read();
  });
  for (const [id,action] of Object.entries(actions)) {
    el(id).onclick = () => perform(async () => { if (known) render(await api(base+'/runs/'+known+'/'+action,{revision:last.revision},undefined,10000)); });
  }
  el('Refresh').onclick = () => perform(() => read(true));
  const poll = () => { if (!document.hidden && location.hash === '#ping') perform(read); };
  setInterval(poll,5000);
  setInterval(countdown,1000);
  window.addEventListener('hashchange',poll);
  document.addEventListener('visibilitychange',poll);
  buttons(); poll();
})();
