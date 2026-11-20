'use strict';

const $ = id => document.getElementById(id);
const all = (selector, parent = document) => [...parent.querySelectorAll(selector)];
const operationNames = {poe_on: 'Power ON', poe_off: 'Power OFF', enable: '포트 활성화 (Enable)', disable: '포트 비활성화 (Disable)'};
const jobNames = {
  'session.open': '접속 계정 적용', 'session.close': '세션 해제', 'hostkey.inspect': '호스트 키 조회',
  'hostkey.approve': '호스트 키 승인', 'hostkey.list':'등록된 키 조회', 'hostkey.forget':'등록된 키 삭제', 'ports.query': '포트 조회', 'ports.refresh':'선택 범위 재조회',
  'profiles.export': '대상 프로필 저장', 'profiles.import': '대상 프로필 열기',
  'backup.run': '스위치 컨피그 백업', 'control.preview': '포트 제어 검토', 'control.apply': '포트 명령 실행'
};
const stateNames = {ok:'성공',success:'성공',failed:'실패',cancelled:'취소',unsupported:'미지원',unknown:'미등록',changed:'키 변경',trusted:'등록됨',rejected:'거부',retry_exhausted:'접속 재시도 소진',verified:'검증 완료',unchanged:'이미 요청 상태',preflight_failed:'사전 검증 실패',unverified:'검증 미완료',paused:'일시 중지',skipped:'건너뜀',blocked:'실행 차단',result_unknown:'결과 미기록'};
const ui = { token:'', session:{active:false}, busy:false, jobId:null, rows:[], selected:new Set(), tileRow:null, liveId:null, inspectedRow:null };
const dialog = $('review');
const queryCredentials = $('queryCredentials');
Object.assign(jobNames, {'targets.preview':'대상 목록 검토','targets.apply':'대상 목록 적용','control.inspect':'단일 포트 상태 조회','control.quick':'빠른 제어 검토','control.quick-execute':'빠른 PoE 제어','backup.pick-directory':'백업 폴더 선택'});

