'use strict';

(() => {
  const fields = {hostname:'장비 Hostname', swIp:'SW IP', gwIp:'GW IP', bbIp:'BB', nmsIp:'NMS server'};
  const roleNames = {gwIp:'GW', bbIp:'BB', nmsIp:'NMS'};
  const mode = $('pingInputMode'), form = $('pingForm'), panel = $('pingInventory');
  const status = $('pingInventoryStatus'), roles = [...panel.querySelectorAll('[data-inventory-role]')];
  const pageSize = 50;
  const textInput = $('pingInventoryText');
  let preview = null, revision = 0, page = 0;
  let explicitSelection = false, mappedSelection = null;
  const selected = new Set();
  const notify = () => form.dispatchEvent(new Event('input', {bubbles:true}));
  const active = () => mode.value === 'inventory';
  const selectedRoles = () => roles.filter(box => box.checked).map(box => box.dataset.inventoryRole);
  const uniqueDestinations = (row, keys) => [...new Set(keys.map(key => row[key]).filter(Boolean))];

  function targets() {
    const keys = selectedRoles();
    if (!preview || preview.errors.length) throw new Error('열 연결과 입력 오류를 확인한 뒤 표를 다시 읽으세요.');
    if (!keys.length) throw new Error('측정할 목적지를 하나 이상 선택하세요.');
    if (!selected.size || selected.size > 300) throw new Error('측정할 장비를 1~300대 선택하세요.');
    const rows = preview.rows.filter(row => selected.has(row.rowNumber));
    if (!rows.some(row => uniqueDestinations(row, keys).length)) throw new Error('선택한 장비와 목적지에 측정할 IPv4 주소가 없습니다.');
    return {
      inventoryRows:rows.map(row => Object.fromEntries(Object.keys(fields).map(key => [key, row[key]]))),
      inventoryRoles:keys
    };
  }
  function reportSelection() {
    if (!preview) return;
    if (preview.errors.length) { status.textContent = preview.errors.join(' · '); return; }
    const keys = selectedRoles();
    const pairs = preview.rows.reduce((total, row) =>
      total + (selected.has(row.rowNumber) ? uniqueDestinations(row, keys).length : 0), 0);
    const invalid = preview.rows.filter(row => row.errors.length).length;
    status.textContent = '입력 ' + preview.rows.length + '행 · 오류 ' + invalid + '행 · 선택 ' +
      selected.size + '대 · 측정 ' + pairs + '조합' +
      (preview.rows.length > 300 ? ' · 한 번에 300대까지 선택하세요.' : '');
  }
  function rowCheckbox(row) {
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.setAttribute('aria-label', row.rowNumber + '행 ' + (row.hostname || row.swIp) + ' 선택');
    box.checked = selected.has(row.rowNumber);
    box.disabled = !!row.errors.length || !!preview.errors.length;
    box.addEventListener('change', () => {
      explicitSelection = true;
      if (box.checked) selected.add(row.rowNumber);
      else selected.delete(row.rowNumber);
      reportSelection();
    });
    return box;
  }
  function paintRows() {
    if (!preview) return;
    const rows = preview.rows;
    const start = page * pageSize;
    const columns = [['선택', rowCheckbox], ['행', 'rowNumber'],
      ...Object.entries(fields).map(([key, name]) => [name, row => row[key] || (key === 'hostname' ? '—' : '대상 없음')]),
      ['입력 확인', row => row.errors.join(' · ') || '확인']];
    const holder = $('pingInventoryRows');
    holder.replaceChildren(simpleTable(columns, rows.slice(start, start + pageSize)));
    if (rows.length > pageSize) {
      const bar = elem('div', undefined, 'bar');
      for (const [delta, label] of [[-1, '이전 행'], [1, '다음 행']]) {
        const btn = elem('button', label);
        btn.type = 'button';
        btn.disabled = delta < 0 ? !page : start + pageSize >= rows.length;
        btn.onclick = () => { page += delta; paintRows(); };
        bar.append(btn);
      }
      bar.append(elem('span', (start + 1) + '–' + Math.min(start + pageSize, rows.length) + ' / ' + rows.length));
      holder.append(bar);
    }
    reportSelection();
  }
  function chooseValid(previous) {
    selected.clear();
    const valid = preview.rows.filter(row => !row.errors.length);
    $('pingInventoryAll').disabled = valid.length > 300 || !!preview.errors.length;
    if (!preview.errors.length) {
      for (const row of valid) {
        if (previous ? previous.get(row.rowNumber) === row.swIp : valid.length <= 300) selected.add(row.rowNumber);
      }
    }
    paintRows();
    if (valid.length > 300) status.textContent += ' 목록에서 실행할 장비를 선택하세요.';
  }
  $('pingInventoryAll').onclick = () => { explicitSelection = true; chooseValid(); notify(); };
  $('pingInventoryNone').onclick = () => { explicitSelection = true; selected.clear(); paintRows(); notify(); };
  roles.forEach(box => box.addEventListener('change', reportSelection));

  function clearReview() {
    preview = null; page = 0; selected.clear();
    $('pingInventoryReview').hidden = true;
    $('pingInventoryRows').replaceChildren();
    notify();
  }
  function resetInput() {
    revision++;
    mappedSelection = null; explicitSelection = false;
    clearReview();
    $('pingInventoryMap').hidden = true;
    status.textContent = '';
  }
  function syncMode() {
    panel.hidden = !active();
    $('pingListFields').hidden = active();
    $('pingTargets').disabled = $('pingDestinations').disabled = active();
    const destOnly = $('pingProfileScope').querySelector('option[value="destinations"]');
    destOnly.disabled = active();
    if (active() && $('pingProfileScope').value !== 'plan') {
      $('pingProfileScope').value = 'plan';
      $('pingProfileScope').dispatchEvent(new Event('change', {bubbles:true}));
    }
  }
  mode.addEventListener('change', syncMode);
  syncMode();
  textInput.addEventListener('input', () => {
    resetInput();
    status.textContent = '붙여넣은 표 읽기를 눌러 입력을 확인하세요.';
  });
  function rememberMappedSelection() {
    if (preview && !preview.errors.length && explicitSelection) {
      mappedSelection = new Map(preview.rows.filter(row => selected.has(row.rowNumber))
        .map(row => [row.rowNumber, row.swIp]));
    }
  }
  function mappingControls(result) {
    const holder = $('pingInventoryMapping');
    holder.replaceChildren();
    for (const [key, title] of Object.entries(fields)) {
      const label = elem('label', title), select = elem('select');
      select.dataset.inventoryField = key;
      select.add(new Option('연결 안 함', ''));
      result.headers.forEach((name, index) => select.add(new Option((index + 1) + '열 · ' + (name || '(빈 제목)'), String(index))));
      select.value = result.mapping[key] == null ? '' : String(result.mapping[key]);
      select.addEventListener('change', () => {
        rememberMappedSelection();
        clearReview(); status.textContent = '열 연결 적용을 눌러 변경한 열로 검토하세요.';
      });
      label.append(select); holder.append(label);
    }
    $('pingInventoryMap').hidden = false;
    $('pingInventoryMap').open = !!result.errors.length;
  }
  async function inspect(mapping) {
    const version = ++revision;
    if (!mapping) { mappedSelection = null; explicitSelection = false; }
    else rememberMappedSelection();
    clearReview();
    $('pingInventoryMap').hidden = true;
    status.textContent = '표를 읽는 중입니다.';
    try {
      const data = await api('/api/ping/inventory/preview', {text:textInput.value, mapping}, undefined, 20000);
      if (revision !== version) return;
      preview = data;
      mappingControls(data);
      $('pingInventoryReview').hidden = !data.rows.length;
      chooseValid(mappedSelection);
      notify();
    } catch (err) { if (revision === version) status.textContent = err.message; }
  }
  $('pingInventoryRead').onclick = () => {
    const text = textInput.value;
    resetInput();
    if (text.length > 3 * 1024 * 1024 || new Blob([text]).size > 3 * 1024 * 1024) {
      status.textContent = '3 MiB 이하의 표를 붙여넣으세요.';
    }
    else if (text.trim()) inspect();
    else status.textContent = '열 이름과 장비 행을 함께 붙여넣으세요.';
  };
  $('pingInventoryApply').onclick = () => {
    const map = {};
    for (const select of $('pingInventoryMapping').querySelectorAll('select')) {
      map[select.dataset.inventoryField] = select.value === '' ? null : Number(select.value);
    }
    inspect(map);
  };
  function applyProfile(plan) {
    mode.value = plan?.inventoryRows ? 'inventory' : 'list';
    syncMode();
    if (!plan?.inventoryRows) return;
    resetInput(); textInput.value = '';
    preview = {errors:[], rows:plan.inventoryRows.map((row, index) => ({...row, rowNumber:index + 2, errors:[]}))};
    roles.forEach(box => { box.checked = plan.inventoryRoles.includes(box.dataset.inventoryRole); });
    $('pingInventoryReview').hidden = false;
    chooseValid(); notify();
  }
  window.pingInventory = {active, targets, applyProfile, uniqueDestinations, roleNames};
})();
