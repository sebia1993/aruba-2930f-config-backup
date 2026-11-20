/* Classify observed switch power and LLDP evidence. */
(() => {
  'use strict';

  const labels = Object.freeze({
    unpowered: '전력 미공급',
    limited: '전력 공급 · 점검',
    normal: '전력 정상 · AP 이름 확인',
    unclassified: '미분류',
  });
  const text = value => typeof value === 'string' ? value.trim() : '';
  const token = value => text(value).toLowerCase();

  function readFailure(row) {
    const portFailed = row?.status === 'failed';
    if (!portFailed && row?.lldp_status !== 'failed') return '';
    const stage = portFailed ? '포트 조회 실패' : 'LLDP 조회 실패';
    const message = text(portFailed ? row.message : row.lldp_message);
    const code = text(portFailed ? row.error_code : row.lldp_error_code);
    const reason = message || (code ? '' : '상세 사유가 기록되지 않았습니다.');
    return [stage, reason, code && !reason.includes(code) ? code : ''].filter(Boolean).join(' · ');
  }

  function linkLabel(value) {
    switch (token(value)) {
      case 'up': return 'Up';
      case 'down': return 'Down';
      default: return '미확인';
    }
  }

  function linkIndicator(value) {
    switch (linkLabel(value)) {
      case 'Up': return {state: 'up', label: '↑ Up'};
      case 'Down': return {state: 'down', label: '↓ Down'};
      default: return {state: 'unknown', label: '—'};
    }
  }

  function measurement(value) {
    if (typeof value === 'string' && /^[+]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(value.trim())) {
      const number = Number(value);
      if (number === 0 && /[1-9]/.test(value.split(/e/i)[0])) return null;
      value = number;
    }
    return Number.isFinite(value) && value >= 0 ? value : null;
  }

  const missingName = /^(?:[-?–—]+|n\/?a|none|null|undefined|unknown|unnamed|no name|not (?:available|reported|specified)|LLDP (?:pending|failed|cancelled)|이름 없음|이웃 없음|미수집|미확인|확인 불가)$/i;

  function advertisesAccessPoint(neighbor) {
    const model = text(neighbor.model);
    const description = text(neighbor.system_description).split(/[,;]\s*(?:version|build)\b/i)[0];
    const enabled = Array.isArray(neighbor.system_capabilities_enabled)
      ? neighbor.system_capabilities_enabled.map(token) : [];
    // SysName is an operator's label. Only advertised device identity and roles describe its type.
    const otherDevice = /\b(?:switch|controller|router|telephone|phone|printer|laptop|desktop|workstation|server|AOS-CX)\b/i;
    if (model.includes('/') || otherDevice.test(description) || otherDevice.test(model)
        || enabled.some(role => /^(?:telephone|station[ _-]?only|docsis[ _-]?cable[ _-]?device)$/.test(role))) return false;
    if (enabled.some(role => /^wlan[ _-]?(?:access[ _-]?point|ap)$/.test(role))) return true;
    if (/^(?:(?:HPE )?Aruba(?: Networking| Networks)? )?(?:Wireless )?Access[ -]Point$/i.test(description)) return true;
    if (!/^I?AP[- ]\d{3}[A-Z]?$/i.test(model)) return false;
    // Older observations could derive a model from free text anywhere in a description.
    if (Array.isArray(neighbor.system_capabilities_enabled) || !description) return true;
    return /^(?:(?:HPE\s+)?Aruba(?:\s+Networking|\s+Networks)?\s+)?(?:Regulatory\s+)?(?:I?AP[- ]\d{3}[A-Z]?|APIN0\d{3})(?=$|\s*[,/;(]|\s+(?:(?:wireless|campus)\s+)?access point\b|\s+version\b)/i.test(description)
      || /^ArubaOS\s*\(\s*MODEL\s*:\s*\d{3}[A-Z]?\s*\)/i.test(description);
  }

  function neighborIdentity(row) {
    const info = {apName: '', names: [], neighborCount: null, label: 'LLDP 이웃 이름', reason: ''};
    if (row?.complete !== true || row.controlUsed || row.refreshInvalidated
        || !['ok', 'unsupported', 'failed'].includes(token(row.status))) {
      info.reason = '이 포트에 최신 LLDP 조회 결과가 없습니다.';
      return info;
    }
    if (text(row.lldp_error_code) || token(row.lldp_status) === 'failed') {
      info.reason = ['LLDP 조회 실패', text(row.lldp_message) || text(row.lldp_error_code)].filter(Boolean).join(' · ');
      return info;
    }
    const neighbors = row.lldp_neighbors;
    if (token(row.lldp_status) !== 'ok' || !Array.isArray(neighbors)) {
      info.reason = 'LLDP 수집 결과를 확인할 수 없습니다.';
      return info;
    }
    info.neighborCount = neighbors.length;
    info.names = neighbors.map(neighbor => text(neighbor?.system_name)).filter(name => name && !missingName.test(name));
    if (!neighbors.length) {
      info.reason = '이 포트에서 발견된 LLDP 이웃이 없습니다.';
    } else if (neighbors.length > 1) {
      info.label += ' · 이웃 ' + neighbors.length + '대';
      info.reason = 'LLDP 이웃 ' + neighbors.length + '대가 조회되어 이 포트의 AP를 특정할 수 없습니다.';
    } else if (!info.names.length) {
      info.reason = '이 포트의 LLDP 이웃이 장비 이름을 보고하지 않았습니다.';
    } else if (advertisesAccessPoint(neighbors[0])) {
      info.apName = info.names[0];
      info.label = 'AP 이름';
    } else {
      info.label += ' · AP 유형 미확인';
      info.reason = 'LLDP 이웃 이름을 수집했습니다. AP 유형 미확인.';
    }
    return info;
  }

  function accessPointName(row) {
    return neighborIdentity(row).apName;
  }

  function supplyProblem(row) {
    const status = token(row.assessment_status);
    if (['warning', 'off', 'searching', 'error', 'unsupported'].includes(status)) {
      return text(row.assessment_reason) || '스위치 전력 진단에서 공급 이상을 관측했습니다.';
    }
    const evidence = row.power_evidence;
    if (Array.isArray(evidence?.issues) && evidence.issues.length) {
      return '스위치가 보고한 전력 근거에 충돌하거나 해석할 수 없는 값이 있습니다.';
    }
    if (status === 'unknown' && /해석 불가 또는 서로 충돌|전원 협상 근거가 서로 다릅니다|전원 공급 방식 근거가 서로 충돌/.test(text(row.assessment_reason))) {
      return text(row.assessment_reason);
    }
    const requested = measurement(row.requested_watts);
    const allocated = measurement(row.allocated_watts);
    if (requested !== null && allocated !== null && allocated < requested) {
      return '스위치에 기록된 할당 전력이 요청 전력보다 낮습니다.';
    }
    if (row.poe_enabled === false || ['no', 'off', 'false'].includes(token(row.poe_enabled))) {
      return '실측 전력은 양수이지만 스위치의 PoE 설정은 OFF입니다.';
    }
    if (token(row.poe_status) !== 'delivering') {
      return '실측 전력은 양수이지만 스위치에서 Delivering 상태를 확인하지 못했습니다.';
    }
    return '';
  }

  function classify(row = {}) {
    let watts = null;
    let apName = '';
    const result = (state, reason) => ({state, label: labels[state], reason, watts, apName});
    if (!row || token(row.status) !== 'ok' || text(row.error_code)) {
      return result('unclassified', text(row?.message) || '성공한 스위치 전력 조회 결과가 없습니다.');
    }
    if (row.complete !== true || token(row.lldp_status) === 'pending') {
      return result('unclassified', '이 포트의 조회가 완료되지 않았습니다.');
    }
    const neighbor = neighborIdentity(row);
    apName = neighbor.apName;
    watts = measurement(row.watts);
    if (watts === null) return result('unclassified', '유효한 실측 전력값이 없습니다.');
    if (watts === 0) return result('unpowered', '스위치에서 실측 전력 0 W를 보고했습니다.');
    const problem = supplyProblem(row);
    if (problem) return result('limited', problem);
    if (!apName) return result('limited', neighbor.reason);
    return result('normal', '스위치에서 전력 공급과 LLDP 이웃 AP 이름을 확인했습니다.');
  }

  globalThis.PowerMatrix = Object.freeze({labels, classify, linkLabel, linkIndicator, readFailure, accessPointName, neighborIdentity});
})();
