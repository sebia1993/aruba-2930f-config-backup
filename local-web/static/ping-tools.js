'use strict';

// General Ping UI. The server owns admission, scheduling and all measurement counters.
(() => {
  const form = $('pingForm');
  let generation = 0;
  let currentCommands = '';
  let previewedPlan = null;
  const preset = {quick:[3,1], normal:[5,2], detail:[20,3]};
  const summary = $('pingPreviewSummary');
  const status = $('pingPreviewStatus');
  const commands = $('pingCommands');
  const copy = $('pingCopy');

  function lines(value) {
    return value.split(/\r?\n/).filter(line => line.trim() !== '');
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
  }
  function invalidate() {
    generation++;
    previewedPlan = null;
    currentCommands = '';
    commands.value = '';
    copy.disabled = true;
    summary.textContent = '입력을 작성하고 미리보기를 실행하세요.';
    status.textContent = '입력을 검토하세요. 장비 접속은 하지 않습니다.';
    syncSource();
    syncRun();
  }
  function plan() {
    const mode = $('pingSourceMode').value;
    const options = {
      repetitions: $('pingRepetitions').valueAsNumber,
      timeout: $('pingTimeout').valueAsNumber,
      source_mode: mode,
      concurrency: $('pingConcurrency').valueAsNumber
    };
    if ($('pingSize').value !== '') options.data_size = $('pingSize').valueAsNumber;
    if (mode === 'vlan') options.vlan = $('pingVlan').valueAsNumber;
    const body = {
      targets: $('pingTargets').value,
      destinations: lines($('pingDestinations').value).map(line => {
        const [ip, ...label] = line.split('\t');
        return {ip:ip.trim(), label:label.join('\t').trim()};
      }),
      options
    };
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
      body.source_ips = sources;
    }
    return body;
  }
  form.addEventListener('input', invalidate);
  form.addEventListener('change', invalidate);
  for (const button of form.querySelectorAll('[data-ping-preset]')) {
    button.addEventListener('click', () => {
      const [count, timeout] = preset[button.dataset.pingPreset];
      $('pingRepetitions').value = count;
      $('pingTimeout').value = timeout;
      invalidate();
    });
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    invalidate();
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
      currentCommands = text;
      commands.value = text;
      copy.disabled = !text;
      summary.textContent = '스위치 ' + result.switches + '대 · 서버 ' + result.destinations +
        '대 · ' + result.totalPairs + '조합 · 총 ' + result.totalRequests +
        '회 · 요청 동시성 ' + result.requestedConcurrency + ' / 실제 ' +
        result.effectiveConcurrency + (result.truncated ? ' · 처음 20개 명령만 표시' : '');
      status.textContent = '입력 검증 완료 · 실제 Source 및 장비 호환성 미확인 · 미실행';
      previewedPlan = requestPlan;
      syncRun();
    } catch (error) {
      if (requestGeneration === generation) status.textContent = error.message;
    }
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
  let running = false;
  let terminal = false;
  let missingReceipt = false;
  let cursor = 0;
  let pageIndex = 0;
  let rows = [];
  let selectedRowIndex = null;
  try {
    const saved = sessionStorage.getItem(storageKey);
    if (saved && idPattern.test(saved)) trackedId = saved;
    else if (saved) storageOK = false;
  } catch { storageOK = false; }

  function remember(id) {
    try {
      if (id === null) sessionStorage.removeItem(storageKey);
      else sessionStorage.setItem(storageKey, id);
      trackedId = id;
      return true;
    } catch {
      storageOK = false;
      runStatus.textContent = '요청 ID를 안전하게 저장할 수 없습니다. 새 작업을 시작하지 않습니다.';
      return false;
    }
  }
  function syncRun() {
    runButton.disabled = busy || !storageOK || !discovered || !!trackedId || !previewedPlan;
    retryButton.hidden = !(trackedId && missingReceipt && pendingPlan);
    retryButton.disabled = busy || !storageOK;
    cancelButton.disabled = busy || !running || !trackedId;
    newButton.disabled = busy || !terminal || !trackedId || !storageOK;
    refreshButton.disabled = busy;
    $('pingRequestId').textContent = trackedId || '—';
  }
  function clearResults() {
    cursor = 0;
    pageIndex = 0;
    rows = [];
    selectedRowIndex = null;
    $('pingSearch').value = '';
    $('pingStatusFilter').value = '';
    $('pingDestinationFilter').value = '';
    running = terminal = missingReceipt = false;
    $('pingRunProgress').removeAttribute('value');
    $('pingRunCounts').textContent = '아직 확인된 측정 결과가 없습니다.';
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
    return stateLabel(row) + ' · ' + counts + ' · ' + loss + ' · ' + rtt;
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
      ['OS', row.software_version], ['목적지', row.destination_ip],
      ['Source 모드', row.source_mode], ['요청 Source', row.requested_source],
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
  function paintRows() {
    const pageSize = 100;
    const destination = $('pingDestinationFilter');
    const chosen = destination.value;
    const destinations = [...new Set(rows.map(row => row.destination_ip))];
    destination.replaceChildren(new Option('전체', ''));
    for (const ip of destinations) destination.add(new Option(ip, ip));
    if (destinations.includes(chosen)) destination.value = chosen;
    const query = $('pingSearch').value.trim().toLowerCase();
    const wanted = $('pingStatusFilter').value;
    const filtered = rows.filter(row =>
      (!wanted || row.status === wanted) &&
      (!destination.value || row.destination_ip === destination.value) &&
      (!query || [switchKey(row), row.hostname, row.destination_ip, row.destination_label]
        .some(value => String(value ?? '').toLowerCase().includes(query))));
    const matrix = $('pingResultView').value === 'matrix';
    let items = filtered;
    let columns = [
      ['스위치', switchKey],
      ['목적지', r => r.destination_ip + (r.destination_label ? ' (' + r.destination_label + ')' : '')],
      ['확인 Source', 'verified_source_ip'],
      ['상태', stateLabel],
      ['송신', 'sent'], ['응답', 'received'],
      ['손실률', r => r.loss_pct == null ? '—' : r.loss_pct + '%'],
      ['RTT min/avg/max (ms)', r => r.rtt_evidence ?? (
        r.rtt_avg_ms == null ? '—' : [r.rtt_min_ms, r.rtt_avg_ms, r.rtt_max_ms].join('/')
      )],
      ['사유', 'reason_code'], ['상세', r => detailButton(r)]
    ];
    if (matrix) {
      const grouped = new Map();
      for (const row of filtered) {
        const key = switchKey(row);
        if (!grouped.has(key)) grouped.set(key, {key, cells: new Map()});
        grouped.get(key).cells.set(row.destination_ip, row);
      }
      items = [...grouped.values()];
      columns = [['스위치', 'key'], ...destinations
        .filter(ip => !destination.value || ip === destination.value)
        .map(ip => [ip, group => {
          const row = group.cells.get(ip);
          return row ? detailButton(row, responseSummary(row)) : '미수신 또는 필터 제외';
        }])];
    }
    const pages = Math.max(1, Math.ceil(items.length / pageSize));
    pageIndex = Math.max(0, Math.min(pageIndex, pages - 1));
    $('pingRows').replaceChildren(simpleTable(columns,
      items.slice(pageIndex * pageSize, (pageIndex + 1) * pageSize)));
    $('pingFilterCount').textContent = '필터 결과 ' + filtered.length + ' / 수신 ' + rows.length + '조합';
    $('pingPage').textContent = (pageIndex + 1) + ' / ' + pages + (matrix ? ' · 스위치 단위' : ' · 조합 단위');
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
    missingReceipt = false;
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
      if (snapshot.rows.length) {
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
    runStatus.textContent = ({
      running:'작업 진행 중 · 기존 운영 작업 우선',
      done:'작업 종료 · 개별 측정 상태를 확인하세요.',
      cancelled:'작업 취소됨 · 미실행 조합 포함',
      failed:'작업 실패 · 개별 측정 상태 확인 필요',
      interrupted:'서버 재시작 또는 중단 · 상세 결과 미확인'
    }[job.state] || '작업 상태 미확인') + (job.historyOnly ? ' · 이력만 확인됨' : '') +
      (job.error ? ' · ' + job.error : '');
    if (!running && !terminal && !job.historyOnly) runStatus.textContent += ' · 결과 페이지 수신 중';
  }

  async function discover() {
    const current = await api('/api/ping/runs', undefined, undefined, 10000);
    if (current.id !== null && !idPattern.test(current.id)) throw new Error('작업 ID 형식 오류');
    discovered = true;
    if (current.id && !trackedId) {
      clearResults();
      // Even if storage becomes unavailable, allow read-only monitoring of this ID.
      if (!remember(current.id)) trackedId = current.id;
    }
  }
  async function refresh() {
    if (busy || !ui.token) return;
    busy = true;
    syncRun();
    try {
      if (!trackedId) await discover();
      if (trackedId && !terminal) await readRun();
      else if (!trackedId && storageOK) runStatus.textContent = '실행 중인 Ping 작업 없음 · 미리보기 후 시작하세요.';
    } catch (error) {
      running = false;
      missingReceipt = !!trackedId && error.status === 404 && error.serverRejection;
      if (!trackedId) discovered = false;
      runStatus.textContent = (trackedId ? '작업 상태 미확인 · 새 요청을 보내지 않습니다. · ' : '') + error.message;
    } finally {
      busy = false;
      syncRun();
    }
  }
  async function postRun(retry = false) {
    if (busy || !storageOK || !ui.token) return;
    if (retry ? !(trackedId && missingReceipt && pendingPlan) : (!previewedPlan || trackedId || !discovered)) return;
    const requestPlan = retry ? pendingPlan : previewedPlan;
    const requestGeneration = generation;
    busy = true;
    syncRun();
    try {
      if (!retry) {
        await discover();
        if (trackedId || requestGeneration !== generation) return;
        const id = Array.from(crypto.getRandomValues(new Uint8Array(16)),
          byte => byte.toString(16).padStart(2, '0')).join('');
        if (!remember(id)) return;
        clearResults();
        pendingPlan = requestPlan;
      }
      runStatus.textContent = '작업 접수 중 · 요청 ID 유지';
      try {
        const receipt = await api('/api/ping/runs', {requestId:trackedId, plan:requestPlan}, undefined, 10000);
        if (receipt.id !== trackedId || receipt.accepted !== true) throw new Error('접수 응답 미확인');
        pendingPlan = null;
      } catch (error) {
        // A first, explicit server rejection proves no admission. An ambiguous
        // response or rejection of a retry does not; retain the original ID.
        if (!retry && error.serverRejection && [400,403,409,413,415].includes(error.status)) {
          if (remember(null)) pendingPlan = null;
          discovered = false;
          runStatus.textContent = error.message;
          return;
        }
        runStatus.textContent = '접수 미확인 · 기존 요청 ID를 조회합니다.';
      }
      await readRun();
    } catch (error) {
      running = false;
      missingReceipt = !!trackedId && error.status === 404 && error.serverRejection;
      if (!trackedId) discovered = false;
      runStatus.textContent = '접수/상태 미확인 · 자동 재전송하지 않습니다. · ' + error.message;
    } finally {
      busy = false;
      syncRun();
    }
  }
  runButton.addEventListener('click', () => postRun());
  retryButton.addEventListener('click', () => postRun(true));
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
      runStatus.textContent = '취소 결과 미확인 · 작업 상태를 다시 확인하세요. · ' + error.message;
    } finally {
      busy = false;
      syncRun();
    }
  });
  newButton.addEventListener('click', () => {
    if (busy || !terminal || !remember(null)) return;
    pendingPlan = null;
    discovered = false;
    clearResults();
    invalidate();
    refresh();
  });
  // GET-only polling, never auto-submit on reload, navigation or lost response.
  function visibleRefresh() {
    if (location.hash === '#ping' && (!terminal || !trackedId)) refresh();
  }
  window.addEventListener('hashchange', visibleRefresh);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) visibleRefresh();
  });
  setInterval(() => { if (!document.hidden) visibleRefresh(); }, 2000);
  syncSource();
  syncRun();
  if (!storageOK) runStatus.textContent = '요청 ID 저장 불가 · 새 작업 시작 차단';
  visibleRefresh();
})();
