'use strict';

const $ = id => document.getElementById(id);
const all = (selector, parent = document) => [...parent.querySelectorAll(selector)];
const operationNames = {poe_on: 'PoE 전원 ON', poe_off: 'PoE 전원 OFF', enable: '포트 Up', disable: '포트 Down'};
const jobNames = {
  'session.open': '접속 계정 적용', 'session.close': '세션 해제', 'hostkey.inspect': '호스트 키 조회',
  'hostkey.approve': '호스트 키 승인', 'hostkey.list':'등록된 키 조회', 'hostkey.forget':'등록된 키 삭제', 'ports.query': '포트 조회', 'ports.refresh':'선택 범위 재조회', 'ports.export': '조회 CSV 저장',
  'profiles.export': '대상 프로필 저장', 'profiles.import': '대상 프로필 열기', 'logs.query': '스위치 로그 수집',
  'backup.run': '구성 백업', 'backup.history':'백업 보고서 조회', 'control.preview': '포트 제어 검토', 'control.apply': '포트 명령 실행', 'control.history': '포트별 기록 조회'
};
const states = {queued: '대기', running: '진행 중', done: '완료', failed: '실패', cancelled: '취소', interrupted:'서버 종료로 중단'};
const labels = {
  ip: 'IP 주소', ssh_port: 'SSH 포트', label: '장비 구분', hostname: '호스트 이름', model: '스위치 모델',
  interface: '포트', status: '조회 결과', enabled: '관리 상태', link_status: '링크', poe_enabled: 'PoE 설정',
  poe_status: 'PoE 상태', watts: '공급 전력 (W)', observed_at: '관측 시각', finished_at: '조회 종료',
  error_code: '오류 코드', message: '메시지', poe_finished_at: 'PoE 조회 종료', complete: '조회 완결',
  lldp_status: 'LLDP 조회', lldp_observed_at: 'LLDP 관측 시각', lldp_error_code: 'LLDP 오류 코드',
  lldp_message: 'LLDP 메시지', ap_names: 'AP 이름', lldp_neighbors: 'LLDP 이웃', power_evidence: '전력 근거',
  assessment_label: '전력 판정', assessment_reason: '판정 근거', assessment_model: 'AP 모델',
  assessment_model_source: '모델 근거', requested_watts: '요청 전력 (W)', allocated_watts: '할당 전력 (W)',
  negotiation: '전력 협상', port_poe_state: '포트 전력 상태', port_poe_explanation: '전력 상세'
};
const stateNames = {ok:'성공',success:'성공',failed:'실패',cancelled:'취소',unsupported:'미지원',unknown:'미등록',changed:'키 변경',trusted:'등록됨',rejected:'거부',retry_exhausted:'접속 재시도 소진',verified:'검증 완료',unchanged:'이미 요청 상태',preflight_failed:'사전 검증 실패',unverified:'검증 미완료',paused:'일시 중지',skipped:'건너뜀',blocked:'실행 차단',result_unknown:'결과 미기록'};
const ui = { token:'', session:{active:false}, busy:false, jobId:null, rows:[], selected:new Set(), detail:null, logs:[], chosenLog:null, logLoaded:false, csvLoaded:false, matrixFile:null, pendingMatrix:null, portView:'monitor', inspectedRow:null };
const dialog = $('review');
const queryCredentials = $('queryCredentials');
Object.assign(jobNames, {'targets.preview':'대상 목록 검토','targets.apply':'대상 목록 적용','control.inspect':'단일 포트 상태 조회','control.quick':'빠른 제어 검토','control.import-history':'작업 기록 열기','backup.pick-directory':'저장 폴더 선택','history.open-folder':'이력 위치 열기'});

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
function notify(message, error = false) {
  $('statusBar').hidden = !message;
  $('statusBar').dataset.level = error ? 'error' : '';
  $('statusText').textContent = message;
  $('dismissStatus').hidden = ui.busy;
}
function sync() {
  const active = ui.session.active;
  $('sessionChip').classList.toggle('active', active);
  $('sessionChip').textContent = active ? 'SSH · ' + ui.session.username : 'SSH 계정 설정';
  $('connectionState').textContent = active ? ui.session.username + ' 계정 적용 중' : '계정 미설정';
  for (const node of all('[data-live]')) {
    node.disabled = ui.busy || !ui.token || !active;
    node.title = !active ? 'SSH 접속 설정에서 계정을 먼저 적용하세요.' : '';
  }
  for (const node of all('[data-job]')) node.disabled = ui.busy || !ui.token;
  $('stopJob').hidden = !ui.jobId;
  $('dismissStatus').hidden = ui.busy;
  $('shutdown').disabled = ui.busy || !ui.token;
}
async function api(path, data) {
  const options = {credentials:'same-origin', cache:'no-store'};
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
  if (!response.ok) throw new Error(json.error || '로컬 요청 실패 (' + response.status + ')');
  return json;
}
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function poll(id) {
  let failures = 0;
  let cursor = 0;
  for (;;) {
    let job;
    try { job = await api('/api/jobs/' + encodeURIComponent(id) + '?after=' + cursor); failures = 0; }
    catch (e) {
      if (++failures >= 3) throw new Error(e.message + ' 작업 이력에서 실행 결과를 확인하세요.');
      await delay(850);
      continue;
    }
    renderHostReview(id,job.progress?.hostKeys || []);
    if (job.state === 'done') return {result:job.result || {}, cancelled:false};
    if (job.state === 'cancelled') return {result:job.result || {}, cancelled:true};
    if (job.state === 'failed') throw new Error(job.error || '작업이 실패했습니다.');
    if (!['running','queued'].includes(job.state)) throw new Error('작업 상태를 읽을 수 없습니다.');
    window.portTools?.onProgress?.(job.action,job.progress || {});
    window.backupTools?.onProgress?.(job.action,job.progress || {});
    window.controlTools?.onProgress?.(job.action,job.progress || {});
    if (Number.isInteger(job.progress?.cursor)) cursor = job.progress.cursor;
    if (!$('homePorts').closest('section').hidden) displayHomeProgress(job.action,job.progress || {});
    if (job.progress?.message && !$('stopJob').disabled) {
      const {message,completed,total} = job.progress;
      notify((jobNames[job.action] || '작업') + ' · ' + message + (Number(total) > 0 ? ' (' + completed + ' / ' + total + ')' : ''));
    }
    await delay(450);
  }
}
async function run(action, body, receive) {
  if (ui.busy || !ui.token) return;
  ui.busy = true;
  $('stopJob').disabled = false;
  sync();
  notify(jobNames[action] + ' 진행 중…');
  try {
    if (['backup.run','ports.query','ports.refresh','control.inspect','control.preview','control.quick','logs.query'].includes(action)) body.interactive_hostkeys = true;
    window.portTools?.onJob?.(action,'running',body);
    const submitted = await api('/api/jobs', {action,body});
    body = null;
    ui.jobId = submitted.id;
    sync();
    const {result,cancelled} = await poll(ui.jobId);
    if (receive) receive(result, cancelled);
    notify(jobNames[action] + (cancelled ? ' 취소됨. 완료된 항목은 결과에 표시됩니다.' : ' 완료'));
  } catch (error) {
    notify(error.message, true);
  } finally {
    renderHostReview('',[]);
    ui.jobId = null;
    ui.busy = false;
    sync();
    window.portTools?.onJob?.(action,'idle');
    if (!document.querySelector('[data-page=history]').hidden) loadHistory();
    if (!$('homePorts').closest('section').hidden) loadHome();
  }
}
$('stopJob').onclick = async () => {
  if (!ui.jobId) return;
  $('stopJob').disabled = true;
  try {
    await api('/api/jobs/' + encodeURIComponent(ui.jobId) + '/cancel', {});
    if (ui.busy) notify('취소를 요청했습니다. 스위치에서 진행 중인 단계가 끝날 때까지 기다려 주세요.');
  } catch (e) { if (ui.busy) { notify(e.message, true); $('stopJob').disabled = false; } }
};
$('dismissStatus').onclick = () => notify('');

