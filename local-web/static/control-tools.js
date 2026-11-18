'use strict';
(() => {
  Object.assign(stateNames, {queued:'승인 대기',preflight:'사전 검증'});
  const single = $('singleControls'), batch = $('batchControls');
  const inspection = elem('div',undefined,'output');
  inspection.hidden = true;
  single.after(inspection);
  function liveButton(id,title,onClick) {
    const node = button(title,onClick);
    node.id = id;
    node.dataset.live = '';
    return node;
  }
  function hideInspection() { inspection.hidden = true; inspection.replaceChildren(); }
  for (const id of ['singleIP','singlePort','controlMode']) $(id).addEventListener('input',hideInspection);
  single.append(liveButton('inspectSingle','현재 상태 조회',() => {
    hideInspection();
    const ip = $('singleIP').value.trim(), port = $('singlePort').value.trim();
    if (!ip || !port) return notify('스위치 주소와 한 개의 포트를 입력하세요.',true);
    run('control.inspect',{targets:[{ip,port}]},(result,cancelled) => {
      if (cancelled || !result.snapshot) return;
      showInspection(result.snapshot);
    });
  }));
  function showInspection(s) {
    inspection.hidden = false;
    inspection.replaceChildren(elem('b','현재 포트 상태'),simpleTable([
      ['주소',r=>r.ip + ':' + r.sshPort],['포트','port'],['호스트','hostname'],['모델','model'],
      ['현재 상태','detail',true],['공급 상태',r=>r.poeStatus || '미측정'],['관측 시각',r=>dateText(r.observed)]
    ],[s]));
  }
  const quickResult = elem('div',undefined,'output');
  quickResult.id = 'quickResult'; quickResult.hidden = true;
  batch.after(quickResult);
  function selectedTargets() {
    return window.portTools?.checkedTargets() || ui.rows.filter(row=>ui.selected.has(portKey(row))).map(targetFromRow);
  }
  function keys(targets) {
    return targets.map(r=>[r.ip,r.sshPort || 22,r.port].join('|')).sort().join('\n');
  }
  function requestQuick(operation) {
    const targets = selectedTargets();
    const selection = keys(targets);
    if (!targets.length) return notify('빠른 제어를 실행할 포트를 체크하세요.',true);
    if (targets.length > 300) return notify('최대 300포트까지 한 번에 처리할 수 있습니다.',true);
    quickResult.hidden = true;
    $('controlResult').replaceChildren(); $('controlResult').hidden = true;
    run('control.quick',{operation,targets},(plan,cancelled) => {
      if (cancelled) return;
      quickResult.hidden = false;
      if (keys(selectedTargets()) !== selection || $('controlMode').value !== 'batch') {
        quickResult.replaceChildren(elem('p','대상 선택이 변경되었습니다. 현재 선택을 확인하고 다시 검토하세요.'));
        return;
      }
      if (Array.isArray(plan.rows)) {
        const checked = new Set(ui.selected);
        receivePorts(plan);
        ui.selected = checked;
        renderPorts();
      }
      quickResult.replaceChildren(elem('p','선택 ' + plan.selected + '개 · 사전 재조회 ' + plan.rechecked +
          '개 · 변경 대상 ' + (plan.targets?.length || 0) + '개 · 이미 ON ' + plan.skipped.length + '개'));
      if (plan.blocked.length) {
        quickResult.append(elem('b','다음 포트의 사전 검증에 실패하여 전체 작업을 보류했습니다.'),
          simpleTable([['IP','ip'],['SSH 포트','sshPort'],['포트','port'],['보류 사유','detail',true]],plan.blocked));
      }
      if (plan.skipped.length) quickResult.append(simpleTable([['IP','ip'],['SSH 포트','sshPort'],['포트','port'],['결과','detail']],plan.skipped));
      if (plan.planId) renderPlan(plan);
    });
  }
  batch.append(liveButton('quickOff','빠른 PoE OFF',()=>requestQuick('poe_off')),
               liveButton('quickOn','빠른 PoE ON',()=>requestQuick('poe_on')));
  const progress = elem('details'); progress.id = 'controlProgress'; progress.hidden = true;
  progress.append(elem('summary','포트별 실행 단계'));
  const stageTable = elem('div'); progress.append(stageTable);
  $('controlResult').before(progress);
  function onProgress(action,data) {
    if (action !== 'control.apply' || !data?.ports?.length) return;
    progress.hidden = false;
    stageTable.replaceChildren(simpleTable([['스위치','ip'],['SSH 포트','sshPort'],['포트','port'],
      ['단계',r=>stateNames[r.stage] || r.stage],['결과','message',true]],data.ports));
  }
  function finishProgress() { progress.hidden = true; stageTable.replaceChildren(); hideInspection(); }
  const file = elem('input'); file.type = 'file'; file.accept = '.jsonl'; file.hidden = true;
  const opener = button('JSONL 이력 파일 열기',()=>{ file.value = ''; file.click(); });
  opener.id = 'importControlHistory'; opener.dataset.job = '';
  $('controlHistory').after(opener,file);
  file.onchange = async () => {
    const selected = file.files[0]; if (!selected) return;
    if (selected.size > 2 * 1024 * 1024) return notify('2 MiB 이하의 JSONL 파일을 선택하세요.',true);
    try {
      const text = await selected.text();
      run('control.import-history',{name:selected.name,text},r => {
        $('controlHistoryTable').replaceChildren(elem('b',selected.name),simpleTable([
          ['시각',x=>dateText(x.time)],['IP','ip'],['SSH 포트','sshPort'],['포트','port'],['호스트','hostname'],
          ['작업',x=>operationNames[x.operation] || x.operation],['결과',x=>stateNames[x.status] || x.status]
        ],r.rows || []));
        explainUnknown(r.rows || []);
      });
    } catch { notify('선택한 JSONL 파일을 읽을 수 없습니다.',true); }
  };
  function explainUnknown(rows) {
    if (rows.some(row=>row.status === 'result_unknown' || row.status === 'unverified')) {
      $('controlHistoryTable').prepend(elem('p','명령의 결과가 확인되지 않은 포트가 있습니다. 해당 포트의 현재 상태를 다시 조회하세요.','warning'));
    }
  }
  window.controlTools = {onProgress,finishProgress,explainUnknown,showInspection};
  sync();
})();
