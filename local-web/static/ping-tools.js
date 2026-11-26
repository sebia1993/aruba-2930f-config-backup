'use strict';

// Preview-only development slice. Server validation is authoritative; no SSH
// execution is exposed here until admission/recovery and result UI are ready.
(() => {
  const form = $('pingForm');
  let generation = 0;
  let currentCommands = '';
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
    currentCommands = '';
    commands.value = '';
    copy.disabled = true;
    summary.textContent = '입력을 작성하고 미리보기를 실행하세요.';
    status.textContent = '입력을 검토하세요. 장비 접속은 하지 않습니다.';
    syncSource();
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
      const result = await api('/api/ping/preview', plan(), undefined, 15000);
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
  syncSource();
})();
