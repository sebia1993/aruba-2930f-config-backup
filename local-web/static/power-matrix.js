/* Apply the same switch-only display rules to query events and saved CSV rows. */
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

  function measurement(value) {
    if (typeof value === 'string' && /^[+]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(value.trim())) {
      const number = Number(value);
      if (number === 0 && /[1-9]/.test(value.split(/e/i)[0])) return null;
      value = number;
    }
    return Number.isFinite(value) && value >= 0 ? value : null;
  }

  function decoded(value) {
    if (typeof value !== 'string') return value;
    try { return JSON.parse(value); } catch { return null; }
  }

  function namedAccessPoint(name, model = '', description = '') {
    if (!name || /^(?:[-?–—]+|n\/?a|none|null|undefined|unknown|unnamed|no name|not (?:available|reported|specified)|LLDP (?:pending|failed|cancelled)|이름 없음|이웃 없음|미수집|미확인|확인 불가)$/i.test(name)) return '';
    if (/^(?:[\da-f]{2}[:-]){5}[\da-f]{2}$|^(?:[\da-f]{4}\.){2}[\da-f]{4}$|^[\da-f]{12}$|^(?:\d{1,3}\.){3}\d{1,3}$|^[\da-f]*:[\da-f:]+$/i.test(name)) return '';
    // An AP-like substring in a switch/uplink label is not a neighbor AP identity.
    const otherDevice = /(?:^|[\s_.-])(?:switch|sw|router|controller|uplink|telephone|phone|printer|laptop|desktop|workstation|server|pc)(?=$|[\s_.-]|\d)/i;
    if (otherDevice.test(name) || otherDevice.test(description) || otherDevice.test(model) || /\s\/\s/.test(model)) return '';
    const apModel = /^I?AP[- ]\d{3}[A-Z]?$/i.test(model);
    const apDescription = /\b(?:I?AP[- ]\d{3}[A-Z]?|APIN0\d{3}|(?:wireless\s+)?access[ -]point)\b/i.test(description);
    const apName = /(?:^|[\s_.-])I?AP(?=\d|[\s_.-]|$)/i.test(name) && !/^i?ap$/i.test(name);
    return apModel || apDescription || apName ? name : '';
  }

  function accessPointName(row) {
    if (text(row.lldp_error_code) || (Object.hasOwn(row, 'lldp_status') && token(row.lldp_status) !== 'ok')) return '';
    if (Object.hasOwn(row, 'lldp_neighbors')) {
      const neighbors = decoded(row.lldp_neighbors);
      if (!Array.isArray(neighbors) || neighbors.length !== 1) return '';
      const neighbor = neighbors[0];
      return namedAccessPoint(text(neighbor?.system_name), text(neighbor?.model), text(neighbor?.system_description));
    }
    // Early CSVs stored only LLDP names. A single AP-style name is the only AP evidence there.
    const legacyName = text(row.ap_names);
    return /\||\s\/\s/.test(legacyName) ? '' : namedAccessPoint(legacyName);
  }

  function supplyProblem(row) {
    const status = token(row.assessment_status);
    if (['warning', 'off', 'searching', 'error', 'unsupported'].includes(status)) {
      return text(row.assessment_reason) || '스위치 전력 진단에서 공급 이상을 관측했습니다.';
    }
    const evidence = decoded(row.power_evidence);
    if (Array.isArray(evidence?.issues) && evidence.issues.length) {
      return '스위치가 보고한 전력 근거에 충돌하거나 해석할 수 없는 값이 있습니다.';
    }
    // Older query exports store these diagnostic causes in the assessment reason.
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
    // Exports predating the completion column only contained final query results.
    if ((Object.hasOwn(row, 'complete') && row.complete !== true && !['yes', 'true'].includes(token(row.complete))) ||
        token(row.lldp_status) === 'pending') {
      return result('unclassified', '이 포트의 조회가 완료되지 않았습니다.');
    }
    watts = measurement(row.watts);
    if (watts === null) return result('unclassified', '유효한 실측 전력값이 없습니다.');
    apName = accessPointName(row);
    if (watts === 0) return result('unpowered', '스위치에서 실측 전력 0 W를 보고했습니다.');
    const problem = supplyProblem(row);
    if (problem) return result('limited', problem);
    if (!apName) return result('limited', '전력을 공급하고 있으나 이 포트의 LLDP에서 AP 이름을 확인하지 못했습니다.');
    return result('normal', '스위치에서 전력 공급과 LLDP 이웃 AP 이름을 확인했습니다.');
  }

  globalThis.PowerMatrix = Object.freeze({labels, classify});
})();
