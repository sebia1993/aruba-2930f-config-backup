'use strict';

(() => {
  const fields = {hostname:'장비 Hostname', swIp:'SW IP', gwIp:'GW IP', bbIp:'BB', nmsIp:'NMS server'};
  const roleNames = {gwIp:'GW', bbIp:'BB', nmsIp:'NMS'};
  const mode = $('pingInputMode'), form = $('pingForm'), panel = $('pingInventory');
  const status = $('pingInventoryStatus'), roles = [...panel.querySelectorAll('[data-inventory-role]')];
  const pageSize = 50;
  const textInput = $('pingInventoryText');
  let preview = null, revision = 0, page = 0;
  let readTimer, waitingForToken = false;
  let explicitSelection = false, mappedSelection = null;
  const selected = new Set();
  const active = () => mode.value === 'inventory';
  const notify = () => {
    if (active()) form.dispatchEvent(new Event('input', {bubbles:true}));
  };
  const selectedRoles = () => roles.filter(box => box.checked).map(box => box.dataset.inventoryRole);
  const rolesForRow = (row, keys) => keys.filter(key => row[key]);
  function hasTargets() {
    if (!preview || preview.errors.length || !selected.size || selected.size > 300) return false;
    const keys = selectedRoles();
    return preview.rows.some(row => selected.has(row.rowNumber) && keys.some(key => row[key]));
  }
  function minimumConcurrency() {
    if (!active() || !preview) return 1;
    const roles = selectedRoles();
    return preview.rows.reduce((minimum, row) => selected.has(row.rowNumber)
      ? Math.max(minimum, rolesForRow(row, roles).length) : minimum, 1);
  }

  function targets() {
    const keys = selectedRoles();
    if (!preview || preview.errors.length) throw new Error('표의 열 제목과 입력 상태를 확인하세요.');
    if (!keys.length) throw new Error('측정할 목적지를 하나 이상 선택하세요.');
    if (!selected.size || selected.size > 300) throw new Error('측정할 장비를 1~300대 선택하세요.');
    const rows = preview.rows.filter(row => selected.has(row.rowNumber));
    if (!rows.some(row => rolesForRow(row, keys).length)) throw new Error('선택한 장비와 목적지에 측정할 IPv4 주소가 없습니다.');
    return {
      inventoryRows:rows.map(row => Object.fromEntries(Object.keys(fields).map(key => [key, row[key]]))),
      inventoryRoles:keys,
      inventoryRoleSessions:true
    };
  }
  function reportSelection() {
    if (!preview) return;
    if (preview.errors.length) { status.textContent = preview.errors.join(' · '); return; }
    const keys = selectedRoles();
    const pairs = preview.rows.reduce((total, row) =>
      total + (selected.has(row.rowNumber) ? rolesForRow(row, keys).length : 0), 0);
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
    clearTimeout(readTimer);
    waitingForToken = false;
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
  textInput.addEventListener('input', event => {
    resetInput();
    const text = textInput.value;
    if (!text.trim()) return;
    if (text.length > 3 * 1024 * 1024 || new Blob([text]).size > 3 * 1024 * 1024) {
      status.textContent = '3 MiB 이하의 표를 붙여넣으세요.';
      return;
    }
    status.textContent = '표를 읽는 중입니다.';
    if (event.inputType === 'insertFromPaste') inspect();
    else readTimer = setTimeout(() => inspect(), 180);
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
        const mapping = {};
        for (const field of holder.querySelectorAll('select')) {
          mapping[field.dataset.inventoryField] = field.value === '' ? null : Number(field.value);
        }
        inspect(mapping);
      });
      label.append(select); holder.append(label);
    }
    $('pingInventoryMap').hidden = false;
    $('pingInventoryMap').open = !!result.errors.length;
  }
  async function inspect(mapping) {
    if (!ui.token) {
      waitingForToken = true;
      status.textContent = '로컬 서버 연결을 기다리는 중입니다.';
      return;
    }
    const version = ++revision;
    if (!mapping) { mappedSelection = null; explicitSelection = false; }
    else rememberMappedSelection();
    clearReview();
    status.textContent = '표를 읽는 중입니다.';
    try {
      const data = await api('/api/ping/inventory/preview', {text:textInput.value, mapping}, undefined, 20000);
      if (revision !== version) return;
      preview = data;
      if (!mapping) mappingControls(data);
      $('pingInventoryReview').hidden = !data.rows.length;
      if (!mapping) $('pingInventoryReview').open = data.rows.length > 300 || data.rows.some(row => row.errors.length);
      chooseValid(mappedSelection);
      notify();
    } catch (err) {
      if (revision !== version) return;
      status.textContent = err.message;
      if (!err.serverRejection || err.status >= 500) {
        const retry = elem('button', '표 다시 읽기');
        retry.type = 'button';
        retry.onclick = () => inspect(mapping);
        status.append(' ', retry);
      }
    }
  }
  window.addEventListener('aruba-session-ready', () => {
    if (waitingForToken) {
      waitingForToken = false;
      inspect();
    }
  });
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
  window.pingInventory = {active, targets, hasTargets, applyProfile, rolesForRow, roleNames, minimumConcurrency};
})();