function theme(value) {
  document.documentElement.dataset.theme = value;
  for (const node of all('[data-theme-choice]')) node.setAttribute('aria-pressed', String(value === node.dataset.themeChoice));
  for (const frame of all('iframe')) {
    frame.contentWindow?.postMessage({type:'aruba-theme',theme:value}, location.origin);
  }
}
for (const node of all('[data-theme-choice]')) node.onclick = () => {
  const value = node.dataset.themeChoice;
  theme(value);
  try { localStorage.setItem('aruba2930f.portMatrix.theme',value); } catch {}
};
let savedTheme;
try { savedTheme = localStorage.getItem('aruba2930f.portMatrix.theme'); } catch {}
theme(savedTheme === 'light' ? 'light' : 'dark');
function postLog(log) {
  if (log && ui.logLoaded) $('logViewer').contentWindow.postMessage({type:'aruba-switch-log', source:log.hostname ? log.hostname + ' (' + log.endpoint + ')' : log.endpoint, text:log.text}, location.origin);
}
function monitor() {
  return $('csvViewer').contentWindow?.PortMonitor;
}
function historicalSource() {
  const source=monitor()?.source();
  return source?.hasData && (!source.liveId || source.liveId!==ui.matrixFile);
}
function updatePortActions() {
  const historical=historicalSource();
  const inspected=historical && !!ui.inspectedRow;
  $('portControlTab').disabled=!!historical && !inspected;
  $('portControlTab').title=$('portControlTab').disabled?'관제에서 포트를 선택한 뒤 실시간으로 조회하세요.':'';
  $('controlRows').hidden=!!inspected;
  $('liveScope').hidden=ui.portView==='monitor' || (ui.portView==='control' && !!inspected);
  $('controlMode').disabled=!!inspected;
  $('singleIP').readOnly=$('singlePort').readOnly=!!inspected;
  $('openPortCsv').disabled=!ui.csvLoaded;
}
function setPortView(view) {
  if (view==='control' && historicalSource() && !ui.inspectedRow) return;
  ui.portView=view;
  for (const [name,tab,panel] of [['monitor','portMonitorTab','portMonitor'],['live','portLiveTab','portTargets'],['control','portControlTab','portLiveControl']]) {
    $(panel).hidden=name!==view;
    $(tab).setAttribute('aria-pressed',String(name===view));
  }
  updatePortActions();
}
$('portMonitorTab').onclick=()=>setPortView('monitor');
$('portLiveTab').onclick=()=>setPortView('live');
$('portControlTab').onclick=()=>setPortView('control');
$('openPortCsv').onclick=()=>{
  setPortView('monitor');
  $('csvViewer').contentDocument?.getElementById('openCsv')?.click();
};
function selectObservedPort(message) {
  if (ui.portView!=='monitor' || ui.busy) return;
  const row=monitor()?.selected();
  if (!row || row.ip!==message.ip || row.port!==message.port || row.sshPort!==message.sshPort) return;
  if (!ui.session.active) { notify('SSH 접속 설정에서 계정을 적용한 뒤 이 포트를 다시 선택하세요.',true); return; }
  const source=monitor().source();
  const record=source.liveId && source.liveId===ui.matrixFile && ui.rows.find(
    item=>item.ip===row.ip && Number(item.ssh_port || 22)===row.sshPort && String(item.interface)===row.port
  );
  if (record && record.status==='ok' && !record.controlUsed) {
    ui.selected=new Set([portKey(record)]); ui.detail=portKey(record);
    $('controlMode').value='batch'; $('controlMode').onchange();
    setPortView('control'); renderPorts(); showDetail(record);
    return;
  }
  const address=row.sshPort===22?row.ip:row.ip+':'+row.sshPort;
  run('control.inspect',{targets:[{ip:row.ip,sshPort:row.sshPort,port:row.port}]},(result,cancelled)=>{
    if (cancelled || !result.snapshot || row!==monitor()?.selected() || ui.portView!=='monitor') return;
    ui.inspectedRow=row;
    $('controlMode').value='single'; $('controlMode').onchange();
    $('singleIP').value=address; $('singlePort').value=row.port;
    window.controlTools.showInspection(result.snapshot);
    setPortView('control');
  });
}
function postMatrix() {
  if (!ui.pendingMatrix || !ui.csvLoaded) return;
  $('csvViewer').contentWindow.postMessage({type:'aruba-live-matrix',fileId:ui.pendingMatrix},location.origin);
  ui.pendingMatrix = null;
}
window.addEventListener('message',e=>{
  if (e.origin !== location.origin || !e.data) return;
  if (e.source === $('logViewer').contentWindow && e.data.type === 'aruba-log-loaded' && e.data.ok === false)
    notify('로그 수집 후 분석 결과: ' + e.data.message,true);
  if (e.source === $('csvViewer').contentWindow) {
    if (e.data.type==='aruba-observed-port') selectObservedPort(e.data);
    if (e.data.type==='aruba-port-source') {
      ui.inspectedRow=null;
      if (historicalSource()) { ui.selected.clear(); updateSelection(); }
      if (ui.portView==='control' && historicalSource()) setPortView('monitor');
      updatePortActions();
    }
  }
  if (e.source === $('csvViewer').contentWindow && e.data.type === 'aruba-matrix-loaded' && e.data.ok === false)
    notify('관제 화면에 조회 결과를 표시할 수 없습니다. ' + (e.data.message || ''),true);
});
for (const frame of all('iframe')) frame.addEventListener('load', () => {
  frame.contentWindow.postMessage({type:'aruba-theme',theme:document.documentElement.dataset.theme}, location.origin);
  if (frame.id === 'logViewer') { ui.logLoaded = true; postLog(ui.chosenLog); }
  if (frame.id === 'csvViewer' && frame.contentWindow.location.pathname === '/csv') { ui.csvLoaded = true; updatePortActions(); postMatrix(); }
});
// A cached viewer may finish loading before the deferred script attaches its handler.
if ($('logViewer').contentDocument?.readyState === 'complete' && $('logViewer').contentWindow.location.pathname === '/logs') ui.logLoaded = true;
function route() {
  if (location.hash==='#csv') {
    history.replaceState(null,'','#ports');
    setPortView('monitor');
  }
  const key = location.hash.slice(1);
  const link = all('nav a').find(a => a.hash === '#' + key) || document.querySelector('nav a');
  const name = link.hash.slice(1);
  for (const node of all('nav a')) {
    if (node === link) node.setAttribute('aria-current','page'); else node.removeAttribute('aria-current');
  }
  for (const node of all('[data-page]')) node.hidden = node.dataset.page !== name;
  $('crumb').textContent = link.lastChild.textContent;
  if (name === 'ports' && !$('csvViewer').hasAttribute('src')) $('csvViewer').src = $('csvViewer').dataset.src;
  if (name === 'history' && ui.token) loadHistory();
  if (name === 'home' && ui.token) loadHome();
  if (dialog.open) dialog.close();
  if (queryCredentials.open) queryCredentials.close();
}
window.addEventListener('hashchange',route);
route();

