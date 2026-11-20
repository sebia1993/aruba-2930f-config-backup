/* Show saved switch observations without asking the switch to take another reading. */
(() => {
  'use strict';

  const byId = id => document.getElementById(id);
  const panel = byId('powerHistory');
  const chart = byId('powerHistoryChart');
  const plot = byId('powerHistoryPlot');
  const notice = byId('powerHistoryStatus');
  const retry = byId('powerHistoryRetry');
  const buttons = [...panel.querySelectorAll('[data-hours]')];
  const dateFormat = new Intl.DateTimeFormat('ko-KR', {
    year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23'
  });
  const hourTick = new Intl.DateTimeFormat('ko-KR', {hour:'2-digit', minute:'2-digit', hourCycle:'h23'});
  const secondTick = new Intl.DateTimeFormat('ko-KR', {hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23'});
  const dayTick = new Intl.DateTimeFormat('ko-KR', {month:'numeric', day:'numeric'});
  const number = new Intl.NumberFormat('ko-KR', {maximumFractionDigits:3});
  const watts = value => number.format(value) + ' W';
  let port = null, revision = '', hours = 1, request = null;
  let samples = [], selected = -1, start = 0, end = 0, geometry = null;

  function changeText(sample) {
    if (sample.previousWatts == null) return '이전 측정 없음';
    const delta = Math.round((sample.watts - sample.previousWatts) * 1000) / 1000;
    return (delta > 0 ? '+' : '') + watts(delta);
  }

  function message(text) {
    notice.textContent = text;
    notice.hidden = !text;
  }

  function clearChart() {
    samples = [];
    selected = -1;
    chart.hidden = true;
    geometry = null;
    plot.replaceChildren();
    delete plot.dataset.samples;
    delete plot.dataset.gaps;
    byId('powerHistoryReading').textContent = '';
    for (const id of ['powerHistoryLatest','powerHistoryDelta','powerHistoryTime']) byId(id).textContent='';
  }

  function svg(tag, attrs, text) {
    const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [name,value] of Object.entries(attrs)) element.setAttribute(name,String(value));
    if (text != null) element.textContent=text;
    return element;
  }

  function axisCeiling() {
    let highest = 1;
    for (const sample of samples) highest = Math.max(highest, sample.watts);
    const unit = 10 ** Math.floor(Math.log10(highest));
    return [1,2,5,10].find(step => step * unit >= highest) * unit;
  }

  function setSelected(index) {
    if (!samples.length) return;
    selected = Math.max(0, Math.min(samples.length - 1, index));
    const sample = samples[selected];
    const label = dateFormat.format(sample.time) + ' · ' + watts(sample.watts) + ' · 직전 대비 ' + changeText(sample);
    byId('powerHistoryReading').textContent = label;
    byId('powerHistoryPrev').disabled = selected === 0;
    byId('powerHistoryNext').disabled = selected === samples.length-1;
    plot.setAttribute('aria-valuenow',String(selected+1));
    plot.setAttribute('aria-valuetext',label);
    const marker = plot.querySelector('.history-cursor');
    if (marker && geometry) {
      marker.setAttribute('cx',geometry.x(sample.time));
      marker.setAttribute('cy',geometry.y(sample.watts));
    }
  }

  function draw() {
    if (!panel.open || panel.hidden || chart.hidden || !samples.length) return;
    const width = plot.clientWidth;
    if (!width) return;
    const height=170, top=17, bottom=143, left=40, right=width-10, ceiling=axisCeiling();
    const x = time => left+(right-left)*(time-start)/(end-start);
    const y = value => bottom-(bottom-top)*value/ceiling;
    geometry={x,y,width};
    plot.setAttribute('viewBox','0 0 '+width+' '+height);
    plot.setAttribute('aria-valuemin','1');
    plot.setAttribute('aria-valuemax',String(samples.length));
    const group = document.createDocumentFragment();
    group.append(svg('text',{x:left-7,y:10,'text-anchor':'end'},'W'));
    for (const value of [0,ceiling/2,ceiling]) {
      const pos=y(value);
      group.append(svg('line',{x1:left,x2:right,y1:pos,y2:pos,class:'history-grid'}));
      group.append(svg('text',{x:left-7,y:pos+4,'text-anchor':'end'},number.format(value)));
    }
    const span=end-start;
    const ticks = span < 120000 ? secondTick : span > 86400000 ? dayTick : hourTick;
    const tickTimes=[[start,'start'],[end,'end']];
    if (right-left>210) tickTimes.splice(1,0,[(start+end)/2,'middle']);
    for (const [time,anchor] of tickTimes) {
      group.append(svg('text',{x:x(time),y:height-5,'text-anchor':anchor},ticks.format(time)));
    }
    // Two paths keep the DOM small even for seven days of minute samples. A new
    // move starts every disconnected run; the independent round caps are real samples.
    const line=[], dots=[];
    let previous=null;
    for (const sample of samples) {
      const pos=x(sample.time).toFixed(2)+' '+y(sample.watts).toFixed(2);
      if (sample.connected && previous) {
        if (!previous.connected) line.push('M'+previous.pos);
        line.push('L'+pos);
      }
      dots.push('M'+pos+'h0.01');
      previous={pos,connected:sample.connected};
    }
    if (line.length) group.append(svg('path',{class:'history-line',d:line.join('')}));
    group.append(svg('path',{class:'history-points',d:dots.join('')}));
    group.append(svg('circle',{class:'history-cursor',r:4.5}));
    plot.replaceChildren(group);
    setSelected(selected);
  }

  function showData(data, preferred) {
    start=Date.parse(data.start);
    end=Date.parse(data.end);
    if (!Array.isArray(data.points) || !(end>start)) throw new Error('invalid history range');
    let previousValid = false, gaps = 0, firstRecord, lastRecord;
    for (const point of data.points) {
      const time=Date.parse(point.at);
      if (!(time>=start && time<=end)) continue;
      if (firstRecord==null) firstRecord=time;
      lastRecord=time;
      if (!Number.isFinite(point.watts) || point.watts<0) {
        previousValid=false;
        gaps++;
        continue;
      }
      samples.push({...point,time,connected:previousValid && point.connected===true});
      previousValid=true;
    }
    plot.dataset.samples=String(samples.length);
    plot.dataset.gaps=String(gaps);
    const notes=[];
    if (data.warning) notes.push(data.warning);
    if (!samples.length) {
      notes.push(gaps ? '이 기간에는 수신된 전력 측정값이 없습니다. 조회 누락 '+gaps+'회.' :
        '이 기간에 저장된 전력 측정값이 없습니다.');
      message(notes.join(' · '));
      return;
    }
    if (lastRecord>firstRecord) {
      start=firstRecord;
      end=lastRecord;
    }
    if (gaps) notes.push('조회 누락 '+number.format(gaps)+'회');
    if (data.truncated) notes.push('기록이 많아 이 기간의 최근 측정값만 표시합니다.');
    message(notes.join(' · '));
    const latest=samples[samples.length-1];
    byId('powerHistoryLatest').textContent=watts(latest.watts);
    byId('powerHistoryDelta').textContent=changeText(latest);
    byId('powerHistoryTime').textContent=dateFormat.format(latest.time);
    const priorIndex=preferred==null ? -1 : samples.findIndex(sample=>sample.at===preferred);
    selected=priorIndex>=0 ? priorIndex : samples.length-1;
    chart.hidden=false;
    draw();
  }

  async function load() {
    request?.abort();
    const preferred=selected>=0 && selected<samples.length-1 ? samples[selected].at : null;
    clearChart();
    retry.hidden=true;
    if (!panel.open || !port) {
      message('');
      return;
    }
    const active=new AbortController();
    request=active;
    message('측정 기록을 읽는 중입니다.');
    const query=new URLSearchParams({ip:port.ip,ssh_port:port.sshPort,interface:port.port,hours});
    try {
      const response=await fetch('/api/power-history?'+query,{credentials:'same-origin',cache:'no-store',signal:active.signal});
      if (!response.ok) throw new Error('history request failed');
      const data=await response.json();
      if (active.signal.aborted) return;
      showData(data,preferred);
    } catch {
      if (active.signal.aborted) return;
      clearChart();
      message('전력 측정 기록을 읽을 수 없습니다.');
      retry.hidden=false;
    }
  }

  function setPort(row) {
    const identityChanged=port?.key!==row?.key;
    const stamp=row?.raw || {};
    const newRevision=[stamp.observed_at,stamp.poe_finished_at,stamp.finished_at,stamp.status].join('|');
    if (!identityChanged && newRevision===revision) return;
    port=row;
    revision=newRevision;
    panel.hidden=!row;
    if (identityChanged) clearChart();
    load();
  }

  panel.addEventListener('toggle',load);
  retry.onclick=load;
  for (const button of buttons) button.onclick=()=>{
    const next=Number(button.dataset.hours);
    if (hours===next) return;
    hours=next;
    for (const item of buttons) item.setAttribute('aria-pressed',String(item===button));
    load();
  };
  byId('powerHistoryPrev').onclick=()=>setSelected(selected-1);
  byId('powerHistoryNext').onclick=()=>setSelected(selected+1);
  plot.addEventListener('keydown',event=>{
    const keys={ArrowLeft:selected-1,ArrowDown:selected-1,ArrowRight:selected+1,ArrowUp:selected+1,Home:0,End:samples.length-1};
    if (!Object.hasOwn(keys,event.key)) return;
    event.preventDefault();
    setSelected(keys[event.key]);
  });
  plot.addEventListener('click',event=>{
    if (!geometry || !samples.length) return;
    const rect=plot.getBoundingClientRect();
    const px=(event.clientX-rect.left)*geometry.width/rect.width;
    const py=(event.clientY-rect.top)*170/rect.height;
    let nearest=0, distance=Infinity;
    for (let i=0;i<samples.length;i++) {
      const sample=samples[i];
      const squared=(geometry.x(sample.time)-px)**2+(geometry.y(sample.watts)-py)**2;
      if (squared<=distance) { nearest=i; distance=squared; }
    }
    setSelected(nearest);
  });
  let lastWidth=0;
  new ResizeObserver(()=>{
    const width=plot.clientWidth;
    if (width && lastWidth!==width) draw();
    lastWidth=width;
  }).observe(plot);
  window.PortPowerHistory={setPort};
})();
