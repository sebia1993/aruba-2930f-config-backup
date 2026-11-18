/* Register explicit SSH targets and project the same port states as the desktop matrix. */
(() => {
  const raw = $('queryTargets');
  const jobText = {'targets.preview':'대상 목록 검증', 'targets.apply':'대상 목록 적용'};
  Object.assign(jobNames,jobText);
  Object.assign(labels,{hostname_source:'호스트 이름 수집 출처',sku:'스위치 SKU',software_version:'스위치 소프트웨어',
    reference_id:'PoE 판정 기준',reference_url:'판정 자료 주소'});
  let groups = [], preview = null, page = 0, focused = '', live = new Map(), loading = new Set();
  let matrixJob = '', attention = false, registeredText = '', progressGroups = null;
  const pageSize = 192;
  const stateText = {normal:'링크 Up',attention:'링크 Down',warning:'주의',off:'설정 OFF',unknown:'미수집',pending:'조회 대기',loading:'조회 중'};
  const marked = new Set(['warning','attention']);
  const cmp = new Intl.Collator('en',{numeric:true}).compare;
  const form = $('queryForm');
  const matrix = document.createElement('article');
  matrix.className = 'panel port-matrix-panel';
  matrix.innerHTML = '<header><h2>등록 포트 매트릭스</h2><div class="tools"><button type="button" id="matrixPriority" aria-pressed="false">우선 확인</button><label>크기 <select id="matrixSize"><option value="small">작게</option><option value="medium" selected>보통</option><option value="large">크게</option></select></label></div></header><div class="panelbody"><p id="matrixCounts" class="hint"></p><div id="matrixGroups"></div><div id="matrixPager" class="bar" hidden><button type="button" id="matrixPrev">이전</button><span id="matrixPage"></span><button type="button" id="matrixNext">다음</button></div><div id="matrixDetails" hidden></div></div>';
  form.closest('article').after(matrix);

  const tools = document.createElement('div');
  tools.className = 'bar target-tools';
  tools.innerHTML = '<button type="button" data-job id="targetRows">스위치 행 편집</button><button type="button" data-job id="targetPaste">표 붙여넣기</button><button type="button" data-job id="targetVerify">입력한 대상 검증</button><span class="hint" id="targetCount"></span>';
  raw.closest('label').after(tools);

  const editor = document.createElement('dialog');
  editor.id = 'targetEditor';
  editor.innerHTML = '<div class="dialog-head"><h2 id="targetEditorTitle">스위치 행 편집</h2><button type="button" id="targetClose">닫기</button></div><div class="target-body"><section id="targetRowsPane"><div class="bar"><button id="addTargetRow" type="button">스위치 추가</button><label class="inline-check"><input id="advancedTargets" type="checkbox"> SSH 포트 · 별칭</label></div><div class="tablebox"><table><thead><tr><th>스위치 IPv4</th><th>인터페이스 목록</th><th class="advanced">SSH 포트</th><th class="advanced">별칭</th><th>행</th></tr></thead><tbody id="editTargetRows"></tbody></table></div></section><section id="targetPastePane"><div class="bar"><label>적용 방식<select id="pasteMode"><option value="replace">기존 목록 교체</option><option value="append">기존 목록에 추가</option></select></label><button id="readClipboard" type="button">클립보드에서 가져오기</button></div><label>스위치·포트 목록<textarea id="pasteTargets" rows="6" placeholder="엑셀에서 복사한 IP와 포트 열을 붙여넣으세요."></textarea></label></section><div class="bar"><button id="verifyEditor" type="button" data-job>미리보기</button><button id="acceptTargets" type="button" class="primary" disabled>검증된 대상 적용</button></div><p id="targetMessage" role="status"></p><div class="tablebox" id="targetPreview"></div></div>';
  document.body.append(editor);
  const rowPane = $('targetRowsPane'), pastePane = $('targetPastePane');
  let draftVersion = 0;

  function targetCount(items) { return items.reduce((n,g)=>n+g.interfaces.length,0); }
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
    for (const [field,value,label] of [['ip',group?.ip || '','스위치 IPv4'],['interfaces',group?.interfaces.join(',') || '','인터페이스 목록'],['ssh_port',group?.ssh_port || 22,'SSH 포트'],['label',group?.label || '','별칭']]) {
      const cell = row.insertCell();
      if (['ssh_port','label'].includes(field)) cell.className = 'advanced';
      const input = elem('input');
      input.value = value;
      input.dataset.field=field;
      input.setAttribute('aria-label',label);
      if (field === 'label') input.maxLength = 80;
      if (field === 'ssh_port') { input.type='number'; input.min=1; input.max=65535; }
      cell.append(input);
    }
    row.insertCell().append(button('삭제',()=>{row.remove();editorDirty();}));
    return row;
  }
  function previewPayload() {
    if (!pastePane.hidden) return {targets:$('pasteTargets').value, mode:$('pasteMode').value,existing:raw.value};
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
    $('targetMessage').textContent = result.groups.length + '대 · ' + result.port_count + '개 포트';
    $('targetPreview').replaceChildren(simpleTable([['스위치 IPv4','ip'],['SSH 포트','ssh_port'],['포트',g=>g.interfaces.join(', ')],['별칭','label']],result.groups,'등록 목록을 비웁니다.'));
    $('acceptTargets').disabled = false;
  }
  async function openEditor(mode) {
    editorDirty();
    $('editTargetRows').replaceChildren();
    const rows = mode === 'rows';
    $('targetEditorTitle').textContent = rows ? '스위치 행 편집' : '표 붙여넣기 · 대상 검증';
    rowPane.hidden = !rows; pastePane.hidden = rows;
    $('pasteTargets').value = mode === 'verify' ? raw.value : '';
    $('pasteMode').value = 'replace';
    editor.showModal();
    if (rows) {
      const populate = items => {
        $('advancedTargets').checked = items.some(g=>Number(g.ssh_port)!==22 || g.label);
        rowPane.classList.toggle('show-advanced',$('advancedTargets').checked);
        if (items.length) items.forEach(addRow); else addRow();
      };
      if (raw.value.trim() && raw.value !== registeredText) {
        await requestPreview({targets:raw.value},r=>populate(r.groups));
      } else populate(groups);
    } else if (mode === 'verify' && raw.value.trim()) {
      await requestPreview(previewPayload(),showPreview);
    } else $('pasteTargets').focus();
  }
  $('targetRows').onclick = ()=>openEditor('rows');
  $('targetPaste').onclick = ()=>openEditor('paste');
  $('targetVerify').onclick = ()=>openEditor('verify');
  $('targetClose').onclick = ()=>editor.close();
  editor.addEventListener('close',editorDirty);
  rowPane.addEventListener('input', e=>{if (e.target.id !== 'advancedTargets') editorDirty();});
  pastePane.addEventListener('input',editorDirty);
  $('advancedTargets').onchange = e=>rowPane.classList.toggle('show-advanced',e.target.checked);
  $('addTargetRow').onclick = ()=>{const tr=addRow(); if (tr) {editorDirty();tr.querySelector('input').focus();}};
  $('readClipboard').onclick = async ()=>{
    try { $('pasteTargets').value = await navigator.clipboard.readText(); editorDirty(); }
    catch { $('targetMessage').textContent = '입력란을 클릭하고 Ctrl+V로 붙여넣으세요.'; }
    $('pasteTargets').focus();
  };
  $('verifyEditor').onclick = ()=>{editorDirty();requestPreview(previewPayload(),showPreview);};
  $('acceptTargets').onclick = async ()=>{
    if (!preview) return;
    const accepted = preview;
    $('acceptTargets').disabled = true;
    await run('targets.apply',accepted.groups.length ? {targets:accepted.targets} : {rows:[]},r=>{
      groups = stableGroups(r.groups); registeredText = raw.value = r.targets;
      focused = ''; page = 0; live.clear(); loading.clear(); attention = false; clearPorts();
      $('targetCount').textContent = groups.length + '대 · ' + targetCount(groups) + '포트 등록';
      editor.close(); renderMatrix();
    });
    if (editor.open && preview === accepted) {
      $('targetMessage').textContent = $('statusText').textContent; $('acceptTargets').disabled = false;
    }
  };
  raw.addEventListener('input',()=>{
    if (raw.value === registeredText) return;
    groups=[]; registeredText=''; live.clear(); loading.clear(); focused=''; attention=false; page=0;
    clearPorts(); $('targetCount').textContent = '입력한 목록은 검증 또는 조회 시 적용됩니다.';
  });

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
  function checkedTargets() {
    const checked = [];
    for (const g of inventory()) for (const port of g.interfaces) {
      if (ui.selected.has(address(g)+'|'+port)) checked.push({ip:g.ip,sshPort:Number(g.ssh_port || 22),port});
    }
    return checked;
  }
  function currentAddress() {
    const chosen=focused || ui.detail || '';
    return $('switchFilter').value || chosen.split('|')[0];
  }
  function scopeKeys() {
    const scope=$('selectionScope').value, device=currentAddress();
    if(scope==='switch'&&!device){notify('스위치 필터 또는 포트 상세에서 현재 장비를 선택하세요.',true);return null;}
    if(scope==='filtered')return entries().map(item=>item.key);
    return inventory().filter(g=>scope!=='switch'||address(g)===device)
      .flatMap(g=>g.interfaces.map(p=>address(g)+'|'+p));
  }
  function refreshTargets(bySwitch) {
    const device=currentAddress(), current=focused || ui.detail;
    const lines=[];
    for(const g of inventory()) {
      const addr=address(g);
      const ports=g.interfaces.filter(p=>bySwitch ? device===addr :
        (ui.selected.size ? ui.selected.has(addr+'|'+p) : current===addr+'|'+p));
      if (ports.length) lines.push(addr+' '+ports.join(','));
    }
    if (!lines.length) notify(bySwitch?'스위치 필터 또는 포트 상세에서 장비를 선택하세요.':'재조회할 포트를 체크하거나 상세 보기를 여세요.',true);
    return lines.join('\n') || null;
  }
  function entries() {
    const complete = new Map(ui.rows.map(r=>[portKey(r),r]));
    const filterIndex = new Set(filteredRows().map(portKey));
    const filtersActive = $('portFilter').value || $('portSearch').value.trim() || $('switchFilter').value;
    const term=$('portSearch').value.trim().toLowerCase();
    const result = [];
    for (const g of inventory()) for (const port of g.interfaces) {
      const endpointKey=address(g), key=endpointKey+'|'+port;
      const record=complete.get(key), row=live.get(key) || record;
      if (filtersActive && record && !filterIndex.has(key)) continue;
      if (filtersActive && !record && (($('switchFilter').value && $('switchFilter').value!==endpointKey) ||
          $('portFilter').value || (term && ![endpointKey,g.label,port,row?.hostname,row?.ap_names].join(' ').toLowerCase().includes(term)))) continue;
      const state = loading.has(key) && !live.has(key) ? 'loading' : row?.controlUsed ? 'unknown' : row ? row.matrix_state || 'unknown' : 'pending';
      result.push({g,port,key,row,state,record});
    }
    return result;
  }
  function details(item) {
    const area=$('matrixDetails');
    area.hidden = !item;
    if (!item) return;
    const {g,port,key,state,row}=item;
    const title = elem('strong',(row?.hostname || g.label || g.ip)+' · '+address(g)+' / '+port);
    const facts = [row?.controlUsed ? '제어 후 재조회 필요 · 이전 관측' : stateText[state]];
    if (row?.watts != null && row.watts !== '') facts.push(row.watts+' W');
    if (row?.link_status) facts.push(row.link_status);
    if (row?.ap_names) facts.push(row.ap_names);
    if (row?.observed_at) facts.push(row.observed_at);
    const checkLabel = elem('label','', 'inline-check'), check = elem('input'); check.type='checkbox';
    check.dataset.matrixCheck = key; check.checked = ui.selected.has(key); check.disabled = ui.busy;
    check.onchange = ()=>{if(check.checked)ui.selected.add(key);else ui.selected.delete(key);renderPorts();};
    checkLabel.append(check,document.createTextNode('이 포트 체크'));
    area.replaceChildren(title,elem('span',facts.join(' · ')),checkLabel);
    if (row?.matrix_note) area.append(elem('small',row.matrix_note));
  }
  function syncChecks() {
    const known=new Map(inventory().map(g=>[address(g),g]));
    for (const node of matrix.querySelectorAll('[data-check-group]')) {
      const g=known.get(node.dataset.checkGroup);
      if (!g) continue;
      const count=g.interfaces.filter(p=>ui.selected.has(address(g)+'|'+p)).length;
      node.checked=count===g.interfaces.length; node.indeterminate=count>0&&!node.checked; node.disabled=ui.busy;
    }
    const check=matrix.querySelector('[data-matrix-check]');
    if (check) { check.checked=ui.selected.has(check.dataset.matrixCheck);check.disabled=ui.busy; }
  }
  function selectItem(item) {
    focused=item.key;
    if (item.record) {
      const filtered=filteredRows(), index=filtered.findIndex(r=>portKey(r)===focused);
      if(index>=0) portPage=Math.floor(index/portPageSize);
      ui.detail=focused;renderPorts();showDetail(item.record);
    } else renderMatrix();
  }
  function renderMatrix() {
    const items=entries(), counts={};
    for(const item of items) counts[item.state]=(counts[item.state]||0)+1;
    const visible=attention ? items.filter(x=>marked.has(x.state)) : items;
    const pages=Math.max(1,Math.ceil(visible.length/pageSize));
    page=Math.max(0,Math.min(page,pages-1));
    const start=page*pageSize, shown=visible.slice(start,start+pageSize);
    $('matrixCounts').textContent = items.length ? '표시 '+items.length+'포트 · '+Object.keys(stateText).filter(k=>counts[k]).map(k=>stateText[k]+' '+counts[k]).join(' · ') : '검증한 스위치 목록을 적용하거나 포트를 조회하세요.';
    $('matrixPriority').setAttribute('aria-pressed',String(attention));
    $('matrixGroups').dataset.zoom=$('matrixSize').value;
    const nodes=[], grids=new Map();
    for(const item of shown) {
      const addr=address(item.g);
      if (!grids.has(addr)) {
        const group=elem('section',undefined,'matrix-group'), head=elem('div',undefined,'matrix-group-head');
        const label=elem('label',undefined,'inline-check'), check=elem('input');
        check.type='checkbox'; check.dataset.checkGroup=addr;
        check.setAttribute('aria-label',addr+' 등록 포트 전체 체크');
        check.onchange=()=>{
          for(const p of item.g.interfaces){const key=addr+'|'+p;if(check.checked)ui.selected.add(key);else ui.selected.delete(key);}
          renderPorts();
        };
        const name=item.row?.hostname || item.g.label;
        label.append(check,document.createTextNode((name && name!==addr ? name+' · ' : '')+addr));
        head.append(label,elem('small',item.g.interfaces.length+'포트'));group.append(head);
        const grid=elem('div',undefined,'matrix-tiles');group.append(grid);grids.set(addr,grid);nodes.push(group);
      }
      const tile=button(item.port,()=>selectItem(item),'matrix-tile');
      tile.dataset.key=item.key;tile.dataset.state=item.state;
      tile.setAttribute('aria-pressed',String(focused===item.key));
      tile.title=addr+' / '+item.port+' · '+stateText[item.state];
      tile.setAttribute('aria-label',tile.title); grids.get(addr).append(tile);
    }
    if (attention && !shown.length) nodes.push(elem('p','현재 표시 범위에 우선 점검할 포트가 없습니다.','hint'));
    $('matrixGroups').replaceChildren(...nodes);
    $('matrixPager').hidden=pages<=1; $('matrixPage').textContent=(page+1)+' / '+pages;
    $('matrixPrev').disabled=page===0; $('matrixNext').disabled=page===pages-1;
    details(items.find(x=>x.key===focused)); syncChecks();
  }
  $('matrixSize').onchange=()=>{$('matrixGroups').dataset.zoom=$('matrixSize').value;};
  $('matrixPriority').onclick=()=>{attention=!attention;page=0;const first=entries().find(x=>marked.has(x.state));if (attention&&first) selectItem(first);else renderMatrix();};
  $('matrixPrev').onclick=()=>{page--;renderMatrix();};
  $('matrixNext').onclick=()=>{page++;renderMatrix();};
  function selectDetail(row) {
    focused=portKey(row);
    const items=entries();
    if(attention && !marked.has(items.find(x=>x.key===focused)?.state)) attention=false;
    const list=attention ? items.filter(x=>marked.has(x.state)) : items;
    const pos=list.findIndex(x=>x.key===focused);
    if (pos>=0) page=Math.floor(pos/pageSize);
    renderMatrix();
  }
  function onJob(action,state) {
    if (action==='ports.query' || action==='ports.refresh') {
      matrixJob=state==='running' ? action : '';
      if (state==='running') {
        progressGroups=null;live.clear();
        if (action==='ports.query') {
          loading=new Set(inventory().flatMap(g=>g.interfaces.map(p=>address(g)+'|'+p)));
        }
      } else {live.clear();loading.clear();progressGroups=null;}
      renderMatrix();
    } else if (state==='idle') syncChecks();
  }
  function onProgress(action,progress) {
    if (!matrixJob || action!==matrixJob || !progress) return;
    if(Array.isArray(progress.targets) && progress.targets!==progressGroups) {
      progressGroups=progress.targets;
      const requested=stableGroups(progress.targets);
      if(action==='ports.query') {groups=requested;registeredText=raw.value;}
      loading=new Set(requested.flatMap(g=>g.interfaces.map(p=>address(g)+'|'+p)));
    }
    for(const row of progress.observations || []) live.set(portKey(row),row);
    renderMatrix();
  }
  function restoreInventory(items,targetsText) {
    if(!Array.isArray(items) || !items.length) return;
    groups=stableGroups(items); registeredText=raw.value=targetsText;
    $('targetCount').textContent=groups.length+'대 · '+targetCount(groups)+'포트 등록';
    renderMatrix();
  }
  window.portTools={renderMatrix,selectDetail,syncChecks,onJob,onProgress,checkedTargets,scopeKeys,refreshTargets,restoreInventory};
  renderMatrix();sync();
})();