function simpleTable(columns, items, message) {
  if (!items.length) return elem('p',message || '표시할 결과가 없습니다.','empty');
  const wrap = elem('div',undefined,'tablebox'), table = elem('table'), header = table.createTHead().insertRow(), body = table.createTBody();
  for (const [label] of columns) header.append(elem('th',label));
  for (const item of items) {
    const tr = body.insertRow();
    for (const [,field,long] of columns) {
      const cell = tr.insertCell();
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
function renderHostReview(jobId, requests) {
  const panel = $('hostReview'), request = requests[0];
  if (!request) {
    panel.hidden = true; panel.replaceChildren(); delete panel.dataset.request;
    return;
  }
  if (panel.dataset.request === request.id) return;
  panel.dataset.request = request.id;
  panel.hidden = false;
  const content = elem('div',undefined,'panelbody');
  const choices = keyChecklist(request.checks);
  async function respond(approved) {
    for (const node of content.querySelectorAll('button,input')) node.disabled = true;
    try {
      await api('/api/jobs/' + jobId + '/hostkeys',{requestId:request.id,approved});
      content.replaceChildren(elem('p','호스트 키 검토 결과를 적용하고 있습니다.'));
    } catch (err) {
      notify(err.message,true);
      for (const node of content.querySelectorAll('button,input')) node.disabled = false;
    }
  }
  const approve = button('선택한 지문 승인',()=>respond(choices.checked()),'primary');
  approve.disabled = true;
  choices.list.addEventListener('change',()=>{ approve.disabled = !choices.checked().length; });
  const actions = elem('div',undefined,'bar');
  actions.append(approve,button('새 지문 모두 건너뛰기',()=>respond([])));
  content.append(elem('h2','처음 연결하는 장비의 SSH 지문'),elem('p','장비에서 확인한 SHA256 지문과 대조한 항목을 선택하세요. 승인한 장비는 현재 작업을 이어갑니다.'),choices.list,actions);
  panel.replaceChildren(content);
}
function homeSlot(action) {
  if (action === 'backup.run') return $('homeBackup');
  if (['ports.query','ports.refresh','control.apply'].includes(action)) return $('homePorts');
  return null;
}
function displayHomeProgress(action,progress) {
  const slot = homeSlot(action);
  if (slot) slot.textContent = (jobNames[action] || action) + ' · ' + (progress.message || '진행 중') +
    (progress.total ? ' (' + progress.completed + ' / ' + progress.total + ')' : '');
}
async function loadHome() {
  try {
    const data = await api('/api/history');
    for (const slot of [$('homeBackup'),$('homePorts')]) {
      const latest = data.items.find(item=>homeSlot(item.action) === slot);
      slot.textContent = latest ? [jobNames[latest.action],latest.summary || states[latest.state],dateText(latest.finished || latest.started)].join(' · ') : '실행 이력이 없습니다.';
    }
  } catch (err) { notify(err.message,true); }
}
async function applySshAccount(username, password, enableSecret) {
  const user = username.value.trim();
  const credentials = {username:user, password:password.value, enable_secret:enableSecret.value};
  password.value = ''; enableSecret.value = '';
  let applied = false;
  await run('session.open',credentials,(result,cancelled) => {
    if (!result.connected) return;
    ui.session = {active:true, username:user};
    clearPorts();
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
        elem('b',x.endpoint),elem('code',x.fingerprint),elem('p','이 지문을 삭제하면 다음 접속 전에 장비의 호스트 키를 다시 조회하고 승인해야 합니다.')
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
      openDialog('저장 지문 제거',[elem('p','선택한 지문을 제거하면 해당 장비는 다음 접속에서 다시 승인해야 합니다.'),choices.list,apply]);
    }));
  }
}
$('listKeys').onclick = () => run('hostkey.list',{},r=>showStoredKeys(r.keys || []));
form('logForm', () => {
  run('logs.query',{targets:$('logTargets').value.trim()},r => {
    ui.logs = r.logs || [];
    const first = ui.logs.find(log => log.status === 'success' && log.text);
    const output = $('logResults');
    output.hidden = false;
    output.replaceChildren(simpleTable([
      ['스위치','endpoint'],['호스트 이름','hostname'],['수집 결과',x=>stateNames[x.status] || x.status],['상세','message',true],
      ['관제',x=>x.status === 'success' && x.text ? button('이 로그 보기',()=>{ui.chosenLog=x;postLog(x);$('logViewer').scrollIntoView({block:'start'});}) : '—']
    ],ui.logs));
    if (first) { ui.chosenLog = first; postLog(first); }
  });
});
function backup(body) {
  run('backup.run',body,r => {
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
      if (body.output_directory) path.append(elem('b','요청한 추가 저장 위치 '),elem('code',body.output_directory),elem('br'));
      path.append(elem('b',body.output_directory ? '실제 저장 결과 폴더 ' : '기본 저장 폴더 '),elem('code',r.output_directory));
      area.prepend(path);
    }
    if (r.files?.length) area.append(openFolder(r.files[0]));
    for (const warning of r.warnings || []) area.append(elem('p',warning,'warning'));
    const retry = rows.filter(x=>x.status === 'retry_exhausted');
    if (retry.length) {
      const again = button('접속 실패 장비만 다시 시도 (' + retry.length + ')',()=>
        backup({...body, targets:retry.map(x=>x.endpoint).join('\n')}));
      again.dataset.live = ''; area.append(again);
    }
  });
}
form('backupForm', () => backup({targets:$('backupTargets').value, concurrency:Number($('concurrency').value),
  max_attempts:Number($('attempts').value), ssh_port:Number($('backupSSHPort')?.value || 22),
  filename_mode:$('fileMode').value, output_directory:$('backupDirectory').value.trim()}));

