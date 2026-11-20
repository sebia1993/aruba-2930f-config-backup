/* Register SSH targets and show the same observed power states as the live matrix. */
(() => {
  const raw = $('queryTargets');
  const jobText = {'targets.preview':'대상 목록 검증', 'targets.apply':'대상 목록 적용'};
  Object.assign(jobNames,jobText);
  let groups = [], preview = null, page = 0, focused = '', live = new Map(), loading = new Set();
  let matrixJob = '', registeredText = '', progressGroups = null, fullQuery = null;
  let directCableTarget=false;
  let failureView=null, powerFilters=new Set(), powerFiltersEdited=false;
  let shownKeys=new Set(), matchedGroups=new Map();
  const pageSize = 192;
  const noPowerData = PowerMatrix.classify({});
  const phaseLabels = {pending:'조회 대기',loading:'조회 중',stale:'재조회 필요'};
  const phaseReasons = {pending:'이 포트의 관측값이 없습니다.',loading:'조회가 완료되면 전력을 판정합니다.',stale:'유효한 최신 관측값이 없습니다. 현재 전력 상태를 다시 조회하세요.'};
  const cmp = new Intl.Collator('en',{numeric:true}).compare;
  const matrix = document.createElement('article');
  matrix.className = 'panel port-matrix-panel';
  matrix.hidden = true;
  matrix.innerHTML = '<header><h2>조회 대상 및 진행 상태</h2><div class="tools"><label>등록 포트 검색 <input id="inventorySearch" type="search" placeholder="장비, 주소, 포트"></label><label>크기 <select id="matrixSize"><option value="small">작게</option><option value="medium" selected>보통</option><option value="large">크게</option></select></label></div></header><div class="panelbody"><p id="matrixCounts" class="hint"></p><div id="matrixGroups"></div><div id="matrixPager" class="bar" hidden><button type="button" id="matrixPrev">이전</button><span id="matrixPage"></span><button type="button" id="matrixNext">다음</button></div><div id="matrixDetails" hidden></div></div>';
  $('monitorInventory').append(matrix);

  // Count each power state before applying the selected states so more can be added.
  const legend = elem('div',undefined,'matrix-legend');
  legend.id = 'matrixLegend';
  legend.setAttribute('role','group');
  legend.setAttribute('aria-label','전력 상태 필터');
  for (const state of ['unpowered','limited','normal','unclassified']) {
    const label=state==='unclassified'?'전력 미측정':PowerMatrix.labels[state];
    const entry=button(label,()=>{
      if (powerFilters.has(state)) powerFilters.delete(state); else powerFilters.add(state);
      powerFiltersEdited=true;
      page=0;
      renderMatrix();
      updateMatrixSelectAll();
    },'power-badge');
    entry.dataset.powerFilter=entry.dataset.state=state;
    entry.setAttribute('aria-controls','matrixGroups');
    entry.append(elem('b','0'));
    legend.append(entry);
  }
  $('matrixCounts').before(legend);
  const countText=elem('span');
  const failureToggle=button('',toggleFailures);
  failureToggle.setAttribute('aria-controls','matrixGroups');
  failureToggle.hidden=true;
  $('matrixCounts').append(countText,failureToggle);

  const editor = $('targetEditor'), modeField = $('targetInputMode'), rawDraft = $('editorTargets');
  const rowPane = $('targetRowsPane'), pastePane = $('targetPastePane');
  let draftVersion = 0, rowsSource = null, rowsEdited = false;

  function targetCount(items) { return items.reduce((n,g)=>n+g.interfaces.length,0); }
  function targetSummary(items,unit='포트') {
    const count=targetCount(items);
    const pending=items.filter(g=>g.all_ports && !g.interfaces.length).length;
    const parts=[items.length+'대'];
    if (count || !pending) parts.push(count+unit);
    if (pending) parts.push('전체 포트 조회 '+pending+'대');
    return parts.join(' · ');
  }
  function stableGroups(items) { return items.map(g=>({...g,interfaces:[...g.interfaces].sort(cmp)})).sort((a,b)=>cmp(a.ip,b.ip)||Number(a.ssh_port)-Number(b.ssh_port)); }
  function editorDirty() {
    draftVersion++;
    preview = null;
    $('acceptTargets').disabled = true;
    $('targetPreview').replaceChildren();
    $('targetMessage').textContent = '';
  }
  function addRow(group) {
    if ($('editTargetRows').children.length >= 300) { $('targetMessage').textContent = '최대 300개 스위치 행을 추가할 수 있습니다.'; return; }
    const row = $('editTargetRows').insertRow();
    for (const [field,value,label] of [['ip',group?.ip || '','스위치 IPv4'],['interfaces',group?.all_ports ? '' : group?.interfaces.join(',') || '','인터페이스 목록'],['ssh_port',group?.ssh_port || 22,'SSH 포트'],['label',group?.label || '','별칭']]) {
      const cell = row.insertCell();
      cell.dataset.label=label;
      if (['ssh_port','label'].includes(field)) cell.className = 'advanced';
      const input = elem('input');
      input.value = value;
      input.dataset.field=field;
      input.setAttribute('aria-label',label);
      if (field === 'interfaces') input.placeholder = '비워 두면 전체 포트';
      if (field === 'label') input.maxLength = 80;
      if (field === 'ssh_port') { input.type='number'; input.min=1; input.max=65535; }
      cell.append(input);
    }
    row.insertCell().append(button('삭제',()=>{row.remove();rowsEdited=true;editorDirty();}));
    return row;
  }
  function previewPayload() {
    if (modeField.value === 'raw') return {targets:rawDraft.value};
    if (modeField.value === 'paste') return {targets:$('pasteTargets').value, mode:$('pasteMode').value,existing:rawDraft.value};
    return {rows:[...$('editTargetRows').rows].map(tr=>Object.fromEntries([...tr.querySelectorAll('input')].map(node=>[node.dataset.field,node.value])))};
  }
  async function requestPreview(body, receive) {
    const generation = ++draftVersion;
    let received = false;
    await run('targets.preview',body,r=>{
      if (!editor.open || generation !== draftVersion) return;
      received = true; receive(r);
    });
    if (editor.open && generation === draftVersion && !received) {
      $('targetMessage').textContent = $('statusText').textContent || '목록을 검증하지 못했습니다.';
    }
  }
  function showPreview(result) {
    preview = result;
    $('targetMessage').textContent = targetSummary(result.groups,'개 포트');
    $('targetPreview').replaceChildren(simpleTable([['스위치 IPv4','ip'],['SSH 포트','ssh_port'],['포트',g=>g.all_ports ? '전체 포트' : g.interfaces.join(', ')],['별칭','label']],result.groups,'등록 목록을 비웁니다.'));
    $('acceptTargets').disabled = false;
  }
  function targetDraftVersion() {
    return editor.open && modeField.value === 'raw' ? draftVersion : null;
  }
  async function selectInputMode() {
    editorDirty();
    $('targetRawPane').hidden = modeField.value !== 'raw';
    rowPane.hidden = modeField.value !== 'rows';
    pastePane.hidden = modeField.value !== 'paste';
    if (rowPane.hidden || rowsEdited || rowsSource === rawDraft.value) return;
    const source = rawDraft.value;
    $('editTargetRows').replaceChildren();
    const populate = items => {
      rowsSource = source;
      $('advancedTargets').checked = items.some(g=>Number(g.ssh_port)!==22 || g.label);
      rowPane.classList.toggle('show-advanced',$('advancedTargets').checked);
      if (items.length) items.forEach(addRow); else addRow();
    };
    if (!source.trim()) populate([]);
    else if (source === registeredText) populate(groups);
    else {
      await requestPreview({targets:source},r=>populate(r.groups));
      if (editor.open && !rowPane.hidden && rawDraft.value === source && !rowsEdited && !$('editTargetRows').rows.length) addRow();
    }
  }
  function openTargetEditor() {
    rawDraft.value = raw.value;
    modeField.value = 'raw';
    rowsEdited = false;
    rowsSource = null;
    $('editTargetRows').replaceChildren();
    $('pasteTargets').value = '';
    $('pasteMode').value = 'replace';
    editor.showModal();
    selectInputMode();
    rawDraft.focus();
  }
  $('manageTargets').onclick = openTargetEditor;
  modeField.onchange = selectInputMode;
  $('targetClose').onclick = ()=>editor.close();
  editor.addEventListener('close',editorDirty);
  rawDraft.addEventListener('input',editorDirty);
  rowPane.addEventListener('input', e=>{if (e.target.id !== 'advancedTargets') { rowsEdited=true; editorDirty(); }});
  pastePane.addEventListener('input',editorDirty);
  $('advancedTargets').onchange = e=>rowPane.classList.toggle('show-advanced',e.target.checked);
  $('addTargetRow').onclick = ()=>{const tr=addRow(); if (tr) {rowsEdited=true;editorDirty();tr.querySelector('input').focus();}};
  $('readClipboard').onclick = async ()=>{
    const generation = draftVersion;
    try {
      const text = await navigator.clipboard.readText();
      if (!editor.open || generation !== draftVersion) return;
      $('pasteTargets').value = text;
      editorDirty();
    } catch {
      if (!editor.open || generation !== draftVersion) return;
      $('targetMessage').textContent = '입력란의 붙여넣기 기능을 사용하세요.';
    }
    $('pasteTargets').focus();
  };
  $('verifyEditor').onclick = ()=>{editorDirty();requestPreview(previewPayload(),showPreview);};
  $('acceptTargets').onclick = async ()=>{
    if (!preview) return;
    const accepted = preview;
    $('acceptTargets').disabled = true;
    await run('targets.apply',accepted.groups.length ? {targets:accepted.targets} : {rows:[]},r=>{
      directCableTarget=false;
      groups = stableGroups(r.groups);
      raw.value = r.targets;
      registeredText = raw.value;
      failureView=null; powerFilters.clear(); powerFiltersEdited=false; focused = ''; page = 0; live.clear(); loading.clear(); $('inventorySearch').value=''; clearPorts();
      $('controlMode').value=groups.length?'batch':'single'; $('controlMode').onchange();
      updateTargetCount();
      if (editor.open && preview === accepted) editor.close();
      renderMatrix();
    });
    if (editor.open && preview === accepted) {
      $('targetMessage').textContent = $('statusText').textContent; $('acceptTargets').disabled = false;
    }
  };
  function updateTargetCount() {
    const dirty=raw.value!==registeredText;
    const count=groups.length ? targetSummary(groups)+' 등록' : '';
    const draft=dirty ? '입력한 목록은 검증하여 적용하거나 포트를 조회하면 반영됩니다.' : '';
    $('targetCount').textContent=[count,draft].filter(Boolean).join(' · ');
  }
  function clearSshObservations(endpoints) {
    groups=groups.map(group=>group.all_ports && (!endpoints || endpoints.has(group.ip+':'+group.ssh_port))
      ? {...group,interfaces:[]} : group);
    live.clear(); loading.clear(); focused=''; failureView=null;
    updateTargetCount();
    clearPorts();
  }
  raw.addEventListener('input',()=>{ directCableTarget=false; updateTargetCount(); });
  for (const input of [$('singleIP'),$('singlePort')]) {
    input.addEventListener('input',()=>{ directCableTarget=true; });
  }
  $('controlMode').addEventListener('change',()=>{ directCableTarget=false; });

  const address = g=>Number(g.ssh_port || 22) === 22 ? g.ip : g.ip+':'+g.ssh_port;
  function inventory() {
    if (groups.length) return groups;
    const items = new Map();
    for (const r of ui.rows) {
      const k=endpoint(r);
      if (!items.has(k)) items.set(k,{ip:r.ip,ssh_port:r.ssh_port,label:r.label || '',interfaces:[]});
      items.get(k).interfaces.push(r.interface);
    }
    return stableGroups([...items.values()]);
  }
  function invalidatedKeys() {
    return new Set(ui.rows.filter(row=>row.refreshInvalidated || (row.powerObserved && row.controlUsed)).map(portKey));
  }
  function unobservedTargets() {
    const observed=new Set(ui.rows.map(portKey));
    const missing=[];
    for (const g of inventory()) for (const port of g.interfaces) {
      if (!observed.has(address(g)+'|'+port)) missing.push({ip:g.ip,sshPort:Number(g.ssh_port||22),port,label:g.label});
    }
    return missing;
  }
  function availableControlTargets(operation=$('operation').value,live=!!ui.liveId) {
    const targets=[];
    const restricted=operation!=='disable';
    const fresh=restricted && live ? new Set(ui.rows.filter(freshControlRow).map(portKey)) : null;
    const blocked=restricted && !live ? invalidatedKeys() : null;
    for (const g of inventory()) for (const port of g.interfaces) {
      const key=address(g)+'|'+port;
      if (controlPortSupported(port) && (!fresh || fresh.has(key)) && !blocked?.has(key)) targets.push({ip:g.ip,sshPort:Number(g.ssh_port || 22),port});
    }
    return targets;
  }
  function cableScope() {
    if (directCableTarget && $('controlMode').value==='single') {
      const ip=$('singleIP').value.trim(), port=$('singlePort').value.trim();
      const complete=ip && !/\s/.test(ip) && controlPortSupported(port);
      return {label:'포트·PoE 관리에서 직접 지정한 단일 포트',targets:complete ? ip+' '+port : ''};
    }
    // An edited or cleared query supersedes selections from the previous inventory.
    if (raw.value !== registeredText) {
      if (raw.value.split(/\r?\n/).some(line=>/^\s*\d+(?:\.\d+){3}(?::\d+)?\s*$/.test(line))) {
        return {label:'스위치를 조회한 뒤 진단할 포트를 선택하세요.',targets:''};
      }
      return {label:'포트·PoE 관리에 입력한 조회 대상', targets:raw.value.trim()};
    }
    const items=inventory();
    const tile=ui.liveId ? ui.tileRow : null;
    const focusKey=ui.liveId ? (tile && tile===monitor()?.selected() ? controlPortKey(tile) : '') : focused;
    const keys=ui.selected.size ? ui.selected : focusKey ? new Set([focusKey]) : null;
    if (!keys && items.some(g=>g.all_ports)) return {label:'포트·PoE 관리에서 진단할 포트를 선택하세요.',targets:''};
    const chosen=keys ? items.map(g=>({...g,interfaces:g.interfaces.filter(p=>keys.has(address(g)+'|'+p))})).filter(g=>g.interfaces.length) : items;
    if (chosen.some(g=>g.interfaces.some(port=>!controlPortSupported(port)))) return {label:'케이블 진단이 가능한 숫자 포트를 선택하세요.',targets:''};
    const count=targetCount(chosen);
    const label=keys ? '포트·PoE 관리에서 선택한 대상' : '포트·PoE 관리의 전체 조회 대상';
    if (keys && count!==keys.size) return {label:'포트·PoE 관리에서 포트를 다시 선택하세요.', targets:''};
    return {label, groups:chosen, targets:chosen.map(g=>address(g)+' '+g.interfaces.join(',')).join('\n')};
  }
  function currentScope() {
    if (!ui.liveId) return null;
    const scope=monitor()?.scope();
    return scope?.liveId===ui.liveId ? scope : null;
  }
  function currentKey() {
    if (!ui.liveId) return focused;
    const current=monitor()?.selected();
    return current ? portKey({ip:current.ip,ssh_port:current.sshPort,interface:current.port}) : '';
  }
  function currentAddress() {
    return currentScope()?.device || currentKey().split('|')[0];
  }
  function scopeKeys() {
    const range=$('selectionScope').value, scope=currentScope();
    const device=range==='switch' && currentAddress();
    if (range==='switch' && !device) {
      return [];
    }
    const available=availableControlTargets().map(controlPortKey);
    if (range==='filtered') {
      if (ui.liveId && !scope) {
        return [];
      }
      const visible=new Set(scope ? scope.rows.map(controlPortKey) : visibleEntries(entries()).map(item=>item.key));
      return available.filter(key=>visible.has(key));
    }
    return device ? available.filter(key=>key.split('|')[0]===device) : available;
  }
  function hiddenSelectedCount(selected) {
    const scope=currentScope();
    const visible=scope ? new Set(scope.visibleRows.map(controlPortKey)) : shownKeys;
    let hidden=0;
    for (const key of selected) if (!visible.has(key)) hidden++;
    return hidden;
  }
  function selectedRefreshGroups() {
    const current=ui.selected.size ? '' : currentKey();
    if (!ui.selected.size && !current) return [];
    const selected=[];
    for (const group of inventory()) {
      const addr=address(group);
      const interfaces=group.interfaces.filter(port=>{
        const key=addr+'|'+port;
        return ui.selected.size ? ui.selected.has(key) : key===current;
      });
      if (interfaces.length) selected.push({...group,interfaces});
    }
    return selected;
  }
  $('refreshPorts').onclick=()=>{
    const selected=selectedRefreshGroups();
    if (!selected.length) return;
    focused=currentKey() || focused;
    const targets=selected.map(group=>address(group)+' '+group.interfaces.join(',')).join('\n');
    runSsh('ports.refresh',{targets},result=>receivePorts(result,true));
  };
  function observation(key, record) {
    const fresh = live.get(key);
    const waiting = loading.has(key) && !fresh;
    const stale = record?.controlUsed && !record.powerObserved && (record.status==='ok' || record.status==='refresh_pending');
    const row = waiting ? null : fresh || (stale ? null : record);
    const incomplete = row && [false,'no','false'].includes(row.complete);
    let phase = 'observed';
    if (waiting || (matrixJob && incomplete)) phase = 'loading';
    else if (!fresh && stale) phase = 'stale';
    else if (!row) phase = 'pending';
    const power = phase==='observed' ? PowerMatrix.classify(row) : noPowerData;
    const failure = phase==='observed' ? PowerMatrix.readFailure(row) : '';
    const neighbor = phase==='observed' ? PowerMatrix.neighborIdentity(row) : null;
    return {row, phase, power, failure, neighbor, label:phaseLabels[phase] || power.label, reason:phaseReasons[phase] || power.reason};
  }
  function entries() {
    const complete = new Map(ui.rows.map(r=>[portKey(r),r]));
    const term = $('inventorySearch').value.trim().toLowerCase();
    const result = [];
    for (const g of inventory()) for (const port of g.interfaces) {
      const endpointKey=address(g), key=endpointKey+'|'+port;
      const record=complete.get(key), view=observation(key,record);
      if (term && ![endpointKey,g.label,port,view.row?.hostname,view.row?.ap_names,view.label,view.failure]
        .join(' ').toLowerCase().includes(term)) continue;
      result.push({g,port,key,record,view,row:view.row,state:view.power.state});
    }
    return result;
  }
  function visibleEntries(items) {
    if (failureView) return items.filter(item=>item.view.failure);
    if (!powerFilters.size) return items;
    return items.filter(item=>item.view.phase==='observed' && powerFilters.has(item.state));
  }
  function toggleFailures() {
    const scroller=$('monitorInventory'), search=$('inventorySearch');
    if (failureView) {
      const previous=failureView;
      failureView=null;
      page=previous.page;
      focused=previous.focused;
      search.value=previous.search;
      renderPorts();
      if (failureToggle.hidden) search.focus({preventScroll:true});
      scroller.scrollTop=previous.scroll;
    } else {
      failureView={page,focused,search:search.value,scroll:scroller.scrollTop};
      page=0;
      renderPorts();
    }
  }
  function groupCheckKeys(g) {
    return matchedGroups.get(address(g)) || [];
  }
  function details(item) {
    const area=$('matrixDetails');
    area.hidden = !item;
    if (!item) return;
    const {g,port,key,view,row}=item;
    const title = elem('strong',(row?.hostname || g.label || g.ip)+' · '+address(g)+' / '+port);
    const facts = [];
    const draw=view.power.watts ?? row?.watts;
    if (draw!=null && draw!=='') facts.push(draw+' W');
    if (row) facts.push('링크 ' + PowerMatrix.linkLabel(row.link_status));
    if (row?.link_observed_at) facts.push('링크 관측 시각 ' + dateText(row.link_observed_at));
    const neighborLabel=PowerMatrix.neighborDescription(view.neighbor);
    if (neighborLabel) facts.push(neighborLabel);
    if (row?.observed_at) facts.push('포트 관측 시각 ' + dateText(row.observed_at));
    const checkLabel = elem('label','', 'inline-check'), check = elem('input'); check.type='checkbox';
    check.dataset.matrixCheck = key; check.checked = ui.selected.has(key); check.disabled = ui.busy;
    check.onchange = ()=>{if(check.checked)ui.selected.add(key);else ui.selected.delete(key);renderPorts();};
    checkLabel.append(check,document.createTextNode('이 포트 체크'));
    area.replaceChildren(title,powerBadge(view),elem('span',facts.join(' · ')),checkLabel);
    if (view.failure) {
      const retry=button('이 포트만 재조회',()=>retryFailedPorts(key));
      retry.dataset.job='';
      retry.disabled=ui.busy || !ui.token;
      area.append(elem('small','실패 이유 · '+view.failure),retry);
    } else area.append(elem('small',view.reason));
  }
  function syncChecks() {
    const known=new Map(inventory().map(g=>[address(g),g]));
    const available=new Set(availableControlTargets().map(controlPortKey));
    for (const node of matrix.querySelectorAll('[data-check-group]')) {
      const g=known.get(node.dataset.checkGroup);
      if (!g) continue;
      const keys=groupCheckKeys(g).filter(key=>available.has(key));
      const count=keys.filter(key=>ui.selected.has(key)).length;
      node.checked=count>0 && count===keys.length; node.indeterminate=count>0&&!node.checked; node.disabled=ui.busy || !keys.length;
    }
    const check=matrix.querySelector('[data-matrix-check]');
    if (check) { check.checked=ui.selected.has(check.dataset.matrixCheck);check.disabled=ui.busy || !available.has(check.dataset.matrixCheck); }
  }
  function selectItem(item) {
    directCableTarget=false;
    focused=item.key;
    const selectable=!ui.busy && !item.view.failure && !['loading','stale'].includes(item.view.phase);
    if (selectable && availableControlTargets().some(target=>controlPortKey(target)===item.key)) {
      if (ui.selected.has(item.key)) ui.selected.delete(item.key); else ui.selected.add(item.key);
      $('controlMode').value='batch';
      $('controlMode').onchange();
    }
    renderPorts();
    matrix.querySelector(`.matrix-tile[data-key="${CSS.escape(item.key)}"]`)?.focus({preventScroll:true});
    if (item.view.failure) $('matrixDetails').scrollIntoView({block:'nearest'});
  }
  function showInventory(visible) {
    const viewer=monitor();
    if (visible && matrix.hidden) {
      powerFilters=new Set(viewer?.source().powerFilters || []);
      powerFiltersEdited=false;
    }
    const liveReady=!visible && ui.liveId && viewer?.source().liveId===ui.liveId;
    if (liveReady && powerFiltersEdited) {
      powerFiltersEdited=false;
      viewer.setPowerFilters([...powerFilters]);
    }
    if (liveReady && failureView) {
      const previous=failureView;
      failureView=null;
      const liveKey=key=>{
        const row=ui.rows.find(item=>portKey(item)===key);
        return row ? row.ip+':'+(row.ssh_port || 22)+'/'+row.interface : '';
      };
      viewer.showFailureFilter?.({search:previous.search,selectedKey:liveKey(previous.focused),
        activeSearch:$('inventorySearch').value,activeSelectedKey:liveKey(focused)});
    }
    if (matrix.hidden===visible) {
      matrix.hidden=!visible;
      if (visible) renderMatrix();
    }
  }
  function renderMatrix() {
    if (matrix.hidden) return;
    if (!matrixJob && !ui.rows.length && !live.size) failureView=null;
    const matches=entries(), visible=visibleEntries(matches), counts={};
    matchedGroups=new Map();
    for (const item of visible) {
      const addr=address(item.g);
      if (!matchedGroups.has(addr)) matchedGroups.set(addr,[]);
      matchedGroups.get(addr).push(item.key);
    }
    if (failureView && focused && !visible.some(item=>item.key===focused)) {
      focused='';
      updateControlTarget();
    }
    let unobserved=0;
    const candidates=failureView ? visible : matches;
    for (const item of candidates) {
      if (item.view.phase!=='observed') unobserved++;
      else counts[item.state]=(counts[item.state] || 0)+1;
    }
    for (const badge of legend.querySelectorAll('button[data-power-filter]')) {
      const state=badge.dataset.state;
      badge.lastElementChild.textContent=counts[state] || 0;
      badge.setAttribute('aria-pressed',String(!failureView && powerFilters.has(state)));
      badge.disabled=!!failureView;
    }
    const pages=Math.max(1,Math.ceil(visible.length/pageSize));
    page=Math.max(0,Math.min(page,pages-1));
    const start=page*pageSize, shown=visible.slice(start,start+pageSize);
    shownKeys=new Set(shown.map(item=>item.key));
    updateHiddenSelectedCount();
    const summary = ['표시 '+visible.length+'포트'];
    if (unobserved) summary.push('관측값 대기 '+unobserved);
    const failedCount=failureView ? visible.length : matches.filter(item=>item.view.failure).length;
    let message=summary.join(' · ');
    if (!visible.length) {
      message=failureView ? '현재 조건에서 조회에 실패한 포트가 없습니다.' : '현재 조건에 맞는 포트가 없습니다.';
      if (!failureView && !inventory().length) message='검증한 스위치 목록을 적용하거나 포트를 조회하세요.';
      else if (!failureView && !matches.length && !targetCount(groups) && groups.some(g=>g.all_ports)) {
        message=matrixJob ? '스위치에서 물리 포트 목록을 확인하고 있습니다.' : '조회하면 스위치에서 확인한 물리 포트가 표시됩니다.';
      }
    }
    countText.textContent=message;
    failureToggle.hidden=!failedCount && !failureView;
    failureToggle.setAttribute('aria-pressed',String(!!failureView));
    failureToggle.title=failureView ? '이전 화면으로 돌아가기' : '조회 실패 포트만 보기';
    failureToggle.textContent='조회 실패 '+failedCount.toLocaleString('ko-KR')+'개';
    $('matrixGroups').dataset.zoom=$('matrixSize').value;
    const nodes=[], grids=new Map();
    for(const item of shown) {
      const addr=address(item.g);
      if (!grids.has(addr)) {
        const group=elem('section',undefined,'matrix-group'), head=elem('div',undefined,'matrix-group-head');
        const label=elem('label',undefined,'inline-check'), check=elem('input');
        check.type='checkbox'; check.dataset.checkGroup=addr;
        check.setAttribute('aria-label',addr+(failureView?' 조회 실패 포트 전체 체크':' 검색 결과 포트 전체 체크'));
        check.onchange=()=>{
          const available=new Set(availableControlTargets().map(controlPortKey));
          for (const key of groupCheckKeys(item.g)) {
            if (check.checked && available.has(key)) ui.selected.add(key); else ui.selected.delete(key);
          }
          renderPorts();
        };
        const name=item.row?.hostname || item.g.label;
        label.append(check,document.createTextNode((name && name!==addr ? name+' · ' : '')+addr));
        const count=groupCheckKeys(item.g).length;
        head.append(label,elem('small',count+'포트'));group.append(head);
        const grid=elem('div',undefined,'matrix-tiles');group.append(grid);grids.set(addr,grid);nodes.push(group);
      }
      const tile=button('',()=>selectItem(item),'matrix-tile');
      const face=elem('span',undefined,'tile-face');
      const number=elem('span',item.port,'tile-port');
      face.append(number);
      const linkValue=item.view.phase==='observed' ? item.row?.link_status :
        item.view.phase==='stale' ? item.record?.link_status : null;
      const link=PowerMatrix.linkIndicator(linkValue);
      tile.dataset.key=item.key;tile.dataset.state=item.state;tile.dataset.phase=item.view.phase;
      tile.dataset.link=link.state;
      if (item.view.failure) {
        tile.dataset.readFailed='true';
        const mark=elem('span','!','port-failure-mark');
        mark.setAttribute('aria-hidden','true');
        number.append(mark);
      }
      const neighbor=item.view.neighbor;
      const caption=PowerMatrix.tileCaption(neighbor);
      if (caption) face.append(caption);
      tile.append(face,elem('span',link.label,'tile-link'));
      tile.setAttribute('aria-pressed',String(ui.selected.has(item.key)));
      tile.classList.toggle('focused-port',focused===item.key);
      const linkText=link.state==='unknown' ? '미조회' : PowerMatrix.linkLabel(linkValue);
      tile.title=[addr+' / '+item.port,item.view.failure?'조회 실패':'',item.view.label,item.view.power.watts!=null ? item.view.power.watts+' W' : '',PowerMatrix.neighborDescription(neighbor),'링크 '+linkText].filter(Boolean).join(' · ');
      tile.setAttribute('aria-label',tile.title); grids.get(addr).append(tile);
    }
    $('matrixGroups').replaceChildren(...nodes);
    $('matrixPager').hidden=pages<=1; $('matrixPage').textContent=(page+1)+' / '+pages;
    $('matrixPrev').disabled=page===0; $('matrixNext').disabled=page===pages-1;
    details(visible.find(x=>x.key===focused)); syncChecks();
  }
  $('matrixSize').onchange=()=>{$('matrixGroups').dataset.zoom=$('matrixSize').value;};
  $('inventorySearch').oninput=()=>{page=0;renderMatrix();updateMatrixSelectAll();};
  $('matrixPrev').onclick=()=>{page--;renderMatrix();};
  $('matrixNext').onclick=()=>{page++;renderMatrix();};
  function inventoryText(items) {
    const quote=value=>'"'+String(value ?? '').replaceAll('"','""')+'"';
    const lines=[['IP','ssh_port','interfaces','label'],...items.map(g=>[g.ip,g.ssh_port,g.all_ports ? '' : g.interfaces.join(','),g.label])];
    return lines.map(row=>row.map(quote).join('\t')).join('\n');
  }
  function replaceQueryInventory(items,text,preserveView=false) {
    directCableTarget=false;
    groups=stableGroups(items); registeredText=text;
    failureView=null; powerFilters.clear(); powerFiltersEdited=false; focused=''; page=0; live.clear(); loading.clear(); $('inventorySearch').value='';
    clearPorts(preserveView);
    $('controlMode').value=groups.length?'batch':'single'; $('controlMode').onchange();
    updateTargetCount();
  }
  function showStartingMatrix(items,text) {
    monitor()?.resetFailureFilter?.();
    replaceQueryInventory(items,text,true);
    setQueryWorkspace(true);
  }
  function acceptQuery(items) {
    if (!fullQuery || !items?.length) return;
    if (fullQuery.accepted) {
      if (!matchesInventory(items)) {
        groups=stableGroups(items);
        updateTargetCount();
      }
      return;
    }
    fullQuery.accepted=true;
    showStartingMatrix(items,fullQuery.text);
  }
  async function recoverQuery() {
    if (!fullQuery) return;
    let workspace;
    try { workspace=await api('/api/workspace'); } catch { /* An interrupted request may have replaced the live inventory. */ }
    if (!Array.isArray(workspace?.groups)) {
      replaceQueryInventory([], '');
      return;
    }
    const query=workspace.query;
    const items=stableGroups(workspace.groups);
    const same=matchesInventory(items);
    if (same && !query && !ui.rows.length && !ui.liveId) return;
    if (same && ui.liveId && query && !fullQuery.accepted) {
      receivePorts(query,true);
      return;
    }
    const text=workspace.targetText ?? (same ? registeredText : items.length ? inventoryText(items) : '');
    replaceQueryInventory(items,text);
    if (query) receivePorts(query);
  }
  function onJob(action,state,body,keepMatrix=false) {
    if (action==='ports.query' || action==='ports.refresh') {
      matrixJob=state==='running' ? action : '';
      if (state==='running') {
        progressGroups=null;live.clear();
        if (action==='ports.query') {
          fullQuery={text:body.targets,accepted:false};
        } else if (body?.targets) {
          // Each partial refresh names its registered endpoints and exact port lists.
          loading=new Set(body.targets.split('\n').flatMap(line=>{
            const [addr,ports]=line.split(' ');
            return ports.split(',').map(port=>addr+'|'+port);
          }));
          ui.rows=ui.rows.map(row=>{
            if (!loading.has(portKey(row))) return row;
            if (keepMatrix) return {...row,controlUsed:true,refreshInvalidated:true,link_status:'',link_observed_at:''};
            const {ip,ssh_port,interface:port,label}=row;
            return {ip,ssh_port,interface:port,label,status:'refresh_pending',controlUsed:true,refreshInvalidated:true};
          });
          if (!keepMatrix) expireLiveMatrix(true);
        }
      } else {
        fullQuery=null;
        live.clear();loading.clear();progressGroups=null;
        if ($('operation').value!=='disable') {
          for (const key of invalidatedKeys()) ui.selected.delete(key);
        }
      }
      updateQuerySummary();
      renderPorts();
    } else if (state==='idle') syncChecks();
  }
  function onProgress(action,progress) {
    if (!matrixJob || action!==matrixJob || !progress) return;
    if(Array.isArray(progress.targets) && progress.targets!==progressGroups) {
      progressGroups=progress.targets;
      const requested=stableGroups(progress.targets);
      if (action==='ports.query') acceptQuery(requested);
      loading=new Set(requested.flatMap(g=>g.interfaces.map(p=>address(g)+'|'+p)));
    }
    showDiscoveryErrors(progress.discoveryErrors);
    for(const row of progress.observations || []) live.set(portKey(row),row);
    renderPorts();
  }
  function resumeQuery(progress) {
    if (fullQuery) return;
    const text=progress.targetText ?? inventoryText(progress.targets);
    if (!raw.value) raw.value=text;
    onJob('ports.query','running',{targets:text});
  }
  function endResumedQuery() {
    if (fullQuery) onJob('ports.query','idle');
  }
  function restoreInventory(items,targetsText=inventoryText(items),preserveDraft=false) {
    if(!Array.isArray(items) || !items.length) return;
    directCableTarget=false;
    targetsText=targetsText.replace(/\r\n?/g,'\n');
    const editing=preserveDraft && raw.value.trim() && raw.value!==registeredText && raw.value.trim()!==targetsText.trim();
    groups=stableGroups(items); registeredText=targetsText;
    if (!editing) raw.value=targetsText;
    $('controlMode').value='batch'; $('controlMode').onchange();
    updateTargetCount();
    updatePortActions(); renderMatrix();
  }
  function matchesInventory(items,scopeOnly=false) {
    if (!Array.isArray(items) || items.length!==groups.length) return false;
    return stableGroups(items).every((item,i)=>{
      const group=groups[i];
      return address(item)===address(group) && item.label===group.label && !!item.all_ports===!!group.all_ports &&
        ((scopeOnly && item.all_ports) || item.interfaces.join(',')===group.interfaces.join(','));
    });
  }
  function reconcileRows(rows,refreshed=loading) {
    // A partial refresh also contains earlier observations from other ports.
    const invalid = new Map(ui.rows.filter(row=>row.refreshInvalidated).map(row=>[portKey(row),row]));
    if (!invalid.size) return rows;
    const reconciled=rows.map(row=>{
      const key=portKey(row), previous=invalid.get(key);
      invalid.delete(key);
      return previous && !refreshed.has(key) && row.status==='ok' ? previous : row;
    });
    // A monitoring cycle can discover that a previously invalidated port no longer exists.
    if (invalid.size) {
      for (const group of inventory()) for (const port of group.interfaces) {
        const retained=invalid.get(address(group)+'|'+port);
        if (retained) reconciled.push(retained);
      }
    }
    return reconciled;
  }
  window.portTools={showStartingMatrix,resumeQuery,endResumedQuery,targetDraftVersion,openTargetEditor,reconcileRows,renderMatrix,showInventory,currentKey,acceptQuery,recoverQuery,onJob,onProgress,availableControlTargets,scopeKeys,hiddenSelectedCount,unobservedTargets,cableScope,
    hasInventory:()=>groups.length>0 || ui.rows.length>0,awaitingDiscovery:()=>groups.some(g=>g.all_ports && !g.interfaces.length),
    selectedRefreshCount:()=>targetCount(selectedRefreshGroups()),restoreInventory,matchesInventory,clearSshObservations};
  updatePortActions();sync();
})();
