'use strict';
(() => {
  const profileApi = (action, body) => api('/api/ping/profiles/' + action, body);
  const status = $('pingProfileStatus');
  let view = 'general';
  function showPane(name) {
    view = name;
    const analysisTab = {analysis:'report',heat:'heat',nms:'nms'}[name];
    for (const btn of document.querySelectorAll('[data-ping-pane]')) {
      btn.setAttribute('aria-pressed', String(btn.dataset.pingPane === name));
    }
    $('pingForm').closest('article').hidden = !!analysisTab;
    $('pingPreview').closest('.bar').hidden = name !== 'general';
    $('pingPreviewHeading').closest('article').hidden = name !== 'general';
    $('pingRunHeading').closest('article').hidden = name !== 'general';
    $('pingLong').hidden = name !== 'long';
    $('pingAnalysis').hidden = !analysisTab;
    if (analysisTab) $('pingAnalysis').querySelector('[data-pa-tab="'+analysisTab+'"]')?.click();
    $('pingPath').hidden = name !== 'path';
    document.dispatchEvent(new CustomEvent('pingpane', {detail:name}));
  }
  for (const btn of document.querySelectorAll('[data-ping-pane]')) {
    btn.addEventListener('click', () => showPane(btn.dataset.pingPane));
  }
  $('pingForm').addEventListener('submit', event => {
    if (view !== 'general') { event.preventDefault(); event.stopImmediatePropagation(); }
  }, true);
  function applyProfile(data) {
    if (data.plan || data.targets) $('pingTargets').value = data.plan?.targets ?? data.targets;
    if (data.destinations) {
      $('pingDestinations').value = data.destinations.map(d => d.ip + (d.label ? '\t'+d.label : '')).join('\n');
    }
    if (data.plan) {
      const plan = data.plan, opt = plan.options;
      $('pingDestinations').value = plan.destinations.map(d => d.ip + (d.label ? '\t'+d.label : '')).join('\n');
      $('pingSourceMode').value = opt.source_mode;
      $('pingSources').value = Object.entries(plan.source_ips).map(pair=>pair.join(',')).join('\n');
      for (const [id, key] of [['pingRepetitions','repetitions'], ['pingTimeout','timeout'],
           ['pingConcurrency','concurrency'], ['pingVlan','vlan'], ['pingSize','data_size']]) {
        $(id).value = opt[key] ?? '';
      }
    }
    $('pingTargets').dispatchEvent(new Event('input', {bubbles:true}));
    status.textContent = data.plan ? '대상과 옵션을 입력에 적용했습니다.' : data.destinations ? '목적지 서버만 적용했습니다.' : '포트 프로필의 스위치 주소와 SSH 포트를 적용했습니다.';
  }
  const scope = () => $('pingProfileScope').value;
  function payload() {
    if (scope() === 'plan') return {plan:window.readGeneralPingPlan()};
    return {destinations:$('pingDestinations').value.split(/\r?\n/).filter(v=>v.trim()).map(line=>{
      const [ip,...label] = line.split('\t');
      return {ip:ip.trim(),label:label.join('\t').trim()};
    })};
  }
  async function list() {
    const chosen = scope();
    const {names} = await profileApi('list', {scope:chosen});
    if (chosen !== scope()) return;
    const select = $('pingProfileList'), old = select.value;
    select.replaceChildren(...names.map(name => new Option(name, name)));
    if (names.includes(old)) select.value = old;
  }
  function handle(id, action) {
    $(id).addEventListener('click', async () => {
      $(id).disabled = true;
      try { await action(); } catch (error) { status.textContent = error.message; }
      finally { $(id).disabled = false; }
    });
  }
  function saveBlob(blob, name) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = name;
    a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  }
  $('pingProfiles').addEventListener('toggle', async () => {
    if ($('pingProfiles').open) try { await list(); } catch (e) { status.textContent = e.message; }
  });
  $('pingProfileScope').addEventListener('change', () => list().catch(e => {status.textContent=e.message;}));
  handle('pingProfileSave', async () => {
    const name = $('pingProfileName').value.trim();
    await profileApi('save', {name,scope:scope(),...payload()});
    await list(); $('pingProfileList').value = name;
    status.textContent = '이 PC에 프로필을 저장했습니다.';
  });
  handle('pingProfileLoad', async () => applyProfile(await profileApi('load', {name:$('pingProfileList').value,scope:scope()})));
  handle('pingProfileDelete', async () => {
    const name = $('pingProfileList').value;
    if (name && confirm('선택한 Ping 프로필을 삭제할까요? ' + name)) {
      await profileApi('delete', {name,scope:scope()}); await list(); status.textContent = '프로필을 삭제했습니다.';
    }
  });
  handle('pingProfileDownload', async () => {
    const doc = await profileApi('portable', {scope:scope(),...payload()});
    saveBlob(new Blob([JSON.stringify(doc,null,2)], {type:'application/json'}), 'ping-profile.json');
    status.textContent = 'Ping 프로필 파일을 다운로드했습니다.';
  });
  $('pingProfileFile').addEventListener('change', async event => {
    const file = event.target.files[0];
    event.target.value = '';
    if (!file) return;
    try {
      if (file.size > 1048576) throw new Error('1 MiB 이하의 JSON 파일을 선택하세요.');
      applyProfile(await profileApi('import', {text:await file.text()}));
    } catch (error) { status.textContent = error.message; }
  });
  for (const [id, scope] of [['pingExportFiltered','filtered'],['pingExportSelected','selected']]) {
    $(id).addEventListener('click', async () => {
      const message = $('pingExportStatus');
      try {
        const choice = window.pingExportSelection();
        if (!choice.ready || !choice[scope].length) throw new Error('종료한 작업에서 내보낼 조합을 선택하세요.');
        const response = await fetch('/api/ping/runs/'+choice.id+'/export', {
          method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json','X-Local-Token':ui.token},
          body:JSON.stringify({indices:choice[scope]})
        });
        if (!response.ok) throw new Error('Excel 저장 실패 · 작업 상태 및 저장 공간을 확인하세요.');
        saveBlob(await response.blob(), 'ping-'+choice.id+'.xlsx');
        message.textContent = choice[scope].length + '개 조합을 Excel로 저장했습니다.';
      } catch (error) { message.textContent = error.message; }
    });
  }
})();