const endpoint = row => Number(row.ssh_port || 22) === 22 ? row.ip : row.ip + ':' + row.ssh_port;
const portKey = row => endpoint(row) + '|' + row.interface;
const targetFromRow = row => ({ip:row.ip,sshPort:Number(row.ssh_port || 22),port:String(row.interface)});
const portPageSize = 100;
let portPage = 0;
function clearPorts() {
  ui.rows = []; ui.selected.clear(); ui.detail = null; portPage = 0;
  $('queryFiles').replaceChildren(); $('rowDetails').hidden = true; $('controlResult').hidden = true;
  ui.matrixFile=null; ui.pendingMatrix=null; ui.inspectedRow=null;
  monitor()?.invalidateLive();
  $('showMatrix').hidden=true; $('liveResultsBar').hidden=true;
  if (ui.portView==='control' && historicalSource()) setPortView('monitor');
  updatePortActions();
  $('switchFilter').replaceChildren(new Option('전체 스위치',''));
  renderPorts();
}
function filteredRows() {
  const term = $('portSearch').value.trim().toLowerCase(), device = $('switchFilter').value, filter = $('portFilter').value;
  const conditions = {
    failed:row=>row.status === 'failed' || row.lldp_status === 'failed', unknown:row=>!row.assessment_label || row.assessment_label === '확인 필요',
    off:row=>row.poe_enabled === 'no', disabled:row=>row.enabled === 'no'
  };
  return ui.rows.filter(row => (!filter || conditions[filter](row)) && (!device || device === endpoint(row)) && (!term ||
    [endpoint(row),row.hostname,row.label,row.interface,row.ap_names,row.poe_status,row.link_status,row.assessment_model,row.port_poe_state,row.message].join(' ').toLowerCase().includes(term)));
}
function displayValue(value) {
  if (value == null || value === '') return '—';
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}
function showDetail(row) {
  ui.detail = portKey(row);
  $('rowDetails').hidden = false;
  $('rowDetails').open = true;
  $('rowDetails').querySelector('summary').textContent = (row.hostname || row.ip) + ' · 포트 ' + row.interface + ' 상세';
  const items = [];
  for (const [key,label] of Object.entries(labels)) {
    if (row[key] === undefined || row[key] === null || row[key] === '') continue;
    const value = /(?:_at|_time)$/.test(key) ? dateText(row[key]) : displayValue(row[key]);
    items.push(elem('dt',label),elem('dd',value));
  }
  $('detailFields').replaceChildren(...items);
  for (const tr of $('portTable').querySelectorAll('tr[data-key]')) tr.classList.toggle('active',tr.dataset.key === ui.detail);
  window.portTools?.selectDetail?.(row);
}
function updateSelection() {
  const shown = all('#portTable tbody tr').filter(tr=>ui.selected.has(tr.dataset.key)).length;
  $('selectedCount').textContent = '선택 ' + ui.selected.size + '개' + (ui.selected.size > shown ? ' · 화면 밖 ' + (ui.selected.size - shown) + '개' : '');
  window.portTools?.syncChecks?.();
}
function renderPorts() {
  const filtered = filteredRows(), pages = Math.max(1, Math.ceil(filtered.length / portPageSize));
  portPage = Math.min(portPage,pages - 1);
  const start = portPage * portPageSize, visible = filtered.slice(start,start + portPageSize);
  const table = simpleTable([
    ['선택',row => {
      const field = elem('input'); field.type = 'checkbox';
      field.checked = ui.selected.has(portKey(row)); field.disabled = row.status !== 'ok' || row.controlUsed;
      field.setAttribute('aria-label',endpoint(row) + ' 포트 ' + row.interface + ' 선택');
      field.onchange = () => {
        if (field.checked) ui.selected.add(portKey(row)); else ui.selected.delete(portKey(row));
        updateSelection();
      };
      return field;
    }],['스위치',row=>row.hostname || row.ip],['IP',endpoint],['포트','interface'],
    ['공급 전력',row=>row.watts === '' || row.watts == null ? '—' : row.watts + ' W'],
    ['PoE 상태',row=>row.poe_status || '—'],['링크',row=>row.link_status || '—'],
    ['AP 이름',row=>row.ap_names || '—'],['조회',row=>stateNames[row.status] || row.status],
    ['관측 시각',row=>dateText(row.observed_at || row.finished_at)],
    ['상세',row=>button('상세 보기',()=>showDetail(row),'detailsbtn')]
  ],visible,ui.rows.length ? '검색 조건에 맞는 포트가 없습니다.' : '조회한 포트가 없습니다.');
  for (const [index,tr] of all('tbody tr',table).entries()) {
    tr.dataset.key = portKey(visible[index]);
    tr.classList.toggle('active',tr.dataset.key === ui.detail);
  }
  $('portTable').replaceChildren(table);
  $('rowSummary').textContent = filtered.length ? '전체 ' + ui.rows.length + '개 · 검색 ' + filtered.length + '개 · ' + (start + 1) + '–' + (start + visible.length) + ' 표시' : '';
  $('prevRows').hidden = $('nextRows').hidden = pages <= 1;
  $('prevRows').disabled = portPage === 0; $('nextRows').disabled = portPage === pages - 1;
  updateSelection();
  window.portTools?.renderMatrix?.();
}
$('portSearch').oninput = $('switchFilter').onchange = $('portFilter').onchange = () => { portPage = 0; renderPorts(); };
$('prevRows').onclick = () => { portPage--; renderPorts(); };
$('nextRows').onclick = () => { portPage++; renderPorts(); };
function receivePorts(result, openMonitor=false) {
  if (!Array.isArray(result.rows)) return;
  const previousSwitch = $('switchFilter').value;
  ui.rows = result.rows;
  const switches = new Map(ui.rows.map(row => [endpoint(row),row.hostname]));
  $('switchFilter').replaceChildren(new Option('전체 스위치',''));
  for (const [address,host] of switches) $('switchFilter').add(new Option(host ? host + ' · ' + address : address,address));
  if (switches.has(previousSwitch)) $('switchFilter').value = previousSwitch;
  const available = new Set(ui.rows.filter(row=>row.status === 'ok').map(portKey));
  ui.selected = new Set([...ui.selected].filter(key=>available.has(key)));
  $('queryFiles').replaceChildren(filelinks(result.files));
  const file=(result.files || []).find(f=>f.name.toLowerCase().endsWith('.csv'));
  if (file) {
    const replaceObservation=openMonitor || !historicalSource();
    ui.matrixFile=file.id;
    $('liveResultsBar').hidden=$('showMatrix').hidden=false;
    if (replaceObservation) { ui.pendingMatrix=file.id; postMatrix(); }
    if (openMonitor) setPortView('monitor');
  }
  renderPorts();
  const selected = ui.rows.find(row=>portKey(row) === ui.detail);
  if (selected) showDetail(selected); else $('rowDetails').hidden = true;
}
function queryPorts(targets) {
  clearPorts();
  return run('ports.query',{targets},result=>receivePorts(result,true));
}
form('queryForm', () => {
  if (ui.busy || !ui.token || queryCredentials.open) return;
  const input = $('queryTargets'), targets = input.value.trim();
  if (!targets) {
    input.value = '';
    input.reportValidity();
    return;
  }
  if (ui.session.active) {
    queryPorts(targets);
    return;
  }
  $('queryLoginError').hidden = true;
  queryCredentials.showModal();
});
$('cancelQueryCredentials').onclick = () => queryCredentials.close();
queryCredentials.addEventListener('close', () => $('queryLoginForm').reset());
form('queryLoginForm', async () => {
  if (!queryCredentials.open || ui.busy || !ui.token) return;
  const targets = $('queryTargets').value.trim();
  const submit = $('submitQueryCredentials'), error = $('queryLoginError');
  error.hidden = true;
  submit.textContent = '계정 적용 중…';
  const applied = await applySshAccount($('queryUsername'), $('queryPassword'), $('queryEnableSecret'));
  submit.textContent = '계정 적용 후 조회';
  if (!queryCredentials.open) return;
  if (!applied) {
    error.textContent = $('statusText').textContent;
    error.hidden = false;
    $('queryPassword').focus();
    return;
  }
  queryCredentials.close();
  queryPorts(targets);
});
function refreshTargets(bySwitch) {
  if (window.portTools?.refreshTargets) {
    const targets = window.portTools.refreshTargets(bySwitch);
    if (targets) run('ports.refresh',{targets},result=>receivePorts(result));
    return;
  }
  const detail = ui.rows.find(row=>portKey(row) === ui.detail);
  let chosen;
  if (bySwitch) {
    const device = $('switchFilter').value || (detail && endpoint(detail));
    if (!device) { notify('상세 보기 또는 스위치 필터에서 재조회할 장비를 지정하세요.',true); return; }
    chosen = ui.rows.filter(row=>endpoint(row) === device);
  } else {
    chosen = ui.rows.filter(row=>ui.selected.has(portKey(row)));
    if (!chosen.length && detail) chosen = [detail];
    if (!chosen.length) { notify('재조회할 포트를 체크하거나 상세 보기를 여세요.',true); return; }
  }
  const groups = new Map();
  for (const row of chosen) {
    const key = endpoint(row);
    if (!groups.has(key)) groups.set(key,[]);
    groups.get(key).push(row.interface);
  }
  const targets = [...groups].map(([key,ports])=>key + ' ' + ports.join(',')).join('\n');
  run('ports.refresh',{targets},result=>receivePorts(result));
}
$('refreshPorts').onclick = () => refreshTargets(false);
$('refreshSwitch').onclick = () => refreshTargets(true);
$('showMatrix').onclick = () => {
  if (!ui.matrixFile) return;
  ui.pendingMatrix = ui.matrixFile;
  setPortView('monitor');
  postMatrix();
};
function selectionScope() {
  if ($('selectionScope').value === 'all') return ui.rows;
  if ($('selectionScope').value === 'switch') {
    const detail = ui.rows.find(row=>portKey(row) === ui.detail);
    const device = $('switchFilter').value || (detail && endpoint(detail));
    if (!device) { notify('현재 스위치를 필터 또는 상세 보기에서 지정하세요.',true); return null; }
    return ui.rows.filter(row=>endpoint(row) === device);
  }
  return filteredRows();
}
$('selectVisible').onclick = () => {
  if (window.portTools?.scopeKeys) {
    const keys = window.portTools.scopeKeys();
    if (keys) {ui.selected = new Set(keys); renderPorts();}
    return;
  }
  const rows = selectionScope();
  if (!rows) return;
  for (const row of rows) if (row.status === 'ok' && !row.controlUsed) ui.selected.add(portKey(row));
  renderPorts();
};
$('selectEligible').onclick = () => {
  const rows = selectionScope();
  if (!rows) return;
  ui.selected.clear();
  for (const row of rows) if (!row.controlUsed && (row.eligible_operations || []).includes($('operation').value)) ui.selected.add(portKey(row));
  renderPorts();
};
$('clearSelection').onclick = () => { ui.selected.clear(); renderPorts(); };
$('controlMode').onchange = () => {
  const single = $('controlMode').value === 'single';
  $('batchControls').hidden = single; $('singleControls').hidden = !single;
  if (single) {
    const detail = ui.rows.find(row=>portKey(row) === ui.detail);
    if (detail) { $('singleIP').value = endpoint(detail); $('singlePort').value = detail.interface; }
  }
  $('controlHint').textContent = single ? '직접 입력한 포트의 현재 상태와 전송할 명령을 먼저 검토합니다.' : '일괄 전원 OFF와 포트 Down은 LLDP로 AP-535가 확인된 포트에서 사용할 수 있습니다.';
};
function renderPlan(plan) {
  const title = operationNames[plan.operation] || plan.operation;
  const targets = simpleTable([['스위치',x=>x.hostname || x.ip],['IP',x=>x.sshPort && x.sshPort !== 22 ? x.ip + ':' + x.sshPort : x.ip],['포트','port'],['현재 상태','detail',true],['전송할 명령','command',true]],plan.targets || []);
  const phrase = elem('code',plan.confirmationText), input = elem('input'), label = elem('label','표시된 확인 문구를 그대로 입력하세요.');
  input.autocomplete = 'off'; input.spellcheck = false; label.append(input);
  const execute = button('승인하고 명령 실행',() => {
    if (!plan.confirmationText || (!plan.quick && input.value !== plan.confirmationText)) return;
    dialog.close();
    run('control.apply',{planId:plan.planId,confirmation:plan.quick ? plan.confirmationText : input.value},r => {
      const results = r.results || [], box = $('controlResult');
      box.hidden = false;
      box.append(elem('p',r.message || title + ' 실행 결과'),simpleTable([['스위치','ip'],['포트','port'],['결과',x=>stateNames[x.status] || x.status],['상세','message',true],['변경 후 상태',x=>x.after ? [x.after.detail,x.after.poeStatus].filter(Boolean).join(' · ') : '재조회 필요',true]],results));
      window.controlTools?.finishProgress();
      for (const changed of results) {
        for (const row of ui.rows) if (row.ip === changed.ip && Number(row.ssh_port || 22) === Number(changed.sshPort || 22) && String(row.interface) === String(changed.port)) {
          ui.selected.delete(portKey(row));
          row.controlUsed = true;
        }
      }
      renderPorts();
      if (r.nextPlan) renderPlan(r.nextPlan);
    });
  },'danger');
  execute.disabled = !plan.quick;
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
  if (plan.quick) {
    if (plan.skipped?.length) children.push(elem('p','이미 ON 상태로 확인된 ' + plan.skipped.length + '개 포트는 제외했습니다.'));
    if (plan.targets.length > 10) children.push(elem('p','승인한 전체 대상을 순차 실행합니다. 진행 중에는 작업 취소를 누를 수 있습니다.','hint'));
    openDialog('빠른 PoE 제어 승인',[...children,execute]);
  } else {
    openDialog('포트 제어 승인',[...children,phrase,label,execute]);
    input.focus();
  }
}
$('preview').onclick = () => {
  const mode = $('controlMode').value;
  let targets;
  if (mode === 'single') {
    const ip = $('singleIP').value.trim(), port = $('singlePort').value.trim();
    if (!ip || !port) { notify('단일 제어할 스위치 주소와 포트를 입력하세요.',true); return; }
    targets = [{ip,port}];
  } else {
    targets = window.portTools?.checkedTargets() || ui.rows.filter(row => ui.selected.has(portKey(row))).map(targetFromRow);
    if (!targets.length) { notify('조회 결과에서 제어할 포트를 체크하세요.',true); return; }
  }
  $('controlResult').replaceChildren(); $('controlResult').hidden = true;
  run('control.preview',{operation:$('operation').value,scope:mode,targets},plan => {
    if (plan.planId) renderPlan(plan);
  });
};
function dateText(value) {
  if (!value) return '—';
  const stamp = new Date(value);
  return isNaN(stamp.getTime()) ? value : stamp.toLocaleString('ko-KR',{hour12:false});
}
async function loadHistory() {
  try {
    const data = await api('/api/history');
    $('historyTable').replaceChildren(simpleTable([
      ['시작',r=>dateText(r.started)],['작업',r=>jobNames[r.action] || r.action],['상태',r=>states[r.state] || r.state],
      ['종료',r=>dateText(r.finished)],['결과','summary',true],['파일',r=>{
        const links = filelinks(r.files);
        if (r.action === 'backup.run' && r.files?.length) links.append(openFolder(r.files[0]));
        return links;
      },true]
    ],data.items || [],'아직 실행한 작업이 없습니다.'));
  } catch (e) { $('historyTable').replaceChildren(elem('p',e.message,'warning')); }
}
$('refreshHistory').onclick = loadHistory;
$('backupHistory').onclick = () => run('backup.history',{output_directory: $('backupDirectory').value.trim()},r=>{
  $('backupHistoryTable').replaceChildren(simpleTable([
    ['저장 위치',x=>({desktop:'데스크톱',selected:'지정한 백업 폴더',web:'로컬 웹'}[x.source] || '—')],['실행','run'],['스위치','hostname'],['IP','ip'],
    ['결과',x=>stateNames[x.status] || x.status],['파일 경로','path',true],['폴더', x=>window.backupTools?.historyButton(x)]
  ],r.rows || [],'저장된 백업 보고서가 없습니다.'));
});
$('controlHistory').onclick = () => run('control.history',{},r => {
  $('controlHistoryTable').replaceChildren(simpleTable([
    ['시각',x=>dateText(x.time)],['IP','ip'],['SSH 포트','sshPort'],['포트','port'],['호스트','hostname'],
    ['작업',x=>operationNames[x.operation] || x.operation],['결과',x=>stateNames[x.status] || x.status],['출처','source'],
    ['폴더', x=>window.backupTools?.historyButton(x)]
  ],r.rows || [],'기록된 포트 제어 명령이 없습니다.'));
  window.controlTools?.explainUnknown(r.rows || []);
});
let fileDestination = null;
for (const node of all('[data-import]')) node.onclick = () => {
  fileDestination = node.dataset.import;
  $('targetFile').accept = fileDestination === 'queryTargets' ? '.txt,.tsv,.csv,.json' : '.txt,.tsv,.csv';
  $('targetFile').value = '';
  $('targetFile').click();
};
$('targetFile').onchange = async () => {
  const file = $('targetFile').files[0], dest = fileDestination;
  if (!file || !dest) return;
  if (file.size > 2 * 1024 * 1024) { notify('2 MB 이하인 대상 목록 파일을 선택하세요.',true); return; }
  try {
    const text = (await file.text()).replace(/^\uFEFF/,'');
    if (dest === 'queryTargets' && file.name.toLowerCase().endsWith('.json')) {
      let profile;
      await run('profiles.import',{text},r=>{ profile = r; });
      if (profile && dest === 'queryTargets') {
        await run('targets.apply',{targets:profile.targets},applied=>{
          clearPorts(); window.portTools.restoreInventory(applied.groups,applied.targets);
        });
      }
    } else { $(dest).value = text; $(dest).dispatchEvent(new Event('input')); }
  } catch (e) { notify(e.message,true); }
};
for (const node of all('[data-save]')) node.onclick = () => {
  const value = $(node.dataset.save).value.trim();
  if (!value) { notify('저장할 스위치 주소를 입력하세요.',true); return; }
  const a = document.createElement('a');
  const url = URL.createObjectURL(new Blob(['\uFEFF' + value + '\n'],{type:'text/plain;charset=utf-8'}));
  a.href = url; a.download = 'switch-targets.txt'; a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
};
$('exportProfile').onclick = () => run('profiles.export',{targets:$('queryTargets').value.trim()},r => {
  $('profileFiles').hidden = false; $('profileFiles').replaceChildren(filelinks(r.files));
});
$('shutdown').onclick = () => confirmDialog('로컬 웹 서버 종료',[elem('p','현재 SSH 세션을 닫고 이 PC의 로컬 웹 서버를 종료합니다.')],'서버 종료',async () => {
  try {
    await api('/api/shutdown',{});
    ui.token = ''; ui.session = {active:false}; clearPorts(); sync();
    notify('로컬 웹 서버를 종료했습니다. 다시 사용하려면 Start-Local-Web.cmd를 여세요.');
  } catch (e) { notify(e.message,true); }
});
async function start() {
  sync();
  try {
    const bootstrap = await api('/api/bootstrap');
    ui.token = bootstrap.token;
    ui.session = bootstrap.session || {active:false};
    $('version').textContent = 'v' + String(bootstrap.version || '').replace(/^v/,'');
    if (bootstrap.busyJob) {
      ui.busy = true; ui.jobId = bootstrap.busyJob; sync();
      notify('진행 중인 작업을 기다리고 있습니다.');
      try { await poll(bootstrap.busyJob); }
      finally {
        ui.busy = false; ui.jobId = null;
        const latest = await api('/api/bootstrap'); ui.session = latest.session || {active:false};
      }
      notify('기존 작업이 끝났습니다. 작업 이력에서 결과를 확인하세요.');
    }
    const workspace = await api('/api/workspace');
    if (workspace.groups?.length && !$('queryTargets').value.trim()) {
      const quote = value => '"' + String(value ?? '').replaceAll('"','""') + '"';
      const lines = [['IP','ssh_port','interfaces','label'],...workspace.groups.map(g=>[g.ip,g.ssh_port,g.interfaces.join(','),g.label])];
      const targets = lines.map(row=>row.map(quote).join('\t')).join('\n');
      if (document.readyState === 'loading') await new Promise(resolve=>window.addEventListener('DOMContentLoaded', resolve,{once:true}));
      window.portTools?.restoreInventory?.(workspace.groups,targets);
      if (workspace.query) receivePorts(workspace.query);
    }
    route();
  } catch (error) { notify(error.message,true); }
  finally { sync(); }
}
start();
