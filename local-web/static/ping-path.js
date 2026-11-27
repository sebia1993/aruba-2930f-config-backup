'use strict';
(() => {
  const status = $('pingTraceStatus'), results = $('pingTraceRows');
  jobNames['ping.trace'] = 'Traceroute';
  const contexts = {incident:'현재 경로', baseline:'정상 기준', baseline_unconfirmed:'정상 기준 저장 보류', recovered:'복구 후 관측', event:'장애 관측 후 자동 진단'};
  const states = {reached:'목적지 도달', partial:'일부 경로 관측', unknown:'결과 미확인', ssh_failure:'SSH 실패', unsupported:'미지원 장비', cli_error:'CLI 거부', source_error:'Source 확인 실패', cancelled:'중단'};
  function paint(items) {
    results.replaceChildren();
    for (const row of items) {
      const details = elem('details');
      details.open = items.length === 1;
      details.append(elem('summary', [row.observedAt, contexts[row.purpose], row.endpoint, row.destination, states[row.state]].join(' · ')));
      if (row.reason) details.append(elem('p', row.reason));
      const baseline = new Map(row.baselineHops.map(h => [h.ttl, h]));
      details.append(elem('p', row.baselineAt ? '비교 기준 '+row.baselineAt+' · 주소 변동 홉 '+(row.changedTtls.join(', ') || '관측 없음') : '같은 Source로 확인한 비교 기준 없음'));
      if (row.failedAt) details.append(elem('p', '최초 실패 관측 '+row.failedAt+' · 진단 시작 시차 '+row.delaySeconds+'초 · '+(row.recoveredBeforeTrace?'복구 후 관측':'진단 전 복구 관측 없음')));
      details.append(simpleTable([['홉','ttl'],['응답 주소', h=>h.ip || '무응답'],['RTT (ms)',h=>h.rttMs ?? '—'],['기준 주소', h=>baseline.get(h.ttl)?.ip || '—']], row.hops));
      results.append(details);
    }
  }
  $('pingTraceStart').addEventListener('click', async () => {
    try {
      const plan = window.readGeneralPingPlan();
      if (plan.targets.trim().split(/\r?\n/).filter(Boolean).length !== 1 || plan.destinations.length !== 1)
        throw new Error('경로 진단에 사용할 스위치 한 대와 목적지 한 곳을 입력하세요.');
      status.textContent = '경로 측정 중';
      let received = false;
      await run('ping.trace', {plan,purpose:$('pingTracePurpose').value}, r => {
        paint([r]); received = true; status.textContent = states[r.state];
      });
      if (!received) status.textContent = $('statusText').textContent || '경로 진단을 접수하지 못했습니다.';
    } catch (e) { status.textContent = e.message; }
  });
  $('pingTraceHistory').addEventListener('click', async () => {
    try { const {items} = await api('/api/ping/routes'); paint(items); status.textContent = '최근 '+items.length+'건'; }
    catch (e) { status.textContent = e.message; }
  });
})();
