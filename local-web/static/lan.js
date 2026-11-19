(() => {
  const local = location.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(location.hostname);
  if (!local) {
    $('shutdown').hidden = true;
    $('backupDirectory').closest('label').hidden = true;
    $('chooseBackupDirectory').hidden = true;
    return;
  }
  const dialog = $('lanDialog'), message = $('lanMessage'), address = $('lanAddress');
  const actions = ['lanStart', 'lanRenew', 'lanStop'];
  const busy = flag => actions.forEach(id => { $(id).disabled = flag; });

  function render(data) {
    $('lanSetup').hidden = data.active;
    $('lanRunning').hidden = !data.active;
    $('lanAccess').textContent = data.active ? 'LAN 공유 중' : 'LAN 공유';
    if (data.active) {
      $('lanEndpoint').textContent = data.url + ' · 접속 허용 대역 ' + data.network;
      $('lanLink').value = data.invitation || '';
      $('lanCopy').disabled = !data.invitation;
      $('lanFingerprint').textContent = data.fingerprint;
      if (!data.invitation) message.textContent = '새 링크를 발급해 다른 기기를 초대하세요.';
    } else {
      $('lanLink').value = '';
      if (data.interfaces) {
        address.replaceChildren(...data.interfaces.map(n => new Option(n.address + '/' + n.prefix, n.address)));
      }
      $('lanStart').disabled = !address.value;
      if (!address.value) message.textContent = '이 PC의 유선 또는 Wi-Fi 연결을 확인하세요.';
    }
  }
  async function request(path, body) {
    busy(true);
    message.textContent = '';
    try {
      const data = await api(path, body);
      busy(false);
      render(data);
    } catch (error) {
      busy(false);
      message.textContent = error.message;
    }
  }
  $('lanAccess').hidden = false;
  $('lanAccess').onclick = () => {
    dialog.showModal();
    request('/api/sharing');
  };
  $('lanStart').onclick = () => request('/api/sharing/enable', {address: address.value});
  $('lanRenew').onclick = () => request('/api/sharing/renew', {});
  $('lanStop').onclick = async () => {
    await request('/api/sharing/stop', {});
    if (!$('lanRunning').hidden) return;
    await request('/api/sharing');
  };
  $('lanCopy').onclick = async () => {
    try {
      await navigator.clipboard.writeText($('lanLink').value);
      message.textContent = '초대 링크를 복사했습니다.';
    } catch {
      $('lanLink').select();
      message.textContent = '선택된 링크를 복사하세요.';
    }
  };
  dialog.addEventListener('close', () => { $('lanLink').value = ''; });
})();