function elem(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function button(text, fn, className) {
  const node = elem('button', text, className);
  node.type = 'button';
  node.addEventListener('click', fn);
  return node;
}
function notify(message, error = false, inMatrix = false) {
  $('statusBar').classList.toggle('query-complete',inMatrix);
  $('statusBar').hidden = !message;
  $('statusBar').dataset.level = error ? 'error' : '';
  if ($('statusText').textContent!==message) $('statusText').textContent = message;
  $('dismissStatus').hidden = ui.busy;
}
const cableStatusLabels = {
  measured:'측정 완료', partial:'부분 측정', empty:'측정값 없음',
  pending:'테스트 진행 중', unsupported:'미지원', failed:'실패', uncertain:'측정 결과 미확인', cancelled:'취소됨', skipped:'건너뜀'
};
function notifyCableResult(result, cancelled) {
  const rows = result.rows || [];
  const counts = new Map();
  for (const row of rows) {
    const label = cableStatusLabels[row.status] || '측정 결과 미확인';
    counts.set(label, (counts.get(label) || 0) + 1);
  }
  const parts = [cancelled ? '케이블 진단 취소됨' : '케이블 진단'];
  for (const [label, count] of counts) parts.push(label + ' ' + count + '포트');
  if (!rows.length && !cancelled) parts.push('받은 결과 없음');
  notify(parts.join(' · '), !cancelled && !rows.some(row => row.status === 'measured' || row.status === 'partial'));
}
function setJobProgress(progress) {
  const meter=$('jobProgress'), percent=$('jobPercent');
  meter.hidden=progress===null;
  const total=progress?.total, completed=progress?.completed;
  const counted=Number.isSafeInteger(total) && total>0 &&
    Number.isSafeInteger(completed) && completed>=0 && completed<=total;
  percent.hidden=!counted;
  const received=progress?.powerRead;
  const hasReadings=counted && Number.isSafeInteger(received) && received>=0 && received<=total;
  meter.style.setProperty('--power-read',hasReadings ? received*100/total+'%' : '0%');
  if (counted) {
    meter.max=total;
    meter.value=completed;
    meter.setAttribute('aria-valuetext',completed+' / '+total);
    percent.textContent=Math.floor(completed*100/total)+'%';
  } else {
    meter.removeAttribute('value');
    meter.removeAttribute('aria-valuetext');
    percent.textContent='';
  }
}
function sync() {
  const active = ui.session.active;
  $('connectionState').textContent = active ? ui.session.username + ' 계정 적용 중' : '계정 미설정';
  $('logout').disabled = ui.busy || !ui.token || !active;
  for (const node of all('[data-job]')) node.disabled = ui.busy || !ui.token;
  $('queryTargets').readOnly = ui.busy;
  document.querySelector('[data-import="queryTargets"]').disabled = ui.busy;
  monitor()?.setControlBusy?.(ui.busy);
  $('stopJob').hidden = !ui.jobId;
  $('dismissStatus').hidden = ui.busy;
  $('shutdown').disabled = ui.busy || !ui.token;
  updateControlTarget();
  window.cableTools?.sync();
  window.powerMonitoring?.sync();
}
async function api(path, data, headers) {
  const options = {credentials:'same-origin', cache:'no-store', headers};
  if (data !== undefined) {
    options.method = 'POST';
    options.headers = {'Content-Type':'application/json','X-Local-Token':ui.token};
    options.body = JSON.stringify(data);
  }
  let response;
  try { response = await fetch(path, options); }
  catch { throw new Error('로컬 서버 응답이 없습니다. 실행 창과 연결 상태를 확인하세요.'); }
  let json;
  try { json = await response.json(); }
  catch { throw new Error('로컬 서버에서 응답을 읽을 수 없습니다. Start-Local-Web.cmd로 웹 화면을 다시 여세요.'); }
  if (!response.ok) {
    const error = new Error(json.error || '로컬 요청 실패 (' + response.status + ')');
    error.status = response.status;
    throw error;
  }
  return json;
}
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function poll(id,resuming=false,responseKey=null) {
  const headers=responseKey ? {'X-Cable-Response-Key':responseKey} : undefined;
  let failures = 0;
  let cursor = 0;
  for (;;) {
    let job;
    try { job = await api('/api/jobs/' + encodeURIComponent(id) + '?after=' + cursor,undefined,headers); failures = 0; }
    catch (e) {
      if (++failures >= 3) throw new Error(e.message + ' 작업 상태를 확인할 수 없습니다. 잠시 후 다시 연결하세요.');
      await delay(850);
      continue;
    }
    if (job.state === 'done') return {action:job.action, result:job.result || {}, cancelled:false};
    if (job.state === 'cancelled') return {action:job.action, result:job.result || {}, cancelled:true};
    if (job.state === 'failed') throw new Error(job.error || '작업이 실패했습니다.');
    if (!['running','queued'].includes(job.state)) throw new Error('작업 상태를 읽을 수 없습니다.');
    if (resuming && job.action==='ports.query' && job.progress?.targets?.length) {
      window.portTools?.resumeQuery(job.progress);
    }
    window.portTools?.onProgress?.(job.action,job.progress || {});
    window.backupTools?.onProgress?.(job.action,job.progress || {});
    window.controlTools?.onProgress?.(job.action,job.progress || {});
    if (job.action==='cable.test') window.cableTools?.showProgress(id,job.progress?.cableRows,resuming);
    if (Number.isInteger(job.progress?.cursor)) cursor = job.progress.cursor;
    if (job.progress?.message && !$('stopJob').disabled) {
      setJobProgress(job.progress);
      const {message,completed,total} = job.progress;
      const counts=total>0 && Number.isSafeInteger(job.progress.powerRead)
        ? ' · PoE 수집 '+job.progress.powerRead+'/'+total+' · 포트 완료 '+completed+' / '+total
        : total>0 ? ' ('+completed+' / '+total+')' : '';
      const prefix=job.action==='ports.query' ? '' : (jobNames[job.action] || '작업')+' · ';
      notify(prefix+message+counts);
    }
    await delay(450);
  }
}
async function run(action, body, receive, retryKeys=null) {
  if (ui.busy || !ui.token) return;
  ui.busy = true;
  window.powerMonitoring?.foregroundStarted();
  $('stopJob').disabled = false;
  sync();
  const quick = action === 'control.quick-execute' ? body : null;
  const requestId = quick || action==='cable.test' ? body.requestId : null;
  const responseKey = action==='cable.test' ? body.responseKey : null;
  setJobProgress({});
  notify(quick ? operationNames[quick.operation] + ' 즉시 실행 · 선택 ' + quick.targets.length + '개' : jobNames[action] + ' 진행 중…');
  try {
    const submitted = await api('/api/jobs', {action,body}).catch(error=>{
      if (!requestId || (error.status && error.status < 500)) throw error;
      // The server may have accepted the operation. Read its original job without submitting again.
      return {id:requestId};
    });
    window.portTools?.onJob?.(action,'running',body,!!retryKeys);
    body = null;
    ui.jobId = submitted.id;
    sync();
    const {result,cancelled} = await poll(ui.jobId,false,responseKey);
    if (action==='ports.query' && !Array.isArray(result.rows)) {
      await window.portTools.recoverQuery();
      setQueryWorkspace(false);
    } else if (receive) receive(result, cancelled);
    if (action === 'cable.test') {
      notifyCableResult(result, cancelled);
    } else if (['control.apply','control.quick-execute'].includes(action) && hasUncertainControl(result)) {
      notify(result.message || '포트 제어 결과를 확인하지 못했습니다.',true);
    } else if (action === 'control.quick-execute' && result.blocked?.length) {
      notify(result.message || '선택한 포트의 사전 검증에 실패했습니다.',true);
    } else if (result.powerRefresh?.readings?.length) {
      const readings=result.powerRefresh.readings, measured=readings.filter(isMeasuredReading).length;
      const missing=readings.length-measured;
      notify(jobNames[action] + (cancelled ? ' 취소됨' : ' 완료') + ' · 전력 자동 재조회 ' + measured + '개 측정' +
        (missing ? ' · ' + missing + '개 미측정' : '') + singlePowerChange(result),missing>0);
    } else {
      // Refresh returns the whole matrix, including errors from earlier requests.
      const readings=retryKeys ? result.rows?.filter(row=>retryKeys.has(portKey(row))) : result.rows;
      if (!cancelled && retryKeys && readings?.length!==retryKeys.size) {
        notify('재조회한 포트의 결과를 확인할 수 없습니다.',true);
        return;
      }
      const label=retryKeys && readings?.length===1
        ? endpoint(readings[0])+' 포트 '+readings[0].interface+' 재조회' : jobNames[action];
      if ((action==='ports.query' || retryKeys) && !cancelled && Array.isArray(readings)) {
        const failed=readings.filter(portReadFailed);
        if (failed.length || !readings.some(portObserved)) {
          const reason=failed.length ? PowerMatrix.readFailure(failed[0]) : readings.find(row=>row.message)?.message || result.discoveryErrors?.[0]?.message;
          const summary=failed.length ? '조회 실패 '+failed.length+'포트' : '조회에 성공한 포트가 없습니다.';
          const partial=result.rows.some(portObserved);
          notify(label+' 완료 · '+summary+(reason?' · '+reason:''),!partial,partial);
          return;
        }
      }
      const shownInMatrix=!cancelled && ['ports.query','ports.refresh'].includes(action) &&
        readings?.length>0 && readings.every(portObserved);
      notify(label + (cancelled ? ' 취소됨. 완료된 항목은 결과에 표시됩니다.' : ' 완료'),false,shownInMatrix);
    }
  } catch (error) {
    if (action==='ports.query') {
      await window.portTools.recoverQuery();
      setQueryWorkspace(false);
    }
    if (quick) window.controlTools.finishProgress();
    if (quick && ui.jobId) {
      markControlRowsUsed(quick.targets);
      notify(error.message + ' 선택한 포트의 현재 상태를 다시 조회하세요.',true);
    } else notify(error.message, true);
  } finally {
    setJobProgress(null);
    ui.jobId = null;
    ui.busy = false;
    sync();
    window.portTools?.onJob?.(action,'idle');
    window.powerMonitoring?.wake();
  }
}
$('stopJob').onclick = async () => {
  if (!ui.jobId) return;
  $('stopJob').disabled = true;
  try {
    await api('/api/jobs/' + encodeURIComponent(ui.jobId) + '/cancel', {});
    if (ui.busy) {
      setJobProgress({});
      notify('취소를 요청했습니다. 스위치에서 진행 중인 단계가 끝날 때까지 기다려 주세요.');
    }
  } catch (e) { if (ui.busy) { notify(e.message, true); $('stopJob').disabled = false; } }
};
$('dismissStatus').onclick = () => notify('');

const themeToggle = $('themeToggle');
function theme(value) {
  document.documentElement.dataset.theme = value;
  const switchToLight = value === 'dark';
  const label = switchToLight ? '라이트' : '다크';
  themeToggle.querySelector('use').setAttribute('href', switchToLight ? '#i-sun' : '#i-moon');
  themeToggle.querySelector('span').textContent = label;
  themeToggle.title = label + ' 모드로 전환';
  themeToggle.setAttribute('aria-label', themeToggle.title);
  for (const frame of all('iframe')) {
    frame.contentWindow?.postMessage({type:'aruba-theme',theme:value}, location.origin);
  }
}
themeToggle.onclick = () => {
  const value = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  theme(value);
  try { localStorage.setItem('aruba2930f.portMatrix.theme',value); } catch {}
};
let savedTheme;
try { savedTheme = localStorage.getItem('aruba2930f.portMatrix.theme'); } catch {}
theme(savedTheme === 'light' ? 'light' : 'dark');
function monitor() {
  return $('matrixViewer').contentWindow?.PortMonitor;
}
let observationSequence=0;
function displayingInventory() {
  return !ui.liveId && !!window.portTools?.hasInventory();
}
function currentInspection() {
  return ui.inspectedRow && ui.inspectedRow===monitor()?.selected();
}
function controlContextMatches(inspected) {
  return !$('portMonitor').closest('[data-page]').hidden &&
    (inspected ? ui.inspectedRow===inspected && currentInspection() : !ui.inspectedRow);
}
function controlChoice() {
  const mode=$('controlMode').value;
  const targets=mode==='single' ? [$('singleIP').value.trim(),$('singlePort').value.trim()] : matrixControlTargets().map(controlPortKey).sort();
  return JSON.stringify([mode,$('operation').value,targets]);
}
function freshControlRow(row) {
  return row.status==='ok' && !row.controlUsed && !row.refreshInvalidated;
}
function matrixControlTargets(operation=$('operation').value) {
  const viewer=monitor();
  if (ui.liveId) {
    const checked=viewer?.batch();
    if (!checked || checked.liveId!==ui.liveId || checked.rows.length!==ui.selected.size ||
        checked.rows.some(target=>!ui.selected.has(controlPortKey(target)))) return [];
  }
  const tile=ui.tileRow;
  if (!ui.selected.size && (!tile || tile!==viewer?.selected())) return [];
  const keys=ui.selected.size ? ui.selected : new Set([controlPortKey(tile)]);
  const targets=window.portTools.availableControlTargets(operation).filter(target=>keys.has(controlPortKey(target)));
  return targets.length===keys.size ? targets : [];
}
function showChangedControlChoice() {
  $('controlResult').replaceChildren(elem('p','검토 중 제어 대상 또는 작업이 변경되었습니다. 현재 선택으로 다시 검토하세요.'));
  $('controlResult').hidden=false;
}
function clearInspection() {
  if (!ui.inspectedRow) return;
  ui.inspectedRow=null;
  $('singleIP').value=$('singlePort').value='';
  window.controlTools?.hideInspection();
  $('controlMode').value='batch';
  $('controlMode').onchange();
  updatePortActions();
}
function updatePortActions() {
  const source=monitor()?.source();
  const inventory=displayingInventory();
  const inspected=!!currentInspection() && !inventory;
  $('matrixViewer').hidden=inventory;
  $('portMonitor').classList.toggle('has-data',!!source?.hasData);
  const showViews=!!ui.liveId && source?.liveId===ui.liveId;
  const list=showViews && source.presentation==='list';
  $('portPresentation').hidden=!showViews;
  $('matrixView').setAttribute('aria-pressed',String(!list));
  $('listView').setAttribute('aria-pressed',String(list));
  $('matrixWorkspaceTitle').textContent=list ? '포트별 조회 결과' : '포트 상태 매트릭스';
  $('controlMode').disabled=inspected;
  $('singleIP').readOnly=$('singlePort').readOnly=inspected;
  window.portTools?.showInventory(inventory);
  updateControlTarget();
}
$('matrixView').onclick=()=>monitor()?.setPresentation('matrix');
$('listView').onclick=()=>monitor()?.setPresentation('list');
function setQueryExpanded(expanded) {
  $('queryFields').hidden=!expanded;
  const edit=$('editQueryTargets');
  edit.setAttribute('aria-expanded',String(expanded));
  edit.textContent=expanded?'설정 닫기':'조회 설정';
}
function setQueryWorkspace(active) {
  $('matrixWorkspace').closest('[data-page]').classList.toggle('matrix-first',active);
  $('editQueryTargets').hidden=!active;
  $('queryPorts').textContent=active?'전체 재조회':'포트 조회';
  setQueryExpanded(!active);
}
function portObserved(row) {
  return row.status==='ok' || row.status==='unsupported';
}
function portReadFailed(row) {
  return !!PowerMatrix.readFailure(row);
}
function updateQuerySummary() {
  const failedCount=ui.rows.filter(portReadFailed).length;
  const retry=$('retryFailedPorts');
  retry.hidden=!failedCount || !ui.rows.some(portObserved);
  retry.textContent='실패 포트만 재조회 ('+failedCount+')';
  const last=$('lastPortQuery');
  let latest=0;
  for (const row of ui.rows) {
    const time=Date.parse(row.finished_at || row.observed_at);
    if (time>latest) latest=time;
  }
  last.hidden=!latest;
  if (latest) {
    const stamp=new Date(latest);
    last.dateTime=stamp.toISOString();
    last.textContent='최근 조회 '+stamp.toLocaleTimeString('ko-KR',{hour12:false});
  }
}
$('editQueryTargets').onclick=()=>{
  const open=$('queryFields').hidden || !$('queryTargets').value.trim();
  setQueryExpanded(open);
  if (open) $('queryTargets').focus();
};
$('controlPanel').addEventListener('toggle',updateControlTarget);
$('portLiveControl').addEventListener('keydown',event=>{
  if (event.key==='Escape' && $('controlPanel').open) {
    event.preventDefault();
    $('controlPanel').open=false;
    $('controlHeading').focus({preventScroll:true});
  }
});
function selectObservedPort(message) {
  if ($('portMonitor').closest('[data-page]').hidden || ui.busy) return;
  const row=monitor()?.selected();
  if (!row || row.key!==message.key || !ui.liveId || message.liveId!==ui.liveId || monitor().source().liveId!==ui.liveId) return;
  clearInspection();
  ui.tileRow=ui.selected.size ? null : row;
  $('controlMode').value='batch'; $('controlMode').onchange();
}
function selectObservedBatch() {
  if ($('portMonitor').closest('[data-page]').hidden || ui.busy) return;
  const batch=monitor()?.batch();
  if (!batch?.liveId || batch.liveId!==ui.liveId || !Array.isArray(batch.rows)) return;
  const available=new Set(window.portTools.availableControlTargets().map(controlPortKey));
  const keys=batch.rows.map(controlPortKey);
  if (keys.some(key=>!available.has(key))) { syncMonitorChecks(); return; }
  clearInspection();
  ui.tileRow=null;
  ui.selected=new Set(keys);
  $('controlMode').value='batch'; $('controlMode').onchange();
  renderPorts();
}
function postMatrix(keepView=false,periodic=false) {
  const viewer=monitor(), id=ui.liveId;
  if (viewer && id) viewer.loadLive(id,ui.rows,()=>ui.liveId===id,window.portTools.unobservedTargets(),keepView,periodic,()=>ui.tileRow?.key);
}
window.addEventListener('message',e=>{
  if (e.origin !== location.origin || !e.data) return;
  if (e.source === $('matrixViewer').contentWindow) {
    if (e.data.type==='aruba-observed-port') selectObservedPort(e.data);
    if (e.data.type==='aruba-retry-failed-port') {
      const viewer=monitor(), row=viewer?.selected();
      if (ui.liveId && e.data.liveId===ui.liveId && viewer?.source().liveId===ui.liveId && row && row.key===e.data.key) {
        retryFailedPorts(controlPortKey(row));
      }
    }
    if (e.data.type==='aruba-port-checks') selectObservedBatch();
    if (e.data.type==='aruba-port-context') {
      if (ui.tileRow!==monitor()?.selected()) ui.tileRow=null;
      if (!currentInspection()) clearInspection();
      updateControlTarget();
    }
    if (e.data.type==='aruba-port-view') updatePortActions();
    if (e.data.type==='aruba-port-source') {
      if (!currentInspection()) clearInspection();
      renderPorts();
    }
  }
});
let noticeObserver;
function fitEmptyViewer(frame) {
  noticeObserver?.disconnect();
  const sourceBar=frame.contentDocument.getElementById('sourceBar');
  if (!sourceBar) return;
  noticeObserver=new ResizeObserver(()=>{
    if (!frame.clientWidth || monitor()?.source().hasData) return;
    frame.style.setProperty('--empty-view-height',Math.ceil(sourceBar.getBoundingClientRect().bottom+18)+'px');
  });
  noticeObserver.observe(sourceBar);
}
const matrixFrame=$('matrixViewer');
matrixFrame.addEventListener('load', () => {
  if (matrixFrame.contentWindow.location.pathname !== '/matrix') return;
  matrixFrame.contentWindow.postMessage({type:'aruba-theme',theme:document.documentElement.dataset.theme},location.origin);
  fitEmptyViewer(matrixFrame);
  postMatrix();
  updatePortActions();
});
const phoneLayout = matchMedia('(max-width:650px), (max-width:950px) and (max-height:500px)');
const mobileServerInfo = $('mobileServerInfo');
function placeServerInfo() {
  mobileServerInfo.open = false;
  const container = phoneLayout.matches ? mobileServerInfo : document.querySelector('.sidebar');
  container.append($('serverInfo'));
}
phoneLayout.addEventListener('change', placeServerInfo);
placeServerInfo();
document.addEventListener('click', event => {
  if (mobileServerInfo.open && !mobileServerInfo.contains(event.target)) mobileServerInfo.open = false;
});
mobileServerInfo.addEventListener('focusout', event => {
  if (!mobileServerInfo.contains(event.relatedTarget)) mobileServerInfo.open = false;
});
mobileServerInfo.addEventListener('keydown', event => {
  if (event.key === 'Escape' && mobileServerInfo.open) {
    mobileServerInfo.open = false;
    mobileServerInfo.querySelector('summary').focus();
  }
});
function route() {
  const links = all('nav a');
  const admin = location.hash === '#connect';
  const link = admin ? null : links.find(a => a.hash === location.hash) || links[0];
  if (link && location.hash !== link.hash) history.replaceState(history.state,'',link.hash);
  const name = admin ? 'connect' : link.hash.slice(1);
  for (const node of links) {
    if (node === link) node.setAttribute('aria-current','page'); else node.removeAttribute('aria-current');
  }
  for (const node of all('[data-page]')) node.hidden = node.dataset.page !== name;
  mobileServerInfo.open = false;
  const page = document.querySelector('[data-page]:not([hidden])');
  $('crumb').textContent = page.getAttribute('aria-label') || page.querySelector('h1').textContent;
  if (name === 'ports' && !$('matrixViewer').hasAttribute('src')) $('matrixViewer').src = $('matrixViewer').dataset.src;
  if (dialog.open) dialog.close();
  if (queryCredentials.open) queryCredentials.close();
  if (name === 'cable') window.cableTools?.sync();
}
window.addEventListener('hashchange',route);
route();

function simpleTable(columns, items, message) {
  if (!items.length) return elem('p',message || '표시할 결과가 없습니다.','empty');
  const wrap = elem('div',undefined,'tablebox result-table'), table = elem('table'), header = table.createTHead().insertRow(), body = table.createTBody();
  table.setAttribute('role','table');
  for (const [label] of columns) {
    const heading = elem('th',label);
    heading.scope = 'col';
    header.append(heading);
  }
  for (const item of items) {
    const tr = body.insertRow();
    for (const [label,field,long] of columns) {
      const cell = tr.insertCell();
      cell.dataset.label = label;
      const value = typeof field === 'function' ? field(item) : item[field];
      if (value instanceof Node) cell.append(value); else cell.textContent = value ?? '—';
      if (long) cell.className = 'long';
    }
  }
  wrap.append(table);
  return wrap;
}
function filelinks(files) {
  const group = elem('div',undefined,'filelinks');
  for (const f of files || []) {
    if (!f.id || !f.name) continue;
    const a = elem('a',f.name);
    a.href = '/download/' + String(f.id).split('/').map(encodeURIComponent).join('/');
    a.download = f.name;
    group.append(a);
  }
  return group;
}
function openFolder(file) {
  if (location.protocol==='https:') return document.createDocumentFragment();
  return button('결과 폴더 열기', async () => {
    try { await api('/api/open-folder',{fileId:file.id}); }
    catch (e) { notify(e.message,true); }
  });
}
function openDialog(title, children) {
  $('reviewTitle').textContent = title;
  $('reviewBody').replaceChildren(...children);
  if (!dialog.open) dialog.showModal();
}
function confirmDialog(title, children, label, action) {
  const confirm = button(label, () => { dialog.close(); action(); },'primary');
  openDialog(title,[...children,confirm]);
}
function form(id, callback) {
  $(id).addEventListener('submit', e => { e.preventDefault(); callback(); });
}
function keyChecklist(checks) {
  const list = elem('div',undefined,'key-list');
  const selected = new Set();
  for (const check of checks) {
    const label = elem('label');
    const box = elem('input'); box.type = 'checkbox';
    box.onchange = () => box.checked ? selected.add(check) : selected.delete(check);
    label.append(box,elem('b',check.endpoint),elem('span',check.key_type),elem('code',check.fingerprint));
    list.append(label);
  }
  return {list, checked:()=>Array.from(selected,c=>({endpoint:c.endpoint,fingerprint:c.fingerprint}))};
}
async function applySshAccount(username, password, enableSecret) {
  const user = username.value.trim();
  const credentials = {username:user, password:password.value, enable_secret:enableSecret.value};
  password.value = ''; enableSecret.value = '';
  let applied = false;
  await run('session.open',credentials,(result,cancelled) => {
    if (!result.connected) return;
    if (ui.session.active || ui.rows.length || ui.liveId || ui.inspectedRow) clearPorts();
    ui.session = {active:true, username:user};
    applied = !cancelled;
  });
  return applied;
}
form('loginForm', () => applySshAccount($('username'), $('password'), $('enableSecret')));
$('logout').onclick = () => run('session.close',{},r => {
  if (r.connected !== false) return;
  ui.session = {active:false}; clearPorts();
  $('password').value = ''; $('enableSecret').value = '';
});
form('inspectForm', () => {
  const targets = $('trustTargets').value.trim();
  $('trustResult').replaceChildren();
  run('hostkey.inspect',{targets},r => renderKeys(r.checks || []));
});
const trustNames = {trusted:'등록된 키',unknown:'처음 발견된 키',changed:'기존 지문과 다름',rejected:'조회 거부'};
function renderKeys(checks) {
  const target = $('trustResult');
  target.hidden = false;
  target.replaceChildren();
  const unknown = checks.filter(item=>item.state === 'unknown' && item.fingerprint);
  if (unknown.length > 1) {
    target.append(button('여러 지문 함께 검토',()=>{
      const choices = keyChecklist(unknown);
      const accept = button('선택한 지문 승인',()=>{
        const picked = choices.checked(); dialog.close();
        run('hostkey.approve',{checks:picked},r=>{
          if (r.approved) {
            for (const item of unknown) if (picked.some(c=>c.endpoint === item.endpoint)) item.state = 'trusted';
            renderKeys(checks);
          }
        });
      },'primary');
      accept.disabled = true;
      choices.list.onchange = ()=>{ accept.disabled = !choices.checked().length; };
      openDialog('SSH 호스트 키 일괄 검토',[elem('p','장비에서 확인한 지문과 대조한 항목을 선택하세요.'),choices.list,accept]);
    }));
  }
  for (const item of checks) {
    const row = elem('div',undefined,'entry'), info = elem('div');
    info.append(elem('b',item.endpoint + ' · ' + (trustNames[item.state] || item.state)),elem('small',item.key_type || ''),elem('code',item.fingerprint || ''));
    if (item.known_fingerprint && item.state === 'changed') info.append(elem('small','저장된 지문 ' + item.known_fingerprint));
    if (item.message) info.append(elem('small',item.message));
    row.append(info);
    if (item.state === 'unknown' && item.fingerprint) {
      const approve = button('지문 검토',() => confirmDialog('SSH 호스트 키 승인',[
        elem('b',item.endpoint),elem('p','장비에서 확인한 SHA256 지문과 일치하는지 대조하세요.'),elem('code',item.fingerprint)
      ],'이 지문 승인',() => run('hostkey.approve',{endpoint:item.endpoint,fingerprint:item.fingerprint},r => {
        if (r.approved) { item.state = 'trusted'; item.message = ''; renderKeys(checks); }
      })));
      approve.dataset.job = '';
      row.append(approve);
    }
    target.append(row);
  }
}
function showStoredKeys(keys) {
  const area = $('knownKeys');
  area.hidden = false;
  area.replaceChildren(simpleTable([
    ['스위치','endpoint'],['종류','key_type'],['SHA256 지문','fingerprint'],['등록 시각',x=>dateText(x.approved_at)],
    ['관리',x=> {
      const forget = button('등록 삭제',()=>confirmDialog('호스트 키 등록 삭제',[
        elem('b',x.endpoint),elem('code',x.fingerprint),elem('p','이 지문을 삭제하면 다음 접속에서 장비가 제시하는 지문을 새로 등록합니다.')
      ],'이 키 삭제',()=>run('hostkey.forget',{endpoint:x.endpoint,fingerprint:x.fingerprint},r=>{
        if (r.removed) {
          showStoredKeys(keys.filter(key=>key!==x));
          $('trustResult').replaceChildren(); $('trustResult').hidden = true; clearPorts();
        }
      })));
      forget.dataset.job = '';
      return forget;
    }]
  ],keys,'등록된 호스트 키가 없습니다.'));
  if (keys.length > 1) {
    area.append(button('여러 저장 지문 제거',()=>{
      const choices = keyChecklist(keys);
      const apply = button('선택한 등록 지문 제거',()=>{
        const checks = choices.checked();
        dialog.close();
        run('hostkey.forget',{checks},r=>{
          if (r.removed) {
            const removed = new Set(checks.map(c=>c.endpoint));
            showStoredKeys(keys.filter(k=>!removed.has(k.endpoint)));
            $('trustResult').replaceChildren(); $('trustResult').hidden = true; clearPorts();
          }
        });
      });
      apply.disabled = true;
      choices.list.onchange = ()=>{ apply.disabled = !choices.checked().length; };
      openDialog('저장 지문 제거',[elem('p','선택한 지문을 제거하면 해당 장비가 다음 접속에서 제시하는 지문을 새로 등록합니다.'),choices.list,apply]);
    }));
  }
}
$('listKeys').onclick = () => run('hostkey.list',{},r=>showStoredKeys(r.keys || []));
function backup(body) {
  runSsh('backup.run',body,r => showBackupResult(r,body));
}
function showBackupResult(r,body) {
  const rows = r.rows || [], area = $('backupResult');
  $('backupFiles').replaceChildren(filelinks(r.files));
  area.replaceChildren(simpleTable([
    ['스위치','endpoint'],['호스트 이름','hostname'],['모델 / SKU',x=>[x.model,x.sku].filter(Boolean).join(' / ') || '—'],
    ['결과',x=>stateNames[x.status] || x.status],['지문 시도','host_key_attempts'],['백업 시도','attempts'],['오류 코드','error_code'],
    ['상세','message',true],['진단 코드',x=>x.diagnostic_code ? button(x.diagnostic_code,async()=>{
      try { await navigator.clipboard.writeText(x.diagnostic_code); notify('진단 코드를 복사했습니다.'); }
      catch { notify('진단 코드 ' + x.diagnostic_code); }
    }) : '—']
  ],rows));
  window.backupTools?.showSummary(area, rows);
  if (r.output_directory) {
    const path = elem('p',undefined,'hint');
    if (body?.output_directory) path.append(elem('b','요청한 추가 저장 위치 '),elem('code',body.output_directory),elem('br'));
    path.append(elem('b',body && !body.output_directory ? '기본 저장 폴더 ' : '실제 저장 결과 폴더 '),elem('code',r.output_directory));
    area.prepend(path);
  }
  if (r.files?.length) area.append(openFolder(r.files[0]));
  for (const warning of r.warnings || []) area.append(elem('p',warning,'warning'));
  const failed = new Set(rows.filter(row=>row.status === 'retry_exhausted').map(row=>row.endpoint));
  const retry = body?.ips?.split('\n').filter(ip=>failed.has(ip+':22')) || [];
  if (retry.length) {
    const again = button('접속 실패 장비만 다시 시도 (' + retry.length + ')',()=>
      backup({...body, ips:retry.join('\n')}));
    again.dataset.job = ''; area.append(again);
  }
}
form('backupForm', () => {
  const ips = window.backupTools.readIPs();
  if (!ips) { $('backupTargets').focus(); return; }
  backup({ips, concurrency:Number($('concurrency').value), max_attempts:Number($('attempts').value),
    filename_mode:$('fileMode').value, output_directory:$('backupDirectory').value.trim()});
});

const endpoint = row => Number(row.ssh_port || 22) === 22 ? row.ip : row.ip + ':' + row.ssh_port;
const controlPortSupported = port => typeof port==='string' && port.length<=12 && /^[1-9]\d*(?:\/[1-9]\d*)?$/.test(port);
const portKey = row => endpoint(row) + '|' + row.interface;
const targetFromRow = row => ({ip:row.ip,sshPort:Number(row.ssh_port || 22),port:String(row.interface)});
const controlPortKey = target => portKey({ip:target.ip,ssh_port:target.sshPort,interface:target.port});
function expireLiveMatrix(preserveView=false) {
  ui.liveId=null; ui.tileRow=null; clearInspection();
  monitor()?.invalidateLive(preserveView);
  updatePortActions();
}
function clearPorts(preserveView=false) {
  window.controlTools?.clearQuickResult();
  ui.rows = []; ui.selected.clear();
  showDiscoveryErrors([]);
  $('controlResult').hidden = true;
  expireLiveMatrix(preserveView);
  $('controlPanel').open=false;
  updateQuerySummary();
  if (!preserveView) setQueryWorkspace(false);
  renderPorts();
}
function powerBadge(view) {
  const badge = elem('span',view.label,'power-badge');
  badge.dataset.state = view.power.state;
  badge.title = view.reason;
  return badge;
}
function observedInspectionTarget() {
  const viewer=monitor();
  if (ui.selected.size || !ui.liveId || viewer?.source().liveId!==ui.liveId) return null;
  if (currentInspection()) return ui.inspectedRow;
  const row=$('controlMode').value==='batch' ? viewer.inspectionTarget() : null;
  return row && controlPortSupported(row.port) ? row : null;
}
function updateHiddenSelectedCount(targets=[]) {
  // The result list can offer its focused port as the only control target.
  const selected=ui.selected.size ? ui.selected : targets.map(controlPortKey);
  const count=$('controlMode').value==='batch' && (ui.selected.size || targets.length)
    ? window.portTools.hiddenSelectedCount(selected) : 0;
  const label=$('hiddenSelectedCount');
  label.hidden=!count;
  label.textContent='화면 밖 '+count+'개';
}
function updateControlTarget() {
  updateMatrixSelectAll();
  const target=$('controlTarget');
  const batch=$('controlMode').value==='batch';
  const targets=batch ? matrixControlTargets() : [];
  const observed=batch && observedInspectionTarget();
  $('selectEligible').hidden=$('operation').value==='disable';
  if (currentInspection()) {
    target.textContent='실시간으로 다시 조회한 대상 · '+$('singleIP').value+' / 포트 '+$('singlePort').value;
  } else if (targets.length) {
    const keys=targets.map(controlPortKey);
    const ports=keys.slice(0,3).map(key=>{
      const [address,port]=key.split('|');
      return address+' / 포트 '+port;
    });
    if (keys.length===1) {
      const row=ui.rows.find(item=>portKey(item)===keys[0]);
      if (row && !row.refreshInvalidated && !row.controlUsed) {
        const power=PowerMatrix.classify(row);
        ports.push(wattsText(power.watts));
        if (power.apName) ports.push(power.apName);
      }
    }
    target.textContent=ports.join(' · ')+(keys.length>3?' 외 '+(keys.length-3)+'개':'');
  } else if (observed) {
    target.textContent=observed.ip+(observed.sshPort===22 ? '' : ':'+observed.sshPort)+' / 포트 '+observed.port;
  } else target.textContent='';
  target.hidden=!target.textContent;
  const selectedCount=ui.selected.size || targets.length;
  $('selectionSummary').hidden=!batch || !selectedCount;
  $('selectedCount').textContent='선택 '+selectedCount+'개';
  updateHiddenSelectedCount(targets);
  $('quickActions').hidden=!targets.length || ($('operation').value==='disable' && !matrixControlTargets('poe_off').length);
  const refresh=$('refreshPorts');
  const refreshCount=!currentInspection() && (ui.liveId || displayingInventory())
    ? window.portTools?.selectedRefreshCount() || 0 : 0;
  refresh.hidden=!refreshCount;
  refresh.textContent='선택 '+refreshCount+'포트 재조회';
  const inspect=$('inspectSingle');
  const strip=$('portActionStrip');
  const container=observed ? strip : $('singleControls');
  if (inspect.parentElement!==container) container.insertBefore(inspect,observed ? refresh : null);
  inspect.hidden=batch && !observed;
  if (observed) inspect.title=observed.ip+':'+observed.sshPort+' / 포트 '+observed.port;
  else inspect.removeAttribute('title');
  strip.hidden=!observed && target.hidden && $('selectionSummary').hidden && $('quickActions').hidden && refresh.hidden;
}
function syncMonitorChecks() {
  const viewer=monitor();
  if (!ui.liveId || viewer?.source().liveId!==ui.liveId) return;
  const available=window.portTools.availableControlTargets();
  const selected=available.filter(target=>ui.selected.has(controlPortKey(target)));
  const stale=ui.rows.filter(row=>!freshControlRow(row)).map(row=>({...targetFromRow(row),link:row.link_status}));
  viewer.setControlSelection(ui.liveId,selected,stale,available);
}
function renderPorts() {
  syncMonitorChecks();
  updatePortActions();
  window.portTools?.renderMatrix();
}
function showDiscoveryErrors(errors) {
  if (!Array.isArray(errors)) return;
  const list=$('discoveryErrors');
  list.replaceChildren(...errors.map(error=>elem('li',endpoint(error)+' · 포트 목록 조회 실패 · '+error.message)));
  list.hidden=!errors.length;
}
function receivePorts(result,keepView=false,refreshedKeys,periodic=false) {
  if (!Array.isArray(result.rows)) return;
  const selectedKey=periodic && ui.tileRow?.key;
  ui.tileRow=null;
  ui.rows = window.portTools.reconcileRows(result.rows,refreshedKeys);
  showDiscoveryErrors(result.discoveryErrors);
  updateQuerySummary();
  const available = new Set(window.portTools.availableControlTargets($('operation').value,true).map(controlPortKey));
  ui.selected = new Set([...ui.selected].filter(key=>available.has(key)));
  if (ui.rows.length && !ui.rows.some(row=>row.status==='refresh_pending')) {
    ui.liveId=++observationSequence;
    postMatrix(keepView,periodic);
    const selected=monitor()?.selected();
    if (selectedKey && selected?.key===selectedKey) ui.tileRow=selected;
  } else expireLiveMatrix(keepView);
  renderPorts();
}
function queryPorts(targets) {
  return run('ports.query',{targets},result=>{
    window.portTools.acceptQuery(result.groups);
    receivePorts(result,true);
    monitor()?.setPresentation('matrix');
    setQueryWorkspace(ui.rows.some(portObserved));
  });
}
let credentialRequest=null;
function withSshAccount(onConnected) {
  if (queryCredentials.open || ui.busy || !ui.token) return;
  if (ui.session.active) return onConnected();
  credentialRequest=onConnected;
  $('queryLoginForm').reset();
  $('queryLoginError').hidden=true;
  queryCredentials.showModal();
}
function runSsh(action, body, receive) {
  return withSshAccount(()=>run(action,body,receive));
}
form('queryForm', () => {
  if (ui.busy || !ui.token || queryCredentials.open) return;
  const input = $('queryTargets'), targets = input.value;
  if (!targets.trim()) {
    input.value = '';
    input.reportValidity();
    return;
  }
  withSshAccount(()=>queryPorts(targets));
});
$('cancelQueryCredentials').onclick = () => queryCredentials.close();
queryCredentials.addEventListener('close', () => {
  if (queryCredentials.open) return;
  credentialRequest=null; $('queryLoginForm').reset();
});
form('queryLoginForm', async () => {
  if (!queryCredentials.open || ui.busy || !ui.token) return;
  const onConnected=credentialRequest;
  const submit = $('submitQueryCredentials'), error = $('queryLoginError');
  error.hidden = true;
  submit.textContent = '계정 적용 중…';
  const applied = await applySshAccount($('queryUsername'), $('queryPassword'), $('queryEnableSecret'));
  submit.textContent = '계정 적용 후 계속';
  if (!queryCredentials.open || credentialRequest!==onConnected) return;
  if (!applied) {
    error.textContent = $('statusText').textContent;
    error.hidden = false;
    $('queryPassword').focus();
    return;
  }
  queryCredentials.close();
  onConnected?.();
});
function retryFailedPorts(key=null) {
  const liveId=ui.liveId;
  withSshAccount(()=>{
    if (ui.liveId!==liveId || $('portMonitor').closest('[data-page]').hidden) return;
    if (key && window.portTools.currentKey()!==key) return;
    const failed=ui.rows.filter(row=>(!key || portKey(row)===key) && portReadFailed(row));
    if (!failed.length || (!key && !ui.rows.some(portObserved))) return;
    const byEndpoint=new Map();
    for (const row of failed) {
      const address=endpoint(row);
      if (!byEndpoint.has(address)) byEndpoint.set(address,new Set());
      byEndpoint.get(address).add(row.interface);
    }
    const targets=[...byEndpoint].map(([address,ports])=>address+' '+[...ports].join(',')).join('\n');
    const retryKeys=new Set(failed.map(portKey));
    run('ports.refresh',{targets},result=>receivePorts(result,true,retryKeys,true),retryKeys);
  });
}
$('retryFailedPorts').onclick=()=>retryFailedPorts();
function updateMatrixSelectAll() {
  const toggle=$('matrixSelectAll'), scope=$('selectionScope');
  const keys=ui.busy ? [] : (window.portTools?.scopeKeys() || []);
  const checked=keys.length>0 && keys.every(key=>ui.selected.has(key));
  const action=checked ? '전체 선택 해제' : '전체 선택';
  const scopeName=scope.selectedOptions[0].textContent;
  const description=scopeName+' 범위의 선택 가능한 포트';
  $('matrixSelectionScope').hidden=scope.value==='filtered';
  $('matrixSelectionScope').textContent=scopeName;
  toggle.disabled=ui.busy || !keys.length;
  toggle.textContent=action;
  toggle.setAttribute('aria-pressed',String(checked));
  toggle.setAttribute('aria-description',description);
  toggle.title=description+' '+action;
}
$('selectionScope').onchange=updateMatrixSelectAll;
$('matrixSelectAll').onclick=()=>{
  const keys=window.portTools.scopeKeys();
  if (ui.busy || !keys.length) return;
  const remove=keys.every(key=>ui.selected.has(key));
  clearInspection();
  ui.tileRow=null;
  for (const key of keys) {
    if (remove) ui.selected.delete(key); else ui.selected.add(key);
  }
  $('controlMode').value='batch';
  $('controlMode').onchange();
  renderPorts();
};
$('selectEligible').onclick=()=>{
  const keys=window.portTools.scopeKeys();
  if (ui.busy || !keys.length) return;
  ui.tileRow=null;
  const allowed=$('operation').value!=='disable' && new Set(ui.rows
    .filter(row=>!row.controlUsed && (row.eligible_operations || []).includes($('operation').value)).map(portKey));
  ui.selected=new Set(allowed ? keys.filter(key=>allowed.has(key)) : keys);
  renderPorts();
};
$('operation').onchange=()=>{
  const available=new Set(window.portTools.availableControlTargets().map(controlPortKey));
  for (const key of ui.selected) if (!available.has(key)) ui.selected.delete(key);
  renderPorts();
};
$('controlMode').onchange = () => {
  const single = $('controlMode').value === 'single';
  $('batchControls').hidden = single; $('singleControls').hidden = !single;
  if (single) {
    const detail = ui.rows.find(row=>portKey(row) === window.portTools?.currentKey());
    if (detail) { $('singleIP').value = endpoint(detail); $('singlePort').value = detail.interface; }
  }
  $('controlHint').textContent = single ? '직접 입력한 포트의 현재 상태와 전송할 명령을 먼저 검토합니다.' : '포트 비활성화는 링크 상태, LLDP, 전력값과 관계없이 선택한 등록 포트에 적용합니다.';
  updateControlTarget();
};
function controlAfterText(row) {
  if (row.after) return [row.after.detail,row.after.poeStatus].filter(Boolean).join(' · ');
  if (['paused','cancelled','preflight_failed'].includes(row.status)) return '변경 명령 미전송';
  if (['unverified','result_unknown'].includes(row.status)) return '명령 적용 미확인';
  return '재조회 필요';
}
function markControlRowsUsed(targets) {
  const used = new Set(targets.map(controlPortKey));
  if (ui.tileRow && used.has(controlPortKey(ui.tileRow))) ui.tileRow=null;
  for (const key of used) ui.selected.delete(key);
  for (const row of ui.rows) if (used.has(portKey(row))) {
    row.controlUsed=true;
    row.powerObserved=false;
    row.link_status='';
    row.link_observed_at='';
  }
  renderPorts();
}
function receiveControlObservations(response) {
  const results=response.results || [], readings=response.powerRefresh?.readings;
  const affected=['poe_on','poe_off'].includes(response.operation)
    ? results.filter(row=>['verified','unchanged','unverified','result_unknown','preflight_failed'].includes(row.status))
    : results;
  if (Array.isArray(readings) && Array.isArray(response.rows)) {
    for (const row of affected) ui.selected.delete(controlPortKey(row));
    receivePorts(response,true,new Set(readings.map(controlPortKey)));
  } else {
    if (Array.isArray(response.rows)) receivePorts(response,true);
    if (affected.length) markControlRowsUsed(affected);
  }
}
function isMeasuredReading(reading) {
  return reading?.status==='measured' && Number.isFinite(reading.watts) && reading.watts>=0;
}
function hasUncertainControl(result) {
  return result.results?.some(row=>row.status==='unverified' || row.status==='result_unknown');
}
function wattsText(watts) {
  return Number.isFinite(watts) && watts>=0 ? watts+' W' : '미측정';
}
function singlePowerChange(result) {
  const readings=result.powerRefresh.readings;
  if (readings.length!==1) return '';
  const reading=readings[0];
  const target=result.results?.find(row=>controlPortKey(row)===controlPortKey(reading));
  if (!target) return '';
  const address=target.sshPort && target.sshPort!==22 ? target.ip+':'+target.sshPort : target.ip;
  return ' · '+address+' / '+target.port+' · '+wattsText(target.before?.watts)+' → '+wattsText(isMeasuredReading(reading) ? reading.watts : null);
}
const powerDeltaNumber=new Intl.NumberFormat('ko-KR',{useGrouping:false,maximumSignificantDigits:10});
function powerCell(watts, observed, note, beforeWatts) {
  const cell=elem('div',undefined,'control-power');
  const measured=Number.isFinite(watts) && watts>=0;
  cell.append(elem('strong',wattsText(watts)));
  if (measured && Number.isFinite(beforeWatts) && beforeWatts>=0) {
    const delta=watts-beforeWatts, sign=delta>0 ? '+' : delta<0 ? '−' : '';
    cell.append(elem('small','변화 '+sign+powerDeltaNumber.format(Math.abs(delta))+' W','power-delta'));
  }
  if (observed) cell.append(elem('small',dateText(observed)));
  if (note) cell.append(elem('small',note));
  return cell;
}
function showControlResults(response, title) {
  const results = response.results || [];
  const box = $('controlResult');
  box.hidden = false;
  const columns=[
    ['스위치',x=>x.sshPort && x.sshPort !== 22 ? x.ip + ':' + x.sshPort : x.ip],
    ['포트','port'],['결과',x=>stateNames[x.status] || x.status]
  ];
  if (Array.isArray(response.powerRefresh?.readings)) {
    const fresh = new Map(response.powerRefresh.readings.map(reading=>[controlPortKey(reading),reading]));
    columns.push(['변경 전 실측 전력',row=>powerCell(row.before?.watts,row.before?.observed)],
      ['자동 재조회 전력',row=>{
        const reading=fresh.get(controlPortKey(row));
        if (!reading) return '재조회 대상 아님';
        if (isMeasuredReading(reading)) return powerCell(reading.watts,reading.observed,'',row.before?.watts);
        return powerCell(null,reading.observed,reading.message || (reading.status==='cancelled' ? '재조회 취소' : '전력값을 확인할 수 없습니다.'));
      },true],['명령 검증 시점 상태',controlAfterText,true]);
  } else columns.push(['변경 후 상태',controlAfterText,true]);
  columns.push(['상세','message',true]);
  box.append(elem('p',response.message || title + ' 실행 결과'),simpleTable(columns,results));
  if (response.powerRefresh?.message) box.append(elem('p',response.powerRefresh.message,'warning'));
}
function renderPlan(plan, inspected=ui.inspectedRow) {
  const choice=controlChoice();
  const sameChoice=()=>controlContextMatches(inspected) && choice===controlChoice();
  const title = operationNames[plan.operation] || plan.operation;
  const targets = simpleTable([['스위치',x=>x.hostname || x.ip],['IP',x=>x.sshPort && x.sshPort !== 22 ? x.ip + ':' + x.sshPort : x.ip],['포트','port'],['현재 상태','detail',true],['전송할 명령','command',true]],plan.targets || []);
  const phrase = elem('code',plan.confirmationText), input = elem('input'), label = elem('label','표시된 확인 문구를 그대로 입력하세요.');
  input.autocomplete = 'off'; input.spellcheck = false; label.append(input);
  const execute = button('승인하고 명령 실행',() => {
    if (!plan.confirmationText || input.value !== plan.confirmationText) return;
    dialog.close();
    if (!sameChoice()) {
      notify('제어 대상 또는 작업이 변경되었습니다. 현재 선택으로 다시 검토하세요.',true);
      return;
    }
    run('control.apply',{planId:plan.planId,confirmation:input.value},r => {
      const continuePlan=sameChoice();
      window.controlTools.finishProgress();
      showControlResults(r,title);
      receiveControlObservations(r);
      if (r.nextPlan && continuePlan) renderPlan(r.nextPlan,inspected);
    });
  },'danger');
  execute.disabled = true;
  input.oninput = () => { execute.disabled = !plan.confirmationText || input.value !== plan.confirmationText; };
  const children = [elem('b',title + ' · ' + (plan.targets || []).length + '개 포트'),targets];
  if (plan.later?.length) {
    const scope = elem('details');
    scope.append(elem('summary','다음 묶음의 대상 ' + plan.later.length + '개 보기'),
      simpleTable([['스위치','hostname'],['IP','ip'],['SSH 포트','sshPort'],['포트','port'],['예정 명령','command']],plan.later));
    children.unshift(elem('p','전체 계획 ' + (plan.targets.length + plan.later.length) + '개 · 이번 묶음 ' + plan.targets.length + '개'),scope);
  }
  for (const warning of plan.warnings || []) children.push(elem('p',warning,'warning'));
  if (plan.expiresIn) children.push(elem('p','이 승인 요청의 유효 시간은 ' + plan.expiresIn + '초입니다.','hint'));
  openDialog('포트 제어 승인',[...children,phrase,label,execute]);
  input.focus();
}
$('preview').onclick = () => {
  const inspected=ui.inspectedRow;
  if (!controlContextMatches(inspected)) { updatePortActions(); return; }
  const choice=controlChoice();
  const mode = $('controlMode').value;
  let targets;
  if (mode === 'single') {
    const ip = $('singleIP').value.trim(), port = $('singlePort').value.trim();
    if (!ip || !port) { notify('단일 제어할 스위치 주소와 포트를 입력하세요.',true); return; }
    targets = [{ip,port}];
  } else {
    targets = matrixControlTargets();
    if (!targets.length) { notify('제어할 포트를 선택하세요.',true); return; }
  }
  window.controlTools.clearQuickResult();
  $('controlResult').replaceChildren(); $('controlResult').hidden = true;
  runSsh('control.preview',{operation:$('operation').value,scope:mode,targets},plan => {
    if (!plan.planId) return;
    if (controlContextMatches(inspected) && choice===controlChoice()) renderPlan(plan,inspected);
    else showChangedControlChoice();
  });
};
function dateText(value) {
  if (!value) return '—';
  const stamp = new Date(value);
  return isNaN(stamp.getTime()) ? value : stamp.toLocaleString('ko-KR',{hour12:false});
}
let fileDestination = null, fileRequest = 0, fileDraft = null;
for (const node of all('[data-import]')) node.onclick = () => {
  fileRequest++;
  fileDestination = node.dataset.import;
  fileDraft = fileDestination === 'queryTargets' ? window.portTools.targetDraftVersion() : null;
  $('targetFile').accept = fileDestination === 'queryTargets' ? '.txt,.tsv,.json' : '.txt,.tsv';
  $('targetFile').value = '';
  $('targetFile').click();
};
$('targetFile').onchange = async () => {
  const file = $('targetFile').files[0], dest = fileDestination, request = fileRequest, draft = fileDraft;
  if (!file || !dest) return;
  const isQuery = dest === 'queryTargets';
  const current = () => request === fileRequest && (!isQuery || (draft !== null && window.portTools.targetDraftVersion() === draft));
  function fileError(message) {
    if (!current()) return;
    if (isQuery) $('targetMessage').textContent = message;
    notify(message,true);
  }
  if (!current()) return;
  const allowed=$('targetFile').accept.split(',');
  if (!allowed.some(ext=>file.name.toLowerCase().endsWith(ext))) {
    fileError('대상 목록 파일 형식을 확인하세요. 허용 형식: '+allowed.join(', '));
    return;
  }
  if (file.size > 2 * 1024 * 1024) { fileError('2 MB 이하인 대상 목록 파일을 선택하세요.'); return; }
  try {
    let text = (await file.text()).replace(/^\uFEFF/,'');
    if (!current() || (isQuery && ui.busy)) return;
    if (isQuery && file.name.toLowerCase().endsWith('.json')) {
      let profile;
      await run('profiles.import',{text},r=>{ profile = r; });
      if (!current()) return;
      if (!profile) { $('targetMessage').textContent = $('statusText').textContent; return; }
      text = profile.targets;
    }
    const input = $(isQuery ? 'editorTargets' : dest);
    input.value = text;
    input.dispatchEvent(new Event('input'));
    if (dest === 'backupTargets') window.backupTools.readIPs();
    input.focus();
  } catch (e) { fileError(e.message); }
};
document.querySelector('[data-save="backupTargets"]').onclick = () => {
  const value = window.backupTools.readIPs();
  if (!value) { $('backupTargets').focus(); return; }
  const a = document.createElement('a');
  const url = URL.createObjectURL(new Blob(['\uFEFF' + value + '\n'],{type:'text/plain;charset=utf-8'}));
  a.href = url; a.download = 'switch-targets.txt'; a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
};
function showProfileFiles(r) {
  $('profileFiles').hidden = false; $('profileFiles').replaceChildren(filelinks(r.files));
}
$('exportProfile').onclick = async () => {
  const draft = window.portTools.targetDraftVersion();
  if (draft === null) return;
  let saved = false;
  await run('profiles.export',{targets:$('editorTargets').value.trim()},r=>{
    if (window.portTools.targetDraftVersion() !== draft) return;
    saved = true;
    showProfileFiles(r);
    $('profileFiles').querySelector('a')?.focus();
  });
  if (!saved && window.portTools.targetDraftVersion() === draft) $('targetMessage').textContent = $('statusText').textContent;
};
$('shutdown').onclick = () => confirmDialog('로컬 웹 서버 종료',[elem('p','현재 SSH 세션을 닫고 이 PC의 로컬 웹 서버를 종료합니다.')],'서버 종료',async () => {
  try {
    await api('/api/shutdown',{});
    window.dispatchEvent(new Event('aruba-server-stopped'));
    ui.token = ''; ui.session = {active:false}; clearPorts(); sync();
    notify('로컬 웹 서버를 종료했습니다. 다시 사용하려면 Start-Local-Web.cmd를 여세요.');
  } catch (e) { notify(e.message,true); }
});
async function start() {
  sync();
  try {
    const bootstrap = await api('/api/bootstrap');
    ui.token = bootstrap.token;
    import('/static/browser-presence.js').then(module=>module.watchBrowserTab(bootstrap)).catch(()=>{});
    ui.session = bootstrap.session || {active:false};
    ui.bootMonitor=bootstrap.monitor;
    window.powerMonitoring?.connect(ui.bootMonitor);
    $('version').textContent = 'v' + String(bootstrap.version || '').replace(/^v/,'');
    let resumed;
    const resumeId=window.cableTools?.jobOnOpen(bootstrap) || bootstrap.busyJob;
    if (resumeId && resumeId!==bootstrap.monitor?.jobId) {
      ui.busy = true; ui.jobId = resumeId; sync();
      setJobProgress({});
      notify('진행 중인 작업을 기다리고 있습니다.');
      try {
        resumed=await poll(resumeId,true);
        if (resumed.action === 'cable.test') notifyCableResult(resumed.result, resumed.cancelled);
        else notify((jobNames[resumed.action] || '작업') + (resumed.cancelled ? ' 취소됨' : ' 완료'));
      } catch (error) {
        notify(error.message,true);
      } finally {
        window.portTools?.endResumedQuery();
        setJobProgress(null);
        ui.busy = false; ui.jobId = null;
        const latest = await api('/api/bootstrap'); ui.session = latest.session || {active:false};
      }
    }
    const workspace = await api('/api/workspace');
    if (workspace.groups?.length) {
      window.portTools.restoreInventory(workspace.groups,workspace.targetText,true);
      if (workspace.query) {
        receivePorts(workspace.query);
        setQueryWorkspace(ui.rows.some(portObserved));
      } else setQueryWorkspace(false);
    }
    if (resumed?.action === 'backup.run' && Array.isArray(resumed.result.rows)) {
      showBackupResult(resumed.result);
    } else if (resumed?.action === 'profiles.export' && resumed.result.files?.length) {
      setQueryExpanded(true);
      window.portTools.openTargetEditor();
      showProfileFiles(resumed.result);
      $('profileFiles').querySelector('a')?.focus();
    } else if (resumed?.action === 'control.quick-execute') {
      window.controlTools.showQuickResult(resumed.result,resumed.cancelled,true);
    } else if (resumed?.action === 'cable.test') {
      window.cableTools?.sync(true);
      window.cableTools?.showResult(resumed.result,resumed.cancelled,true);
    } else if (resumed?.action === 'control.apply' && resumed.result.results?.length) {
      window.controlTools.finishProgress();
      showControlResults(resumed.result,operationNames[resumed.result.operation] || '포트 제어');
    }
    if (resumed && (hasUncertainControl(resumed.result) || resumed.result.blocked?.length))
      notify(resumed.result.message || '포트 제어 결과를 확인하세요.',true);
    route();
    ui.appReady=true;
    window.powerMonitoring?.ready();
  } catch (error) { notify(error.message,true); }
  finally { sync(); }
}
// The deferred feature scripts register their startup handlers before DOMContentLoaded.
if (document.readyState==='complete') start();
else window.addEventListener('DOMContentLoaded',start,{once:true});
