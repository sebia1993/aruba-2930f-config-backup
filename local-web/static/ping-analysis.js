'use strict';

(() => {
  const host = document.getElementById('pingAnalysis');
  if (!host) return;
  host.innerHTML = `
    <article class="panel">
      <header><h2>Ping 이력 분석</h2><button type="button" id="paLoad">저장된 실행 조회</button></header>
      <div class="panelbody">
        <div class="formgrid"><label>측정 실행 <select id="paRun"></select></label>
        <label>스위치 → 목적지 · Source <select id="paSeries"></select></label></div>
        <div class="bar"><button type="button" id="paPrevRun" disabled>이전 실행 목록</button><button type="button" id="paNextRun" disabled>다음 실행 목록</button>
          <span id="paDownloads"></span></div>
        <details><summary>이력 보존 및 삭제</summary><div class="formgrid"><label>보존 기간 (1~365일)<input id="paDays" type="number" min="1" max="365"></label><label>기록 DB 한도 (32~2048 MiB)<input id="paMaxMb" type="number" min="32" max="2048"></label></div><div class="bar"><button id="paPolicyLoad">현재 정책 조회</button><button id="paPolicySave">보존 정책 저장</button><button id="paDelete" disabled>선택한 종료 이력 삭제</button></div></details>
        <p class="hint" role="status" id="paStatus">저장된 실행을 선택하세요.</p>
        <div id="paWork" hidden>
          <div class="bar" aria-label="이력 분석 보기">
            <button type="button" data-pa-tab="report">조합 이력</button><button type="button" data-pa-tab="heat">동시 장애 분석</button>
            <button type="button" data-pa-tab="nms">NMS 이력 비교</button><button type="button" data-pa-tab="path">관측 지점 비교</button>
          </div>
          <section data-pa-pane="report"><div class="formgrid"><label>조회 시작 (KST)<input id="paFrom" type="datetime-local" step="1"></label><label>조회 종료 (KST)<input id="paUntil" type="datetime-local" step="1"></label></div>
            <div class="bar"><button type="button" id="paReport">조회</button><button type="button" data-pa-range="15">최근 15분</button><button type="button" data-pa-range="60">최근 1시간</button><button type="button" data-pa-range="0">전체 기간</button></div>
            <p id="paMetrics"></p><canvas id="paChart" width="900" height="155" style="width:100%;height:auto" aria-label="관측 시각별 최대 RTT 및 손실 기록"></canvas>
            <p class="hint">가로축은 KST 관측 시각, 세로축은 구간 최대 RTT(ms)입니다. 빨강은 확인 손실, 회색은 미측정·미확인 구간입니다. 정확한 RTT 표본만 백분위수(nearest-rank)에 포함합니다.</p>
            <h3>장애·복구 관측</h3><div id="paEvents"></div><div class="bar"><button id="paPrevEvent">이전 이벤트</button><button id="paNextEvent">다음 이벤트</button></div>
            <h3>개별 측정</h3><div id="paSamples"></div><div class="bar"><button id="paPrevSample">이전 100건</button><button id="paNextSample">다음 100건</button></div>
          </section>
          <section data-pa-pane="heat" hidden><div class="formgrid"><label>공통 목적지<select id="paHeatDest"></select></label><label>시작 (KST)<input id="paHeatTime" type="datetime-local" step="1"></label>
            <label>버킷 크기 (초)<select id="paBucket"><option>5</option><option>10</option><option>30</option><option selected>60</option><option>300</option></select></label></div>
            <details><summary>공통 경로 그룹</summary><label>스위치 IP:SSH포트와 상위 그룹 (한 줄에 하나 · 쉼표 구분)<textarea id="paGroups" rows="3" maxlength="30000"></textarea></label><button id="paGroupSave">이 실행의 그룹 저장</button></details><button id="paHeat">12개 시간 구간 비교</button><div id="paHeatResult"></div>
            <p class="hint">같은 목적지에 대한 서로 다른 스위치의 실패 명령 관측 시간창이 겹칠 때 공통 경로를 점검할 수 있습니다. 물리 장애 원인과 패킷 시각은 확정할 수 없습니다.</p></section>
          <section data-pa-pane="nms" hidden>
            <div class="formgrid"><label>알람 파일 (CSV · 3 MiB 이하)<input id="paFile" type="file" accept=".csv"></label>
            <label>CSV 문자 인코딩<select id="paEncoding"><option value="utf-8-sig">UTF-8</option><option value="cp949">CP949</option></select></label></div>
            <button id="paHeaders">파일의 열 확인</button><div class="formgrid" id="paMap"></div>
            <details><summary>발생·복구가 별도 행인 파일</summary><label><input type="checkbox" id="paSplit"> 두 행을 장비·이벤트 키·시각으로 연결</label><div class="formgrid"><label>발생/복구 구분 열<select id="paKind"></select></label><label>공통 이벤트 키 열<select id="paCategory"></select></label>
            <label>발생값<input id="paOccurs" maxlength="80"></label><label>복구값<input id="paRecovers" maxlength="80"></label></div><label>중복 후보 수동 연결 (복구 행,발생 행 · 한 줄에 하나)<textarea id="paPairs" rows="2"></textarea></label></details>
            <div class="formgrid"><label>원본 시각의 시간대<select id="paZone"><option value="embedded">파일에 적힌 UTC 오프셋 사용</option><option>KST</option><option>UTC</option></select></label>
            <label>NMS Polling 주기 (초 · 선택)<input id="paPoll" type="number" min="1" max="86400"></label></div>
            <div class="bar"><button id="paNmsPreview">유효 행 미리보기</button><button id="paImport" disabled>확인한 행 가져오기</button></div>
            <div id="paImportResult"></div><div class="formgrid"><label>NMS 목적지<select id="paNmsDest"></select></label><label>인접 손실 허용 시간 (초)<input id="paTolerance" type="number" value="60" min="0" max="3600"></label></div>
            <button id="paCompare">알람과 관측 비교</button><div id="paComparison"></div><div class="bar"><button id="paPrevAlarm" disabled>이전 알람</button><button id="paNextAlarm" disabled>다음 알람</button></div>
          </section>
          <section data-pa-pane="path" hidden><div class="formgrid"><label>백본·게이트웨이<select id="paGateway"></select></label><label>중간 L3<select id="paFirewall"></select></label><label>최종 서버<select id="paServer"></select></label>
            <label>시작 (KST)<input id="paPathTime" type="datetime-local" step="1"></label><label>비교 폭 (초)<input id="paWidth" type="number" min="5" max="300" value="60"></label></div>
            <label><input type="checkbox" id="paBaseline"> 실제 경로상 순서와 각 지점의 평시 ICMP 응답을 확인함</label><button id="paPath">선택한 관측 지점 비교</button>
            <p id="paPathResult"></p><p class="hint">중간 장비의 ICMP 응답과 트래픽 전달은 다를 수 있습니다. 동일 출발지의 관측과 링크·정책·서버 기록을 함께 확인하세요.</p>
          </section>
        </div>
      </div>
    </article>`;
  const el = id => document.getElementById('pa' + id);
  const run = () => el('Run').value;
  const stateName = {normal:'정상',no_reply:'무응답',unreachable:'도달 불가',unknown:'미확인',not_run:'미측정',source_error:'Source 오류',cli_error:'CLI 오류',ssh_failure:'SSH 오류',observed_recovery:'복구 관측',interrupted:'관측 단절',open:'복구 미확인'};
  const display = value => value === null || value === undefined || value === '' ? '—' : String(value);
  const utcFromInput = id => el(id).value ? new Date(el(id).value + '+09:00').toISOString() : undefined;
  function kst(value) {
    return value ? new Date(new Date(value).getTime()+9*3600000).toISOString().slice(0,23).replace('T',' ') : '—';
  }
  const inputTime = value => kst(value).replace(' ','T').slice(0,19);
  const metaLabel = m => `${m.switch_ip}:${m.ssh_port} → ${m.destination_label || m.destination_ip}${m.destination_label ? ' ('+m.destination_ip+')' : ''} · ${m.requested_source || m.source_mode}`;
  const options = (element, pairs) => element.replaceChildren(...pairs.map(([value,label])=>new Option(label,value)));
  function table(parent, headers, rows) {
    const wrapper=document.createElement('div'), t=document.createElement('table');
    wrapper.style.overflowX='auto';
    for (const [i,data] of [headers,...rows].entries()) {
      const tr=t.insertRow();
      for (const value of data) {
        const cell = document.createElement(i ? 'td' : 'th');
        if (value instanceof Node) cell.append(value); else cell.textContent=display(value);
        tr.append(cell);
      }
    }
    wrapper.append(t); parent.replaceChildren(wrapper);
  }
  let sampleOffset=0,eventOffset=0,runOffset=0,alarmOffset=0,runInfo=null,approved=null;
  let token=0,lastSampleIds=[];
  function invalidateImport() {approved=null;el('Import').disabled=true;}
  function message(text) {el('Status').textContent=text;}
  function resetResults() {
    ++token; invalidateImport();
    for(const id of ['Metrics','Events','Samples','HeatResult','PathResult','ImportResult','Comparison','Downloads']) el(id).replaceChildren();
    el('Chart').getContext('2d').clearRect(0,0,900,155);
    el('NextAlarm').disabled=el('PrevAlarm').disabled=true;
    el('Map').replaceChildren();
  }
  // Every asynchronous view update belongs to exactly one selection and operation.
  async function request(action, payload, ticket) {
    const result = await api('/api/ping/analysis/'+action,{run:run(),...payload},undefined,60000);
    if (ticket!==token) throw new DOMException('선택이 변경되었습니다.','AbortError');
    return result;
  }
  function bind(id,fn) {
    el(id).addEventListener('click', async () => {const ticket=++token;
      try {await fn(ticket);} catch(e){if(ticket===token && e.name!=='AbortError') message(e.message || '조회하지 못했습니다.');}
    });
  }
  function redrawChart(points) {
    const ctx=el('Chart').getContext('2d'), width=900,height=155;
    ctx.clearRect(0,0,width,height);
    const peak=Math.max(1,...points.map(p=>p.maxRtt || 0));
    const span=(width-60)/Math.max(1,points.length);
    ctx.font='13px sans-serif';ctx.fillStyle='#677580';ctx.fillText(peak.toFixed(1)+' ms',0,15);
    for(const [i,p] of points.entries()) {
      const x=50+i*span;
      if(p.loss){ctx.fillStyle='#c43d39';ctx.fillRect(x,125,Math.max(2,span-1),6);}
      if(p.unknown||p.gap||(!p.loss&&!p.success)){ctx.fillStyle='#a7aab0';ctx.fillRect(x,116,Math.max(2,span-1),5);}
      if(p.maxRtt!==null){ctx.fillStyle='#2684aa';const bar=2+100*p.maxRtt/peak;ctx.fillRect(x,112-bar,Math.max(2,span-1),bar);}
    }
    if(points.length){ctx.fillStyle='#677580';ctx.fillText(kst(points[0].utc).slice(0,19),50,151);ctx.fillText(kst(points.at(-1).utc).slice(0,19),735,151);}
  }
  async function report(ticket) {
    const d=await request('report',{series:Number(el('Series').value),offset:sampleOffset,eventOffset,start:utcFromInput('From'),end:utcFromInput('Until')},ticket);
    const m=d.metrics;
    el('Metrics').textContent=`조합 목표 ${m.target}회 · 선택 기간 시도 ${m.attempts} · 성공 ${m.success} · 확인 손실 ${m.loss} · 미확인 ${m.unknown} · 미측정 ${m.gap} · 유효 손실률 ${display(m.lossRate)}% · 관측 슬롯 대비 유효 ${display(m.coverage)}% · 목표 대비 유효 ${display(m.goalCoverage)}% · 현재/최대 연속 손실 ${m.streak}/${m.longest} · RTT 최소/평균/최대 ${display(m.minimum)}/${display(m.mean)}/${display(m.maximum)} ms · P95/P99 ${display(m.p95)}/${display(m.p99)} ms (${m.rttCount}건)`;
    redrawChart(d.points);
    const eventRows=d.events.map(e=>{
      const detail=document.createElement('button');
      detail.textContent='개별 기록 보기';
      detail.onclick=async()=>{
        const ticket=++token;
        try{sampleOffset=e.sampleOffset;await report(ticket);
          el('Samples').querySelectorAll('tr')[1+(e.firstId===undefined?0:lastSampleIds.indexOf(e.firstId))]?.scrollIntoView({block:'center'});
        }catch(error){if(ticket===token)message(error.message);}
      };
      return [kst(e.lastGood?.[1]),kst(e.first[0])+' / '+kst(e.first[1]),kst(e.last[1]),kst(e.recovery?.[1]),
        e.observedSeconds,e.count,(e.count>=3?'장애 의심 · ':'')+(stateName[e.state]||e.state)+(e.clockUncertain?' · 시계 불확실':''),detail];
    });
    table(el('Events'),['마지막 정상 KST','첫 실패 시작 / 결과 KST','마지막 실패 KST','첫 복구 KST','관측 지속 (초)','연속 손실','상태','근거'],eventRows);
    lastSampleIds=d.samples.map(s=>s.id);
    table(el('Samples'),['시도','예정 KST','명령 시작 KST','결과 수신 KST','상태','RTT ms / 근거','미측정 수','사유'],d.samples.map(s=>[s.attempt,kst(s.scheduled_at),kst(s.started_at),kst(s.received_at),stateName[s.status]||s.status,s.rtt_ms??s.rtt_evidence,s.gap_count,s.reason]));
    el('PrevEvent').disabled=!eventOffset;el('NextEvent').disabled=eventOffset+100>=d.eventCount;
    el('PrevSample').disabled=!sampleOffset;el('NextSample').disabled=sampleOffset+100>=d.sampleCount;
    message(`측정 기록 ${d.sampleCount}건 · 장애·복구 관측 ${d.eventCount}건`);
  }
  async function selectRun(ticket){
    resetResults();ticket=token;
    el('Work').hidden=true;el('Delete').disabled=true;
    if(!run()) return;
    const d=await request('series',{},ticket);
    runInfo=d.run;el('Delete').disabled=!['done','interrupted','failed','cancelled'].includes(runInfo.state);
    el('Groups').value=Object.entries(d.groups||{}).map(([device,group])=>device+', '+group).join('\n');
    const series=d.series.map(m=>[String(m.series_id),metaLabel(m)]);
    for(const id of ['Series','Gateway','Firewall','Server']) options(el(id),series);
    for(const id of ['HeatDest','NmsDest']) options(el(id),[...new Map(d.series.map(m=>[m.destination_ip,m.destination_label?m.destination_label+' ('+m.destination_ip+')':m.destination_ip])).entries()]);
    if(d.series.length>1) el('Firewall').selectedIndex=1;
    if(d.series.length>2) el('Server').selectedIndex=2;
    el('Baseline').checked=false;
    for(const id of ['HeatTime','PathTime']) el(id).value=inputTime(d.run.started_at);
    for(const id of ['From','Until']) el(id).value='';
    const runId = d.run.id;
    if (typeof runId !== 'string' || !/^[a-f0-9]{32}$/.test(runId)) throw new Error('실행 ID를 확인하세요.');
    const links=['csv','xlsx'].map(ext => {
      const a=document.createElement('a');
      a.href='/api/ping/analysis/export/'+encodeURIComponent(runId)+'.'+ext;
      a.textContent=ext==='csv'?'개별 측정 CSV':'분석 Excel';
      a.download='';
      return a;
    });
    el('Downloads').replaceChildren(links[0],' · ',links[1]); el('Work').hidden=!series.length;
    sampleOffset=eventOffset=alarmOffset=0;
    if(series.length) await report(ticket);else message('저장된 측정 조합이 없습니다.');
  }
  async function load(ticket){
    const saved=run();const d=await request('runs',{offset:runOffset},ticket);
    options(el('Run'),d.runs.map(r=>[r.id,kst(r.started_at).slice(0,19)+' · '+r.state+' · '+r.id.slice(0,8)]));
    if(d.runs.some(r=>r.id===saved))el('Run').value=saved;
    el('NextRun').disabled=!d.more;el('PrevRun').disabled=!runOffset;
    await selectRun(ticket);
    if(!d.runs.length)message('저장된 장시간 Ping 실행이 없습니다.');
  }
  bind('Load',load);bind('NextRun',t=>{runOffset+=100;return load(t);});bind('PrevRun',t=>{runOffset=Math.max(0,runOffset-100);return load(t);});
  el('Run').onchange=()=>selectRun(++token).catch(e=>{if(e.name!=='AbortError')message(e.message);});
  el('Series').onchange=()=>{sampleOffset=eventOffset=0;el('Report').click();};
  bind('Report',t=>{sampleOffset=eventOffset=0;return report(t);});
  for(const button of host.querySelectorAll('[data-pa-range]'))button.onclick=()=>{const minutes=Number(button.dataset.paRange);const until=runInfo?.finished_at || new Date().toISOString();
    el('Until').value=minutes?inputTime(until):'';el('From').value=minutes?inputTime(new Date(new Date(until).getTime()-minutes*60000)):'';el('Report').click();};
  for(const [id,delta,mode] of [['PrevEvent',-100,'event'],['NextEvent',100,'event'],['PrevSample',-100,'sample'],['NextSample',100,'sample']])bind(id,t=>{if(mode==='event')eventOffset=Math.max(0,eventOffset+delta);else sampleOffset=Math.max(0,sampleOffset+delta);return report(t);});
  for(const btn of host.querySelectorAll('[data-pa-tab]')) btn.onclick=()=>{++token;for(const pane of host.querySelectorAll('[data-pa-pane]'))pane.hidden=pane.dataset.paPane!==btn.dataset.paTab;};
  bind('Heat',async t=>{
    const d=await request('heat',{destination:el('HeatDest').value,start:utcFromInput('HeatTime'),bucket:Number(el('Bucket').value)},t);
    const view=el('HeatResult');
    table(view,['스위치 / KST',...d.times.map(s=>kst(s).slice(11,19))],d.rows.map(r=>[r.device+(r.group?' · '+r.group:''),...r.cells.map(c=>{
      const b=document.createElement('button');b.textContent=c.loss?(c.success?'일부 손실':'무응답'):c.success?'정상':c.unknown?'불확실':'미측정';
      b.style.borderBottom='3px solid '+(c.loss?'#ce554b':c.success?'#328b79':'#a5a9ad');b.onclick=()=>message(r.device+' · 유효 관측 '+(c.loss+c.success)+'건 · 확인 손실 '+c.loss+' · 미확인 '+c.unknown+' · 미측정 '+c.gap+' · 손실률 '+display(c.rate)+'%');return b;
    })]));
    for(const [i,b] of d.overlap.entries())if(b.failedCount>=2){const p=document.createElement('p');p.textContent=kst(d.times[i])+' · '+(b.devices.length?'손실 관측창 겹침 '+b.devices.join(', '):'동시성 확인 불가')+' · 비교 가능한 장비 '+b.observed+'/'+d.rows.length+' · 같은 시간창 정상 '+(b.counter.join(', ')||'확인 없음')+' · 손실 그룹 '+(b.failureGroups.join(', ')||'미지정')+' · 정상 그룹 '+(b.healthyGroups.join(', ')||'확인 없음');view.append(p);}for(const b of d.overlap){if(b.devices.length>=2 && b.failureGroups.length===1 && b.groupedFailureCount===b.devices.length){const p=document.createElement('p');p.textContent='동일 상위 그룹에 속한 여러 장비의 손실이 겹칩니다. 해당 그룹 업링크·경로 기록과 같은 시간대 다른 그룹의 응답을 확인하세요.';view.append(p);break;}if(b.failureGroups.length>1){const p=document.createElement('p');p.textContent='서로 다른 그룹에서 같은 목적지의 손실이 겹칩니다. 다른 서버와 중간 지점의 응답, 목적지 측 경로·정책을 확인하세요.';view.append(p);break;}}
    message('선택한 시간의 동시 장애 분석을 표시했습니다.');
  });
  bind('GroupSave',async t=>{
    const groups=Object.create(null);
    for(const line of el('Groups').value.split(/\r?\n/)){
      if(!line.trim())continue;
      const comma=line.indexOf(','),device=line.slice(0,comma).trim(),label=line.slice(comma+1).trim();
      if(comma<1||!label||Object.hasOwn(groups,device))throw new Error('각 스위치와 그룹을 한 줄씩 입력하세요.');
      groups[device]=label;
    }
    await request('groups',{groups},t);message('이 실행의 그룹을 저장했습니다.');
  });
  const showPolicy=p=>{el('Days').value=p.days;el('MaxMb').value=p.maxMb;};
  bind('PolicyLoad',async t=>{showPolicy(await request('policy',{},t));message('현재 보존 정책을 표시했습니다.');});
  bind('PolicySave',async t=>{
    if(!confirm('보존 기간과 공간 한도를 변경할까요? 정리 대상이 된 종료 이력은 삭제될 수 있습니다.'))return;
    const p=await request('policy-save',{days:el('Days').valueAsNumber,maxMb:el('MaxMb').valueAsNumber,confirm:true},t);showPolicy(p);message('보존 정책을 저장했습니다.');
  });
  bind('Delete',async t=>{if(!run()||!confirm('선택한 실행의 측정 기록, 그룹과 가져온 NMS 알람을 삭제할까요?'))return;
    await request('delete',{confirm:true},t);runOffset=0;await load(t);message('선택한 종료 이력을 삭제했습니다.');
  });
  let uploaded=null;
  const resetFile=()=>{++token;uploaded=null;invalidateImport();el('ImportResult').replaceChildren();el('Map').replaceChildren();};
  el('File').onchange=el('Encoding').onchange=resetFile;
  async function readFile() {
    const file=el('File').files[0];if(!file||!file.name.toLowerCase().endsWith('.csv')||file.size>3*1024*1024)throw new Error('3 MiB 이하의 CSV 파일을 선택하세요.');
    const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=()=>reject(new Error('파일을 읽지 못했습니다.'));reader.readAsDataURL(file);});
    return {name:file.name,file:data,encoding:el('Encoding').value};
  }
  async function headers(t) {
    const data=await readFile();const d=await request('nms-file',data,t);uploaded=data;invalidateImport();
    el('Map').replaceChildren();
    for(const [id,title] of [['device','장비 IP 또는 IP:SSH포트'],['message','알람 종류/메시지'],['opened','발생 시각'],['closed','복구 시각 (선택)']]){
      const label=document.createElement('label'), select=document.createElement('select');select.id='paCol'+id;label.textContent=title;
      options(select,[['','선택'],...d.headers.map((h,i)=>[String(i),(i+1)+'. '+(h||'빈 헤더')])]);select.onchange=()=>{++token;invalidateImport();};label.append(select);el('Map').append(label);
    }
    for(const id of ['Kind','Category']) options(el(id),[['','선택'],...d.headers.map((h,i)=>[String(i),(i+1)+'. '+(h||'빈 헤더')])]);
    message(d.total+'행 · 해당 열과 시간대를 선택한 뒤 미리보기를 누르세요.');
  }
  bind('Headers',headers);
  for(const id of ['Zone','Poll','Split','Kind','Category','Occurs','Recovers','Pairs'])el(id).oninput=()=>{++token;invalidateImport();};
  bind('NmsPreview',async t=>{
    invalidateImport();if(!uploaded||!el('Coldevice'))throw new Error('먼저 파일의 열을 확인하세요.');
    const columns=Object.fromEntries(['device','message','opened','closed'].map(k=>[k,el('Col'+k).value===''?null:Number(el('Col'+k).value)]));
    const settings={...uploaded,columns,zone:el('Zone').value,polling:el('Poll').value===''?null:el('Poll').valueAsNumber};
    if(el('Split').checked){
      if(!el('Kind').value||!el('Category').value)throw new Error('발생/복구 구분과 공통 이벤트 키 열을 지정하세요.');
      const pairs=Object.create(null);
      for(const line of el('Pairs').value.split(/\r?\n/)){
        if(!line.trim())continue;
        const match=/^\s*(\d+)\s*,\s*(\d+)\s*$/.exec(line);
        if(!match||Object.hasOwn(pairs,match[1]))throw new Error('수동 연결은 복구 행,발생 행 형식으로 입력하세요.');
        pairs[match[1]]=Number(match[2]);
      }
      settings.split={state:Number(el('Kind').value),category:Number(el('Category').value),occurs:el('Occurs').value,recovers:el('Recovers').value,pairs};
    }
    const d=await request('nms-file',settings,t);approved={...settings,digest:d.digest,previewKey:d.previewKey,confirm:true};el('Import').disabled=!d.valid;
    const result=el('ImportResult');table(result,['파일 행','장비','알람','발생 KST','복구 KST'],d.preview.map(r=>[r.row,r.device,r.message,kst(r.opened),r.closed?kst(r.closed):'미복구/미확인']));
    for(const row of d.errors){const p=document.createElement('p');p.textContent=row.row+'행 · '+row.reason+(row.candidates?.length?' 발생 행 후보 '+row.candidates.join(', '):'');result.append(p);}
    message('유효 '+d.valid+'행 · 중복 '+d.duplicates+'행 · 입력 오류 '+d.invalid+'행. 처음 20행씩 표시합니다.');
  });
  bind('Import',async t=>{if(!approved)return;const d=await request('nms-commit',approved,t);invalidateImport();message('가져온 알람 '+d.inserted+'건 · 중복 '+d.duplicates+'건');});
  async function compare(t) {
    const d=await request('nms-compare',{destination:el('NmsDest').value,tolerance:el('Tolerance').valueAsNumber,offset:alarmOffset},t);
    table(el('Comparison'),['장비 · 발생 KST','알람','복구 KST','Polling 초','겹친 손실','겹친 정상','인접 손실','미확인·공백'],d.rows.map(a=>[a.device+' · '+kst(a.opened),a.message,a.closed?kst(a.closed):'미복구/미확인',a.polling_seconds,a.strictLoss,a.strictSuccess,a.nearLoss,a.unknown]));
    el('PrevAlarm').disabled=!alarmOffset;el('NextAlarm').disabled=!d.more;message('같은 장비와 선택한 NMS 목적지의 관측을 비교했습니다. 알람 발생·복구 시각에는 수집 지연이 있을 수 있습니다.');
  }
  bind('Compare',t=>{alarmOffset=0;return compare(t);});bind('PrevAlarm',t=>{alarmOffset=Math.max(0,alarmOffset-50);return compare(t);});bind('NextAlarm',t=>{alarmOffset+=50;return compare(t);});
  bind('Path',async t=>{
    const d=await request('path',{series:['Gateway','Firewall','Server'].map(id=>Number(el(id).value)),start:utcFromInput('PathTime'),seconds:el('Width').valueAsNumber,baselineVerified:el('Baseline').checked},t);
    const names={success:'정상',loss:'손실',inconclusive:'판정 보류'};
    el('PathResult').textContent=d.states.map((s,i)=>['백본','중간 L3','서버'][i]+' '+names[s]+' (성공 '+d.counts[i].success+', 손실 '+d.counts[i].loss+')').join(' · ')+' — '+(d.candidate||(d.sourceVerified?'경로·평시 응답 또는 관측 근거가 부족해 의심 구간 판정을 보류합니다.':'측정에 사용된 동일 Source IP를 확인하지 못해 의심 구간 판정을 보류합니다.'));
  });
})();
