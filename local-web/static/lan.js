(() => {
  const local = location.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(location.hostname);
  if (!local) {
    $('shutdown').hidden = true;
    $('backupDirectory').closest('label').hidden = true;
    $('chooseBackupDirectory').hidden = true;
    return;
  }
  const dialog = $('lanDialog'), message = $('lanMessage'), address = $('lanAddress');
  const toggle = $('lanToggle'), link = $('lanLink');
  let state = null, pending = false;

  function render() {
    const active = state?.active === true;
    toggle.setAttribute('aria-checked', String(active));
    $('lanState').textContent = state ? (active ? 'ON' : 'OFF') : '상태 확인';
    toggle.disabled = pending || !state || (!active && !address.value);
    address.disabled = pending || !state || active || !address.value;
    $('lanRunning').hidden = !active;
    link.value = active ? state.url : '';
    $('lanFingerprint').textContent = active ? state.fingerprint : '';
    $('lanCopy').disabled = pending || !active;
  }

  function receive(data) {
    if (data.interfaces) {
      const selected = data.address || address.value;
      const choices = data.interfaces.map(n => n.address);
      if (data.active && !choices.includes(data.address)) choices.push(data.address);
      address.replaceChildren(...choices.map(ip => new Option(ip, ip)));
      if (choices.includes(selected)) address.value = selected;
    }
    state = data;
  }

  async function request(path, body) {
    if (pending) return;
    pending = true;
    render();
    message.textContent = body === undefined ? '공유 상태를 확인하고 있습니다.' :
      state.active ? '공유를 끄고 있습니다.' : '공유를 켜고 있습니다.';
    try {
      receive(await api(path, body));
      message.textContent = state.active || address.value ? '' : '이 PC의 유선 또는 Wi-Fi 연결을 확인하세요.';
    } catch (error) {
      state = null;
      if (body !== undefined) {
        // The change may have succeeded even if its response was lost.
        try { receive(await api('/api/sharing')); } catch { /* Keep the switch disabled. */ }
      }
      message.textContent = error.message;
      if (!state) message.textContent += ' 이 창을 다시 열어 공유 상태를 확인하세요.';
    } finally {
      pending = false;
      render();
    }
  }

  $('lanAccess').hidden = false;
  $('lanAccess').onclick = () => {
    dialog.showModal();
    request('/api/sharing');
  };
  toggle.onclick = () => state.active ? request('/api/sharing/stop', {}) :
    request('/api/sharing/enable', {address: address.value});
  $('lanCopy').onclick = async () => {
    const url = link.value;
    try {
      await navigator.clipboard.writeText(url);
      if (link.value === url && !pending) message.textContent = '접속 주소를 복사했습니다.';
    } catch {
      if (link.value === url && !pending) {
        link.select();
        message.textContent = '선택한 접속 주소를 복사하세요.';
      }
    }
  };
})();
