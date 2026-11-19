'use strict';
(() => {
  Object.assign(stateNames, {queued:'승인 대기',preflight:'사전 검증'});
  const single = $('singleControls');
  const inspection = elem('div',undefined,'output');
  inspection.hidden = true;
  single.after(inspection);
  function liveButton(id,title,onClick) {
    const node = button(title,onClick);
    node.id = id;
    node.dataset.job = '';
    return node;
  }
  function hideInspection() { inspection.hidden = true; inspection.replaceChildren(); }
  for (const id of ['singleIP','singlePort','controlMode']) $(id).addEventListener('input',hideInspection);
  function inspectSingle() {
    hideInspection();
    const ip = $('singleIP').value.trim(), port = $('singlePort').value.trim();
    if (!ip || !port) return notify('스위치 주소와 한 개의 포트를 입력하세요.',true);
    runSsh('control.inspect',{targets:[{ip,port}]},(result,cancelled) => {
      if (cancelled || !result.snapshot || $('singleIP').value.trim()!==ip || $('singlePort').value.trim()!==port) return;
      showInspection(result.snapshot);
    });
  }
  const inspect = button('현재 상태 조회',inspectSingle);
  inspect.id='inspectSingle'; inspect.dataset.job='';
  single.append(inspect);
  function showInspection(s) {
    inspection.hidden = false;
    inspection.replaceChildren(elem('b','현재 포트 상태'),simpleTable([
      ['주소',r=>r.ip + ':' + r.sshPort],['포트','port'],['호스트','hostname'],['모델','model'],
      ['현재 상태','detail',true],['공급 상태',r=>r.poeStatus || '미측정'],['관측 시각',r=>dateText(r.observed)]
    ],[s]));
  }
  const quickResult = elem('div',undefined,'output');
  quickResult.id = 'quickResult'; quickResult.hidden = true;
  $('controlResult').before(quickResult);
  function clearQuickResult() {
    quickResult.hidden = true;
    quickResult.replaceChildren();
  }
  function showQuickResult(result,cancelled,restored=false) {
    finishProgress();
    if (!restored) receiveControlObservations(result);
    if (!result.operation) return;
    const state = result.operation==='poe_off' ? 'OFF' : 'ON';
    const title = 'PoE ' + state + ' 즉시 실행';
    const skipped=result.skipped || [], blocked=result.blocked || [];
    $('controlPanel').open=true;
    quickResult.hidden=false;
    quickResult.replaceChildren(elem('b',title + (cancelled ? ' 취소 결과' : ' 결과')),
      elem('p','선택 ' + result.selected + '개 · 사전 재조회 ' + result.rechecked + '개 · 이미 ' + state + ' ' + skipped.length + '개'));
    if (blocked.length) quickResult.append(elem('p',result.message || '사전 검증 실패로 선택한 전체 포트의 실행을 보류했습니다.','warning'),
      simpleTable([['IP','ip'],['SSH 포트','sshPort'],['포트','port'],['보류 사유','detail',true]],blocked));
    if (skipped.length) {
      const observed=new Map((result.rows || []).map(row=>[portKey(row),row]));
      quickResult.append(simpleTable([['IP','ip'],['SSH 포트','sshPort'],['포트','port'],['사전 조회 전력',target=>{
        const row=observed.get(controlPortKey(target));
        return powerCell(PowerMatrix.classify(row).watts,row?.observed_at);
      }],['건너뛴 이유','detail']],skipped));
    }
    if (result.results?.length) {
      showControlResults(result,title);
    }
  }
  function requestQuick(operation) {
    if (ui.busy || !controlContextMatches(null) || $('controlMode').value!=='batch') return;
    const targets = window.portTools.checkedTargets();
    if (!targets.length) return notify('빠른 제어를 실행할 포트를 체크하세요.',true);
    if (targets.length > 300) return notify('최대 300포트까지 한 번에 처리할 수 있습니다.',true);
    clearQuickResult();
    finishProgress();
    $('controlResult').replaceChildren(); $('controlResult').hidden = true;
    const requestId = crypto.randomUUID().replaceAll('-','');
    runSsh('control.quick-execute',{operation,targets,requestId},(result,cancelled)=>showQuickResult(result,cancelled));
  }
  $('quickActions').append(liveButton('quickOff','PoE OFF 즉시 실행',()=>requestQuick('poe_off')),
               liveButton('quickOn','PoE ON 즉시 실행',()=>requestQuick('poe_on')));
  const progress = elem('details'); progress.id = 'controlProgress'; progress.hidden = true;
  progress.append(elem('summary','포트별 실행 단계'));
  const stageTable = elem('div'); progress.append(stageTable);
  $('controlResult').before(progress);
  function onProgress(action,data) {
    if (!['control.apply','control.quick-execute'].includes(action) || !data?.ports?.length) return;
    progress.hidden = false;
    stageTable.replaceChildren(simpleTable([['스위치','ip'],['SSH 포트','sshPort'],['포트','port'],
      ['단계',r=>stateNames[r.stage] || r.stage],['결과','message',true]],data.ports));
  }
  function finishProgress() { progress.hidden = true; stageTable.replaceChildren(); hideInspection(); }
  window.controlTools = {onProgress,finishProgress,showInspection,hideInspection,showQuickResult,clearQuickResult};
  sync();
})();
