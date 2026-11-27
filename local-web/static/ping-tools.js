'use strict';

// General Ping UI. The server owns admission, scheduling and all measurement counters.
(() => {
  const form = $('pingForm');
  let generation = 0;
  let editRevision = 0, submittedEdit = 0;
  let navigationRevision = 0;
  let currentCommands = '';
  let firstCommands = '';
  let pairPreviewSequence = 0;
  let previewedPlan = null;
  const preset = {quick:[3,1], normal:[5,2], detail:[20,3]};
  const summary = $('pingPreviewSummary');
  const status = $('pingPreviewStatus');
  const commands = $('pingCommands');
  const copy = $('pingCopy');
  const presetButtons = [...form.querySelectorAll('[data-ping-preset]')];
  const helpButtons = [...form.querySelectorAll('[data-ping-help]')];
  const presetNames = {quick:'빠른 확인', normal:'일반 확인', detail:'상세 확인', custom:'사용자 지정'};
  let activePreset = 'normal';

  function setPresetName(name) {
    activePreset = name;
    $('pingPresetName').textContent = presetNames[name];
    for (const button of presetButtons) {
      button.setAttribute('aria-pressed', String(button.dataset.pingPreset === name));
    }
  }
  function setHelp(button, open) {
    $(button.getAttribute('aria-controls')).hidden = !open;
    button.setAttribute('aria-expanded', String(open));
  }
  function syncAllHelp() {
    const expanded = helpButtons.every(button => button.getAttribute('aria-expanded') === 'true');
    $('pingAllHelp').setAttribute('aria-expanded', String(expanded));
    $('pingAllHelp').textContent = expanded ? '전체 옵션 설명 접기' : '전체 옵션 설명 펼치기';
  }
  for (const button of helpButtons) {
    button.addEventListener('click', () => {
      setHelp(button, button.getAttribute('aria-expanded') !== 'true');
      syncAllHelp();
    });
  }
  $('pingAllHelp').addEventListener('click', () => {
    const open = $('pingAllHelp').getAttribute('aria-expanded') !== 'true';
    if (open) {
      $('pingOptions').open = true;
      $('pingAdvanced').open = true;
    }
    for (const button of helpButtons) setHelp(button, open);
    syncAllHelp();
  });

  function lines(value) {
    return value.split(/\r?\n/).filter(line => line.trim() !== '');
  }
  function schedule() {
    return {interval:Number($('pingInterval').value), count:$('pingRepetitions').valueAsNumber,
      maxHours:$('pingLongHours').valueAsNumber, autoTrace:$('pingLongTrace').checked};
  }
  function syncSource() {
    const mode = $('pingSourceMode').value;
    for (const [name, field, input] of [
      ['vlan', 'pingVlanField', 'pingVlan'],
      ['ip', 'pingSourcesField', 'pingSources']
    ]) {
      const active = mode === name;
      $(field).hidden = !active;
      $(input).disabled = !active;
      $(input).required = active;
    }
    const periodic = Number($('pingInterval').value) > 0;
    $('pingRepetitions').max = periodic ? '10000' : '100';
    $('pingPeriodicOptions').hidden = !periodic;
    $('pingLongHours').disabled = !periodic;
    $('pingLongEstimate').hidden = !periodic;
    $('pingAdvancedSummary').textContent = ' · 데이터 ' +
      ($('pingSize').value === '' ? '장비 기본값' : $('pingSize').value + ' B') +
      ' · 동시 측정 ' + ($('pingConcurrency').value || '—') + '대';
  }
  function invalidate() {
    generation++;
    admissionError = '';
    previewedPlan = null;
    currentCommands = '';
    firstCommands = '';
    $('pingPairPicker').hidden = true;
    commands.value = '';
    copy.disabled = true;
    summary.textContent = '명령 미리보기를 누르면 측정할 명령이 표시됩니다.';
    if (!running && !window.pingPeriodic?.live()) status.textContent = 'Ping 시작을 누르면 스위치 SSH 계정을 확인하고 측정을 시작합니다.';
    syncSource();
    syncRun();
  }
  function plan() {
    const mode = $('pingSourceMode').value;
    const options = {
      repetitions: Number($('pingInterval').value) ? 1 : $('pingRepetitions').valueAsNumber,
      timeout: $('pingTimeout').valueAsNumber,
      source_mode: mode,
      concurrent_devices: $('pingConcurrency').valueAsNumber
    };
    if ($('pingSize').value !== '') options.data_size = $('pingSize').valueAsNumber;
    if (mode === 'vlan') options.vlan = $('pingVlan').valueAsNumber;
    const inputs = window.pingInventory.active() ? window.pingInventory.targets() : {
      targets: $('pingTargets').value,
      destinations: lines($('pingDestinations').value).map(line => {
        const [ip, ...label] = line.split('\t');
        return {ip:ip.trim(), label:label.join('\t').trim()};
      })
    };
    const body = {...inputs, options};
    if (mode === 'ip') {
      const sources = Object.create(null);
      for (const line of lines($('pingSources').value)) {
        const parts = line.split(',');
        if (parts.length !== 2 || !parts[0].trim() || !parts[1].trim() ||
            Object.hasOwn(sources, parts[0].trim())) {
          throw new Error('Source 매핑은 중복 없이 스위치IPv4:SSH포트,SourceIPv4 형식으로 입력하세요.');
        }
        sources[parts[0].trim()] = parts[1].trim();
      }
      if (body.inventoryRows) {
        const endpoints = new Set(body.inventoryRows.filter(row =>
          window.pingInventory.rolesForRow(row, body.inventoryRoles).length
        ).map(row => row.swIp + ':22'));
        body.source_ips = Object.fromEntries(Object.entries(sources).filter(([key]) => endpoints.has(key)));
      } else body.source_ips = sources;
    }
    return body;
  }
  window.readGeneralPingPlan = plan;
  function inputChanged(event) {
    if (event.target.closest('#pingProfiles')) return;
    editRevision++;
    const current = preset[activePreset];
    const option = !event.target.closest('#pingInventory') &&
      !['pingTargets','pingDestinations','pingForm','pingInputMode'].includes(event.target.id);
    if (option || (current && ($('pingRepetitions').valueAsNumber !== current[0] ||
        $('pingTimeout').valueAsNumber !== current[1] || $('pingSourceMode').value !== 'ssh_ip' ||
        $('pingSize').value !== ''))) setPresetName('custom');
    invalidate();
  }
  form.addEventListener('input', inputChanged);
  form.addEventListener('change', inputChanged);
  for (const button of presetButtons) {
    button.addEventListener('click', () => {
      const [count, timeout] = preset[button.dataset.pingPreset];
      $('pingRepetitions').value = count;
      $('pingInterval').value = '0';
      $('pingTimeout').value = timeout;
      $('pingSourceMode').value = 'ssh_ip';
      $('pingSize').value = '';
      setPresetName(button.dataset.pingPreset);
      editRevision++;
      invalidate();
    });
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    $('pingPreviewDetails').open = true;
    invalidate();
    await reviewPlan();
  });
  async function reviewPlan() {
    const requestGeneration = generation;
    if (!ui.token) {
      status.textContent = '브라우저 세션을 확인한 후 다시 시도하세요.';
      return;
    }
    status.textContent = '서버에서 입력을 검증 중입니다.';
    try {
      const requestPlan = plan();
      const result = await api('/api/ping/preview', requestPlan, undefined, 15000);
      if (requestGeneration !== generation) return;
      const text = result.commands.map(row => row.command).join('\n');
      currentCommands = firstCommands = text;
      commands.value = text;
      copy.disabled = !text;
      const interval = Number($('pingInterval').value);
      const duration = interval ? await window.pingPeriodic.preview() : null;
      if (requestGeneration !== generation || (interval && !duration)) return;
      const total = duration ? duration.total : result.totalRequests;
      const capacity = duration || result;
      summary.textContent = '스위치 ' + result.switches + '대 · 서버 ' + result.destinations +
        '대 · ' + result.totalPairs + '조합 · 총 ' + total +
        '회 · 동시 장비 최대 ' + capacity.effectiveDevices + '대 / SSH 최대 ' + capacity.effectiveConcurrency + '세션' +
        (interval ? ' · 목표 간격 ' + interval + '초' : '') + (result.truncated ? ' · 처음 20개 명령만 표시' : '');
      status.textContent = '입력 검증 완료 · 실제 Source 및 장비 호환성 미확인 · 미실행';
      previewedPlan = requestPlan;
      $('pingPreviewTarget').replaceChildren(...result.endpoints.map(ip => new Option(ip, ip)));
      syncPreviewDestinations();
      $('pingPairPicker').hidden = result.totalPairs < 2;
      syncRun();
      return requestPlan;
    } catch (error) {
      if (requestGeneration === generation) status.textContent = error.message;
    }
  }
  function previewDestinations(plan, target) {
    if (!plan.inventoryRows) return plan.destinations;
    const device = plan.inventoryRows.find(row => row.swIp + ':22' === target);
    if (!device) return [];
    return window.pingInventory.rolesForRow(device, plan.inventoryRoles).map(role => ({
      ip:device[role], role, label:window.pingInventory.roleNames[role]
    }));
  }
  function syncPreviewDestinations() {
    if (!previewedPlan) return;
    const dests = previewDestinations(previewedPlan, $('pingPreviewTarget').value);
    $('pingPreviewDest').replaceChildren(...dests.map(dest =>
      new Option(dest.ip + (dest.label ? ' · ' + dest.label : ''), dest.role || dest.ip)));
  }
  $('pingShowPair').addEventListener('click', async () => {
    const reviewed = previewedPlan;
    if (!reviewed) return;
    const target = $('pingPreviewTarget').value;
    const dest = previewDestinations(reviewed, target).find(entry => (entry.role || entry.ip) === $('pingPreviewDest').value);
    if (!dest) return;
    const sequence = ++pairPreviewSequence;
    const subset = {targets: target, destinations: [{ip:dest.ip, label:dest.label}], options: reviewed.options};
    if (reviewed.source_ips) subset.source_ips = {[target]: reviewed.source_ips[target]};
    try {
      const selection = await api('/api/ping/preview', subset, undefined, 15000);
      if (reviewed !== previewedPlan || sequence !== pairPreviewSequence) return;
      currentCommands = commands.value = selection.commands[0].command;
      copy.disabled = false;
      status.textContent = target + ' → ' + dest.ip + (dest.label ? ' (' + dest.label + ')' : '') +
        ' · 선택한 조합의 명령입니다. 작업 시작에는 전체 검토 조건이 적용됩니다.';
    } catch (error) {
      if (reviewed === previewedPlan && sequence === pairPreviewSequence) status.textContent = error.message;
    }
  });
  for (const id of ['pingPreviewTarget', 'pingPreviewDest']) {
    $(id).addEventListener('change', () => {
      if (id === 'pingPreviewTarget' && previewedPlan?.inventoryRows) syncPreviewDestinations();
      pairPreviewSequence++;
      currentCommands = commands.value = '';
      copy.disabled = true;
      status.textContent = '선택한 조합 명령 보기를 누르세요.';
    });
  }
  $('pingShowFirst').addEventListener('click', () => {
    if (!previewedPlan) return;
    pairPreviewSequence++;
    currentCommands = commands.value = firstCommands;
    copy.disabled = !firstCommands;
    status.textContent = '입력 순서대로 최대 20개 미리보기 명령을 표시합니다.';
  });
  copy.addEventListener('click', async () => {
    const requestGeneration = generation;
    if (!currentCommands) return;
    try {
      await navigator.clipboard.writeText(currentCommands);
      if (requestGeneration === generation) status.textContent = '미리보기 명령을 복사했습니다. 실행 결과는 아닙니다.';
    } catch {
      if (requestGeneration !== generation) return;
      commands.focus();
      commands.select();
      status.textContent = '자동 복사를 사용할 수 없습니다. 선택한 명령을 직접 복사하세요.';
    }
  });
  // Persist only an opaque receipt ID, never credentials or the measurement plan.
  const storageKey = 'ping.run.id.v1';
  const latestResultKey = 'ping.latest.result.id.v1';
  const runStatus = $('pingRunStatus');
  const runButton = $('pingStart');
  const retryButton = $('pingRetry');
  const cancelButton = $('pingCancel');
  const newButton = $('pingNew');
  const refreshButton = $('pingRefresh');
  const idPattern = /^[0-9a-f]{32}$/;
  const terminalStates = new Set(['done', 'cancelled', 'failed', 'interrupted']);
  const labels = {
    normal:'정상', partial_loss:'부분 손실', no_reply:'응답 없음',
    unreachable:'도달 불가', source_error:'Source 오류', cli_error:'CLI 오류',
    ssh_failure:'SSH 실패', unknown:'결과 미확인', not_run:'미실행'
  };
  let trackedId = null;
  let pendingPlan = null;
  let storageOK = true;
  let discovered = false;
  let busy = false;
  let confirmingAccount = false;
  let refreshRequest = null;
  let admissionError = '';
  let running = false;
  let terminal = false;
  let missingReceipt = false;
  let quickStarted = 0;
  let latestResultId = null;
  let cursor = 0;
  let pageIndex = 0;
  let rows = [];
  let inventoryRows = null, inventoryRoles = [], inventoryRoleSessions = false;
  let selectedRowIndex = null;
  const selectedIndices = new Set();
  const failedStatuses = new Set(['partial_loss', 'no_reply', 'unreachable', 'source_error', 'cli_error', 'ssh_failure']);
  try {
    const saved = sessionStorage.getItem(storageKey);
    if (saved && idPattern.test(saved)) trackedId = saved;
    else if (saved) storageOK = false;
    latestResultId = sessionStorage.getItem(latestResultKey);
  } catch { storageOK = false; }

  function remember(id) {
    try {
      if (id === null) sessionStorage.removeItem(storageKey);
      else sessionStorage.setItem(storageKey, id);
      trackedId = id;
      return true;
    } catch {
      storageOK = false;
      announceRun('요청 ID를 안전하게 저장할 수 없습니다. 새 작업을 시작하지 않습니다.');
      return false;
    }
  }
  function markLatestResult(id) {
    if (latestResultId === id) return;
    latestResultId = id;
    try { sessionStorage.setItem(latestResultKey, id); } catch { /* Display preference only. */ }
  }
  function showResults() {
    const periodic = window.pingPeriodic?.result();
    const periodicStarted = Date.parse(periodic?.startedAt) || 0;
    const newerPeriodic = quickStarted ? periodicStarted > quickStarted ||
      (periodicStarted === quickStarted && latestResultId === periodic?.id) : latestResultId !== trackedId;
    const showPeriodic = !!periodic && (window.pingPeriodic.live() ||
      (!running && !missingReceipt && (!trackedId || newerPeriodic)));
    $('pingLong').hidden = !showPeriodic;
    $('pingQuickResults').hidden = showPeriodic;
  }
  function announceRun(message) {
    runStatus.textContent = message;
    if (!$('pingQuickResults').hidden || busy || confirmingAccount) status.textContent = message;
  }
  function quickBlocked() {
    return !storageOK || !discovered || busy || confirmingAccount || (!!trackedId && !terminal);
  }
  function readyForNewRun() {
    return !trackedId || (terminal && editRevision !== submittedEdit);
  }
  function canForgetReceipt() {
    return trackedId && (terminal || (missingReceipt && !pendingPlan));
  }
  function syncRun() {
    const hasTargets = window.pingInventory.active() ? window.pingInventory.hasTargets() :
      $('pingTargets').value.trim() && $('pingDestinations').value.trim();
    const periodic = window.pingPeriodic;
    runButton.disabled = quickBlocked() || !periodic?.available() || !hasTargets ||
      (!Number($('pingInterval').value) && !readyForNewRun());
    retryButton.hidden = !(trackedId && missingReceipt && pendingPlan);
    retryButton.disabled = busy || confirmingAccount || !storageOK;
    cancelButton.disabled = busy || !!refreshRequest || !running || !trackedId;
    newButton.disabled = busy || confirmingAccount || !!refreshRequest || !canForgetReceipt() || !storageOK;
    refreshButton.disabled = busy || !!refreshRequest;
    $('pingRequestId').textContent = trackedId || '—';
    const rerunReady = terminal && !!trackedId && rows.length > 0 && !busy && !confirmingAccount && storageOK && periodic?.available();
    $('pingRerunAll').disabled = !rerunReady;
    $('pingRerunSelected').disabled = !rerunReady || !selectedIndices.size;
    $('pingRerunSelected').textContent = '선택 재실행 (' + selectedIndices.size + ')';
    $('pingRerunFailed').disabled = !rerunReady || !rows.some(row => failedStatuses.has(row.status));
    $('pingRerunSelectedFailed').hidden = !inventoryRows;
    $('pingRerunSelectedFailed').disabled = !rerunReady ||
      !rows.some(row => selectedIndices.has(row.index) && failedStatuses.has(row.status));
  }
  function setResultInventory(devices, roles, roleSessions = false) {
    inventoryRows = devices || null;
    inventoryRoles = roles || [];
    inventoryRoleSessions = roleSessions;
    const select = $('pingResultView');
    for (const [name, available] of [['inventory', !!inventoryRows], ['matrix', !inventoryRows]]) {
      const opt = select.querySelector('option[value="' + name + '"]');
      opt.disabled = opt.hidden = !available;
    }
    select.value = inventoryRows ? 'inventory' : 'list';
  }
  function clearResults() {
    cursor = 0;
    pageIndex = 0;
    rows = [];
    setResultInventory(null);
    selectedRowIndex = null;
    selectedIndices.clear();
    $('pingSearch').value = '';
    $('pingStatusFilter').value = '';
    $('pingDestinationFilter').value = '';
    running = terminal = missingReceipt = false;
    $('pingRunProgress').removeAttribute('value');
    $('pingRunCounts').textContent = '아직 확인된 측정 결과가 없습니다.';
    $('pingAccountSettings').hidden = true;
    paintRows();
  }
  function switchKey(row) {
    return row.ip + ':' + row.ssh_port;
  }
  function stateLabel(row) {
    return labels[row.status] || '결과 미확인';
  }
  function responseSummary(row) {
    const counts = row.sent == null || row.received == null ? '—' : row.received + '/' + row.sent;
    const loss = row.loss_pct == null ? '—' : row.loss_pct + '%';
    const rtt = row.rtt_avg_ms == null ? '—' : row.rtt_avg_ms + ' ms';
    return stateLabel(row) + ' · 응답/송신 ' + counts + ' · 손실 ' + loss + ' · 평균 RTT ' + rtt;
  }
  function detailButton(row, label = '상세') {
    const control = elem('button', label);
    control.type = 'button';
    control.addEventListener('click', () => {
      selectedRowIndex = row.index;
      paintDetail();
    });
    return control;
  }
  function selectionBox(row) {
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = selectedIndices.has(row.index);
    box.dataset.pingResultIndex = row.index;
    box.setAttribute('aria-label', switchKey(row) + ' → ' + destinationLabel(row) + ' 선택');
    box.addEventListener('change', () => {
      if (box.checked) selectedIndices.add(row.index);
      else selectedIndices.delete(row.index);
      for (const other of $('pingRows').querySelectorAll('[data-ping-result-index="' + row.index + '"]')) {
        other.checked = box.checked;
      }
      syncRun();
    });
    return box;
  }
  function paintDetail() {
    const panel = $('pingDetail');
    const row = rows.find(item => item.index === selectedRowIndex);
    panel.hidden = !row;
    panel.replaceChildren();
    if (!row) return;
    const close = elem('button', '상세 닫기');
    close.type = 'button';
    close.addEventListener('click', () => {
      selectedRowIndex = null;
      paintDetail();
    });
    const data = [
      ['스위치', switchKey(row)], ['호스트명', row.hostname], ['모델', row.model],
      ['OS', row.software_version], ['목적지', destinationLabel(row)],
      ['Source 모드', row.source_mode], ['요청 Source', row.requested_source],
      ['계획한 Ping 명령', row.planned_command],
      ['검증 Source', row.verified_source_ip], ['측정 판정', responseSummary(row)],
      ['RTT 근거', row.rtt_evidence], ['RTT 출처', row.rtt_origin],
      ['슬롯 시작 (UTC)', row.slot_started_at], ['슬롯 종료 (UTC)', row.slot_finished_at],
      ['슬롯 소요 (ms)', row.duration_ms], ['오류 사유', row.reason_code],
      ['명령 실행 가능성', row.command_may_have_started === true ? '있음' :
        row.command_may_have_started === false ? '없음' : '미확인']
    ];
    panel.append(elem('h3', '선택 조합 상세'), close,
      elem('p', '슬롯 시각은 SSH 작업의 관측 범위이며 개별 ICMP 송수신 시각이 아닙니다.', 'hint'),
      simpleTable([['항목', r => r[0]], ['값', r => r[1]]], data));
  }
  function destinationLabel(row) {
    return row.destination_ip + (row.destination_label ? ' (' + row.destination_label + ')' : '');
  }
  function destinationFilterKey(row) {
    return inventoryRoleSessions ? switchKey(row) + '/' + row.inventory_roles[0] : row.destination_ip;
  }
  function filteredRows() {
    const destination = $('pingDestinationFilter');
    const query = $('pingSearch').value.trim().toLowerCase();
    const wanted = $('pingStatusFilter').value;
    return rows.filter(row =>
      (!wanted || row.status === wanted) &&
      (!destination.value || destinationFilterKey(row) === destination.value) &&
      (!query || [switchKey(row), row.hostname, row.inventory_hostname, row.destination_ip, row.destination_label,
        ...(row.inventory_roles || []).map(key => window.pingInventory.roleNames[key])]
        .some(value => String(value ?? '').toLowerCase().includes(query))));
  }
  window.pingExportSelection = () => ({
    id: trackedId, ready: terminal && rows.length > 0,
    filtered: filteredRows().map(row => row.index), selected: [...selectedIndices].sort((a, b) => a - b)
  });
  function paintRows() {
    const pageSize = 100;
    const destination = $('pingDestinationFilter');
    const chosen = destination.value;
    const destinations = new Map();
    for (const row of rows) destinations.set(destinationFilterKey(row),
      inventoryRoleSessions ? switchKey(row) + ' → ' + destinationLabel(row) : row.destination_ip);
    destination.replaceChildren(new Option('전체', ''));
    for (const [key, label] of destinations) destination.add(new Option(label, key));
    if (destinations.has(chosen)) destination.value = chosen;
    const filtered = filteredRows();
    const matrix = $('pingResultView').value === 'matrix';
    const inventory = $('pingResultView').value === 'inventory' && inventoryRows;
    let items = filtered;
    let columns = [
      ['선택', selectionBox],
      ['스위치', switchKey],
      ['목적지', destinationLabel],
      ['확인 Source', 'verified_source_ip'],
      ['상태', stateLabel],
      ['송신', 'sent'], ['응답', 'received'],
      ['손실률', r => r.loss_pct == null ? '—' : r.loss_pct + '%'],
      ['RTT min/avg/max (ms)', r => r.rtt_evidence ?? (
        r.rtt_avg_ms == null ? '—' : [r.rtt_min_ms, r.rtt_avg_ms, r.rtt_max_ms].join('/')
      )],
      ['사유', 'reason_code'], ['상세', r => detailButton(r)]
    ];
    if (inventory) {
      const bySwitch = new Map();
      for (const row of filtered) {
        if (!bySwitch.has(row.ip)) bySwitch.set(row.ip, new Map());
        bySwitch.get(row.ip).set(inventoryRoleSessions ? row.inventory_roles[0] : row.destination_ip, row);
      }
      const filtering = !!($('pingSearch').value.trim() || $('pingStatusFilter').value || chosen);
      items = inventoryRows.filter(row => !filtering || bySwitch.has(row.swIp));
      columns = [['장비 Hostname', 'hostname'], ['SW IP', 'swIp'], ['GW IP', row => row.gwIp || '—'],
        ['BB', row => row.bbIp || '—'], ['NMS server', row => row.nmsIp || '—']];
      for (const [key, name] of Object.entries(window.pingInventory.roleNames)) {
        columns.push([name + ' 결과', device => {
          const ip = device[key];
          if (!ip) return '대상 없음';
          if (!inventoryRoles.includes(key)) return '미선택';
          if (inventoryRoleSessions ? !device.activeRoles.includes(key) : !device.activeDestinations.includes(ip)) return '미선택';
          const row = bySwitch.get(device.swIp)?.get(inventoryRoleSessions ? key : ip);
          if (!row) return filtering ? '필터 제외' : '결과 대기';
          const cell = elem('span');
          cell.append(selectionBox(row), detailButton(row, responseSummary(row)));
          return cell;
        }]);
      }
    } else if (matrix) {
      const grouped = new Map();
      for (const row of filtered) {
        const key = switchKey(row);
        if (!grouped.has(key)) grouped.set(key, {key, cells: new Map()});
        grouped.get(key).cells.set(row.destination_ip, row);
      }
      items = [...grouped.values()];
      columns = [['스위치', 'key'], ...[...destinations.keys()]
        .filter(ip => !destination.value || ip === destination.value)
        .map(ip => [ip, group => {
          const row = group.cells.get(ip);
          if (!row) return '미수신 또는 필터 제외';
          const cell = elem('span');
          cell.append(selectionBox(row), detailButton(row, responseSummary(row)));
          return cell;
        }])];
    }
    const pages = Math.max(1, Math.ceil(items.length / pageSize));
    pageIndex = Math.max(0, Math.min(pageIndex, pages - 1));
    $('pingRows').replaceChildren(simpleTable(columns,
      items.slice(pageIndex * pageSize, (pageIndex + 1) * pageSize)));
    $('pingFilterCount').textContent = '필터 결과 ' + filtered.length + ' / 수신 ' + rows.length + '조합';
    $('pingPage').textContent = (pageIndex + 1) + ' / ' + pages + (matrix || inventory ? ' · 스위치 단위' : ' · 조합 단위');
    $('pingPrev').disabled = pageIndex === 0;
    $('pingNext').disabled = pageIndex + 1 >= pages;
    paintDetail();
  }
  for (const id of ['pingSearch', 'pingStatusFilter', 'pingDestinationFilter', 'pingResultView']) {
    $(id).addEventListener(id === 'pingSearch' ? 'input' : 'change', () => {
      pageIndex = 0;
      paintRows();
    });
  }
  $('pingPrev').addEventListener('click', () => { pageIndex--; paintRows(); });
  $('pingNext').addEventListener('click', () => { pageIndex++; paintRows(); });

  async function readRun() {
    const id = trackedId;
    const job = await api('/api/ping/runs/' + id + '?after=' + cursor, undefined, undefined, 10000);
    if (job.id !== id) throw new Error('작업 식별자가 일치하지 않습니다.');
    const snapshot = job.result || job.progress;
    running = job.state === 'running' && !job.historyOnly;
    quickStarted = Date.parse(job.started) || 0;
    if (running) markLatestResult(id);
    missingReceipt = false;
    showResults();
    pendingPlan = null; // A receipt proves admission; never submit this plan again.
    terminal = false;
    if (snapshot && Number.isInteger(snapshot.total) && snapshot.total > 0 &&
        snapshot.total <= 3000 && Number.isInteger(snapshot.completed) &&
        snapshot.completed >= cursor && snapshot.completed <= snapshot.total &&
        Number.isInteger(snapshot.processedPairs) && snapshot.processedPairs >= 0 &&
        snapshot.processedPairs <= snapshot.completed && Array.isArray(snapshot.rows) &&
        snapshot.rows.length <= 512 && snapshot.cursor === cursor + snapshot.rows.length &&
        snapshot.cursor <= snapshot.completed &&
        snapshot.rows.every((row, index) => row.index === cursor + index)) {
      const firstInventory = !inventoryRows && Array.isArray(snapshot.inventoryRows) && snapshot.inventoryRows.length > 0;
      if (firstInventory) setResultInventory(snapshot.inventoryRows, snapshot.inventoryRoles, snapshot.inventoryRoleSessions === true);
      if (snapshot.rows.length || firstInventory) {
        rows.push(...snapshot.rows);
        cursor = snapshot.cursor;
        paintRows();
      }
      const meter = $('pingRunProgress');
      meter.max = snapshot.total;
      meter.value = snapshot.completed;
      $('pingRunCounts').textContent = '분류된 조합 ' + snapshot.completed + '/' + snapshot.total +
        ' · 처리 ' + snapshot.processedPairs + ' · 미실행 분류 포함 · 실제 ICMP 응답 수와 다름';
      terminal = terminalStates.has(job.state) && cursor === snapshot.completed;
    } else if (job.historyOnly) {
      $('pingRunProgress').removeAttribute('value');
      $('pingRunCounts').textContent = '서버 이력만 확인됨 · 개별 측정 상세 복원 불가';
      terminal = terminalStates.has(job.state);
    } else {
      throw new Error('진행 데이터 형식이 올바르지 않습니다.');
    }
    let message = ({
      running:'작업 진행 중',
      done:'작업 종료 · 개별 측정 상태를 확인하세요.',
      cancelled:'작업 취소됨 · 미실행 조합 포함',
      failed:'작업 실패 · 개별 측정 상태 확인 필요',
      interrupted:'서버 재시작으로 중단 · 저장된 조합별 판정을 확인하세요.'
    }[job.state] || '작업 상태 미확인') + (job.historyOnly ? ' · 이력만 확인됨' : '');
    if (snapshot && !job.historyOnly) message += ' · ' + snapshot.completed + '/' + snapshot.total + '조합';
    if (running && snapshot?.slotActive) message += ' · SSH 접속 및 Ping 측정 중';
    if (running && snapshot?.waitReason) {
      const wait = {MANUAL_QUEUE:'수동 예약 작업', OPERATOR_CONFIRMATION:'포트 작업 확인',
        MONITOR_DUE:'예약된 포트 조회', OTHER_OPERATION:'다른 작업'};
      message += ' · ' + (wait[snapshot.waitReason] || '공유 작업') + ' 대기';
    } else if (running && !snapshot?.slotActive) {
      message += ' · SSH 작업 시작 대기';
    }
    if (!running && !terminal && !job.historyOnly) message += ' · 결과 페이지 수신 중';
    if (job.error === 'AUTH_FAILED' || job.error === 'ENABLE_FAILED') {
      $('pingAccountSettings').hidden = false;
      message += job.error === 'AUTH_FAILED' ? ' · 스위치 SSH 사용자 이름과 비밀번호를 확인하세요.' :
        ' · 스위치 Enable 비밀번호와 권한을 확인하세요.';
    }
    if (job.error) message += ' · ' + job.error;
    if (terminal && admissionError) message += ' · ' + admissionError;
    announceRun(message);
  }

  async function discover() {
    const current = await api('/api/ping/runs', undefined, undefined, 10000);
    if (current.id !== null && !idPattern.test(current.id)) throw new Error('작업 ID 형식 오류');
    discovered = true;
    if (current.id && current.id !== trackedId && (!trackedId || terminal)) {
      clearResults();
      admissionError = '';
      // Even if storage becomes unavailable, allow read-only monitoring of this ID.
      if (!remember(current.id)) trackedId = current.id;
    }
  }
  function refresh() {
    if (refreshRequest) return refreshRequest;
    if (busy || !ui.token) return;
    refreshRequest = refreshStatus().finally(() => {
      refreshRequest = null;
      syncRun();
    });
    syncRun();
    return refreshRequest;
  }
  async function refreshStatus() {
    try {
      if (!trackedId || terminal) await discover();
      if (trackedId && !terminal) await readRun();
      else if (!trackedId && storageOK) runStatus.textContent = admissionError || '실행 중인 Ping 작업 없음';
    } catch (error) {
      running = false;
      missingReceipt = !!trackedId && error.status === 404 && error.serverRejection;
      if (!trackedId) discovered = false;
      if (missingReceipt && !pendingPlan) {
        showResults();
        announceRun('서버에 기존 요청의 접수 기록이 없습니다. 새 작업 준비를 누르면 현재 작업을 다시 확인합니다.');
      } else if (trackedId) announceRun('작업 상태 미확인 · 새 요청을 보내지 않습니다. · ' + error.message);
      else runStatus.textContent = admissionError || error.message;
    }
  }
  async function postRun(retry = false, rerun = null) {
    if (busy || confirmingAccount || !storageOK || !ui.token || !window.pingPeriodic?.available()) return;
    if (retry && !(trackedId && missingReceipt && pendingPlan)) return;
    if (rerun && (!terminal || !trackedId || rerun.parentId !== trackedId || !rows.length)) return;
    if (!retry && !rerun && (!readyForNewRun() || !discovered || !form.reportValidity())) return;
    let request = retry ? pendingPlan : rerun ? {rerun} : null;
    const requestGeneration = generation;
    const requestEdit = editRevision;
    const requestNavigation = navigationRevision;
    const previousId = trackedId;
    const stillRequested = () => requestNavigation === navigationRevision &&
      location.hash === '#ping' && !$('pingRunHeading').closest('article').hidden &&
      window.pingPeriodic?.available() && trackedId === previousId && (retry ? pendingPlan === request && missingReceipt :
        rerun ? terminal : requestGeneration === generation && request?.plan === previewedPlan);
    confirmingAccount = true;
    syncRun();
    try {
      if (!retry && !rerun) {
        const checked = previewedPlan || await reviewPlan();
        if (!checked) return;
        request = {plan:checked};
        if (!stillRequested()) return;
      }
      status.textContent = '스위치 SSH 계정 확인 중…';
      if (!await ensurePingSshAccount(stillRequested)) {
        if (stillRequested()) status.textContent = 'Ping 시작을 취소했습니다.';
        return;
      }
      // A status GET may have recovered a previously ambiguous request during account entry.
      if (refreshRequest) await refreshRequest;
      if (!stillRequested()) return;
    } catch (error) {
      admissionError = error.message;
      announceRun(admissionError);
      return;
    } finally {
      confirmingAccount = false;
      syncRun();
    }
    admissionError = '';
    busy = true;
    syncRun();
    try {
      if (!retry) {
        if (!rerun && !previousId) {
          await discover();
          if (trackedId) {
            await readRun();
            return;
          }
          if (!stillRequested()) return;
        }
        const id = Array.from(crypto.getRandomValues(new Uint8Array(16)),
          byte => byte.toString(16).padStart(2, '0')).join('');
        if (!remember(id)) return;
        pendingPlan = request;
      }
      announceRun('Ping 작업 접수 중…');
      // Only the operator's explicit action moves the view. Polling and reload never scroll.
      const feedback = retry || rerun ? runStatus : status;
      const bounds = feedback.getBoundingClientRect();
      if (!document.hidden && (bounds.top < 0 || bounds.bottom > innerHeight)) {
        feedback.scrollIntoView({block:'center', behavior:'instant'});
      }
      try {
        const receipt = await api('/api/ping/runs', {requestId:trackedId, ...request}, undefined, 10000);
        if (receipt.id !== trackedId || receipt.accepted !== true) throw new Error('접수 응답 미확인');
        pendingPlan = null;
      } catch (error) {
        // A first, explicit server rejection proves no admission. An ambiguous
        // response or rejection of a retry does not; retain the original ID.
        if (!retry && error.serverRejection && [400,403,409,413,415].includes(error.status)) {
          if (remember(previousId)) pendingPlan = null;
          discovered = !!previousId;
          admissionError = error.message;
          if (error.code === 'SSH_ACCOUNT_REQUIRED') $('pingAccountSettings').hidden = false;
          announceRun(admissionError);
          return;
        }
        announceRun('접수 미확인 · 기존 요청 ID를 조회합니다.');
      }
      if (!retry) {
        quickStarted = 0;
        markLatestResult(trackedId);
        showResults();
        // Keep the prior receipt and rows intact until the new request could have been admitted.
        clearResults();
        if (!rerun) submittedEdit = requestEdit;
      }
      await readRun();
    } catch (error) {
      running = false;
      missingReceipt = !!trackedId && error.status === 404 && error.serverRejection;
      if (!trackedId) discovered = false;
      announceRun('접수/상태 미확인 · 자동 재전송하지 않습니다. · ' + error.message);
    } finally {
      busy = false;
      syncRun();
    }
  }
  runButton.addEventListener('click', () => {
    if (!Number($('pingInterval').value)) postRun();
    else if (!quickBlocked() && form.reportValidity()) window.pingPeriodic.start();
  });
  window.pingMeasurement = {
    sync:syncRun, showResults, markLatestResult, schedule, quickBlocked,
    restoreSchedule(settings) {
      if (editRevision || !settings) return;
      $('pingInterval').value = String(settings.interval);
      $('pingRepetitions').value = settings.count;
      $('pingLongHours').value = settings.maxHours;
      $('pingLongTrace').checked = settings.autoTrace;
      syncSource();
    }
  };
  for (const [id, mode] of [['pingRerunAll','all'], ['pingRerunSelected','selected'], ['pingRerunFailed','failed']]) {
    $(id).addEventListener('click', () => {
      const rerun = {parentId: trackedId, mode};
      if (mode === 'selected') rerun.indices = [...selectedIndices].sort((a, b) => a - b);
      postRun(false, rerun);
    });
  }
  retryButton.addEventListener('click', () => postRun(true));
  $('pingRerunSelectedFailed').addEventListener('click', () => {
    const indices = rows.filter(row => selectedIndices.has(row.index) && failedStatuses.has(row.status))
      .map(row => row.index);
    if (indices.length) postRun(false, {parentId:trackedId, mode:'selected', indices});
  });
  refreshButton.addEventListener('click', refresh);
  cancelButton.addEventListener('click', async () => {
    if (busy || !trackedId || !running || !ui.token) return;
    busy = true;
    syncRun();
    try {
      await api('/api/ping/runs/' + trackedId + '/cancel', {}, undefined, 10000);
      await readRun();
    } catch (error) {
      running = false;
      announceRun('취소 결과 미확인 · 작업 상태를 다시 확인하세요. · ' + error.message);
    } finally {
      busy = false;
      syncRun();
    }
  });
  newButton.addEventListener('click', () => {
    if (busy || confirmingAccount || refreshRequest || !canForgetReceipt() || !remember(null)) return;
    pendingPlan = null;
    quickStarted = 0;
    discovered = false;
    clearResults();
    showResults();
    invalidate();
    refresh();
  });
  // GET-only polling, never auto-submit on reload, navigation or lost response.
  function visibleRefresh() {
    if (location.hash === '#ping') refresh();
  }
  window.addEventListener('hashchange', () => {
    navigationRevision++;
    visibleRefresh();
  });
  document.addEventListener('pingpane', event => {
    if (event.detail !== 'measure') navigationRevision++;
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) visibleRefresh();
  });
  setInterval(() => { if (!document.hidden) visibleRefresh(); }, 2000);
  window.addEventListener('aruba-session-ready', visibleRefresh);
  syncSource();
  setPresetName('normal');
  syncRun();
  if (!storageOK) runStatus.textContent = '요청 ID 저장 불가 · 새 작업 시작 차단';
  visibleRefresh();
})();
