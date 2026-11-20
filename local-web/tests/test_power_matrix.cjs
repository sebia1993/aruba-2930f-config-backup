const {strict: assert} = require('node:assert');
const {test} = require('node:test');
require('../static/power-matrix.js');

const {classify, readFailure} = globalThis.PowerMatrix;
const ap = {system_name: '서쪽 회의실', model: 'AP-535', system_description: 'Aruba AP-535'};
const row = {status: 'ok', complete: true, watts: 1.2, poe_enabled: true,
  poe_status: 'Delivering', lldp_status: 'ok', lldp_neighbors: [ap]};

test('tile borders and captions follow only the physical link observation', () => {
  const {linkIndicator} = globalThis.PowerMatrix;
  for (const [raw, state, label] of [
    ['Up', 'up', '↑ Up'], [' down ', 'down', '↓ Down'], ['UP', 'up', '↑ Up'],
    ['확인 불가', 'unknown', '—'], [undefined, 'unknown', '—'], ['', 'unknown', '—'],
    ['Disabled', 'unknown', '—'], ['Down Up', 'unknown', '—'],
  ]) assert.deepEqual(linkIndicator(raw), {state, label});
});

test('retry reasons use the failing read and exclude valid empty or unfinished observations', () => {
  const failedPort = readFailure({status:'failed', message:'SSH 연결 시간 초과', error_code:'CONNECT_TIMEOUT'});
  assert.match(failedPort, /SSH 연결 시간 초과/);
  assert.match(failedPort, /CONNECT_TIMEOUT/);
  const failedLldp = readFailure({...row, message:'전력 조회 완료', lldp_status:'failed',
    lldp_message:'스위치가 명령을 거부했습니다.', lldp_error_code:'COMMAND_REJECTED'});
  assert.match(failedLldp, /LLDP.*스위치가 명령을 거부했습니다/);
  assert.match(failedLldp, /COMMAND_REJECTED/);
  assert.doesNotMatch(failedLldp, /전력 조회 완료/);
  assert.match(readFailure({status:'unsupported', lldp_status:'failed', lldp_error_code:'COMMAND_REJECTED'}),
    /LLDP.*COMMAND_REJECTED/);
  assert.ok(readFailure({status:'failed'}));
  for (const reading of [null, {...row, watts:0, lldp_neighbors:[]}, {status:'unsupported'},
    {status:'cancelled'}, {status:'ok', lldp_status:'pending'}]) assert.equal(readFailure(reading), '');
});

test('actual measured zero overrides link, LLDP, assessment and configured or allocated power', () => {
  for (const watts of [0, '0.000']) {
    const measured = classify({...row, watts, requested_watts: '15', allocated_watts: '30',
      enabled: 'yes', link_status: 'Up', assessment_status: 'normal'});
    assert.equal(measured.state, 'unpowered');
    assert.equal(measured.watts, 0);
  }
});

test('failed, unfinished and invalid measurements never become red or normal', () => {
  for (const fields of [
    {status: 'failed'}, {status: 'cancelled'}, {status: 'unsupported'}, {complete: false},
    {complete: 'no'}, {complete: ''}, {lldp_status: 'pending'}, {error_code: 'COMMAND_TIMEOUT'},
    ...[null, undefined, '', ' ', NaN, Infinity, -1, false, '1 mW', '0x0', '1e-999'].map(watts => ({watts})),
  ]) assert.equal(classify({...row, watts: 0, ...fields}).state, 'unclassified');
  assert.equal(classify().state, 'unclassified');
  assert.equal(classify(null).state, 'unclassified');
});

test('a genuine AP name can have any naming convention and an arbitrarily low draw', () => {
  const observed = classify({...row, watts: '0.0001', link_status: 'Down', enabled: false});
  assert.equal(observed.state, 'normal');
  assert.equal(observed.apName, ap.system_name);
  assert.equal(observed.watts, 0.0001);
  const noModel = {system_name: 'lobby', system_description: 'Wireless Access Point'};
  assert.equal(classify({...row, lldp_neighbors: [noModel]}).apName, 'lobby');
  const regulatory = {...ap, system_description: 'Aruba AP-535/APIN0535'};
  assert.equal(classify({...row, lldp_neighbors: [regulatory]}).apName, ap.system_name);
});

test('a failed LLDP read, placeholders, ordinary neighbors and ambiguous rows do not confirm an AP', () => {
  for (const fields of [
    {lldp_status: 'failed'}, {lldp_status: 'cancelled'}, {lldp_error_code: 'COMMAND_TIMEOUT'},
    {lldp_neighbors: [ap, {system_name: 'office-phone'}]}, {lldp_neighbors: []},
    {lldp_neighbors: [{...ap, system_name: 'N/A'}]}, {lldp_neighbors: [{...ap, system_name: '이름 없음'}]},
    {lldp_neighbors: [{system_name: 'AP-535', system_description: 'Phone'}]},
    {lldp_neighbors: [{...ap, system_description: 'Access Point Controller'}]},
    {lldp_neighbors: [{system_name: 'rack', model: 'AP-535 / AP-515'}]},
    {lldp_neighbors: 'broken', ap_names: ap.system_name},
    {lldp_neighbors: null, ap_names: 'AP-535', assessment_model: 'AP-535', assessment_model_source: 'configured'},
  ]) {
    const observed = classify({...row, ...fields});
    assert.equal(observed.state, 'limited');
    assert.equal(observed.apName, '');
  }
});

test('a completed LLDP reading retains its AP identity when watts are missing', () => {
  const observation = classify({...row, watts: null});
  assert.equal(observation.state, 'unclassified');
  assert.equal(observation.watts, null);
  assert.equal(observation.apName, ap.system_name);
  assert.equal(classify({...row, watts: null, lldp_status: 'failed'}).apName, '');
});

test('a measured positive draw with conflicting, insufficient or unconfirmed supply stays yellow', () => {
  for (const fields of [
    {poe_status: 'Searching'}, {poe_status: 'Fault'}, {poe_status: ''},
    {poe_enabled: 'no'}, {poe_enabled: false}, {assessment_status: 'warning'},
    {requested_watts: '25', allocated_watts: '24.9'},
    {power_evidence: {issues: ['충돌']}},
    {assessment_status: 'unknown', assessment_reason: 'PoE와 LLDP 전원 협상 근거가 서로 다릅니다.'},
  ]) assert.equal(classify({...row, ...fields}).state, 'limited');
});

test('matrix labels follow the current port LLDP name independently of its PoE reading', () => {
  const {accessPointName} = globalThis.PowerMatrix;
  for (const fields of [
    {watts: 0}, {watts: null}, {poe_status: 'Fault'},
    {status: 'unsupported'}, {status: 'failed', error_code: 'COMMAND_REJECTED'},
  ]) assert.equal(accessPointName({...row, ...fields}), ap.system_name);

  for (const fields of [
    {complete: false}, {status: 'cancelled'}, {status: 'refresh_pending'},
    {controlUsed: true}, {refreshInvalidated: true}, {lldp_status: 'pending'},
    {lldp_status: 'failed'}, {lldp_error_code: 'COMMAND_TIMEOUT'},
    {lldp_neighbors: [{system_name: 'office-phone'}]},
    {lldp_neighbors: [ap, {system_name: 'office-phone'}]},
    {lldp_neighbors: [{...ap, system_name: '', chassis_id: 'switch-id'}]},
    {lldp_neighbors: [], ap_names: 'stale', lastSuccessfulObservation: row},
    {lldp_neighbors: null, previousObservation: row},
  ]) assert.equal(accessPointName({...row, ...fields}), '');
  assert.equal(accessPointName(null), '');
});

test('a verified AP keeps its exact advertised name irrespective of spelling or address format', () => {
  for (const name of ['AP-SW-07', 'SWITCH', '3F-01', '535', '00:11:22:33:44:55', '192.0.2.1']) {
    const current = {...row, lldp_neighbors: [{...ap, system_name: name}]};
    assert.equal(classify(current).state, 'normal', name);
    const info = PowerMatrix.neighborIdentity(current);
    assert.equal(info.apName, name);
    assert.deepEqual(info.names, [name]);
    assert.equal(info.label, 'AP 이름');
  }
});

test('only enabled WLAN capability or direct AP identity confirms a named neighbor', () => {
  for (const role of ['wlan-access-point', 'WLAN Access Point', 'wlanAccessPoint']) {
    const current = {...row, lldp_neighbors: [{system_name: '3F-01', system_description: 'ArubaOS',
      system_capabilities_enabled: ['bridge', role]}]};
    assert.equal(classify(current).state, 'normal');
    assert.equal(PowerMatrix.accessPointName(current), '3F-01');
  }
  for (const metadata of [
    {}, {system_capabilities_supported: ['wlan-access-point'], system_capabilities_enabled: ['bridge']},
    {system_capabilities_enabled: ['not-wlan-access-point']},
    {system_capabilities_enabled: ['wlan-access-point-disabled']},
    {system_capabilities_enabled: ['WL AN Access Point']},
    {system_description: 'Power adapter for AP-535', model: 'AP-535'},
    {system_description: 'AP-535 compatible power adapter', model: 'AP-535'},
    {system_description: 'MODEL: 535', model: 'AP-535'},
    {system_description: 'HP 2930F Switch', system_capabilities_enabled: ['wlan-access-point']},
    {system_description: 'AP controller', system_capabilities_enabled: ['wlan-access-point']},
    {model: 'AP-535 / AP-515', system_capabilities_enabled: ['wlan-access-point']},
    {system_description: 'IP Phone', model: 'AP-535'},
    {...ap, system_capabilities_enabled: ['telephone', 'wlan-access-point']},
  ]) {
    const current = {...row, lldp_neighbors: [{...metadata, system_name: 'AP-01'}]};
    assert.equal(classify(current).state, 'limited', JSON.stringify(metadata));
    const info = PowerMatrix.neighborIdentity(current);
    assert.deepEqual(info.names, ['AP-01']);
    assert.equal(info.apName, '');
    assert.match(info.label, /이웃 이름.*AP 유형 미확인/);
    assert.match(classify(current).reason, /유형/);
  }
});

test('multiple LLDP neighbors keep their reported names but cannot be attributed to one AP', () => {
  const current = {...row, lldp_neighbors: [ap, {system_name: 'IP 전화기'}, {...ap, system_name: ''}]};
  const info = PowerMatrix.neighborIdentity(current);
  assert.deepEqual(info.names, ['서쪽 회의실', 'IP 전화기']);
  assert.equal(info.neighborCount, 3);
  assert.equal(info.apName, '');
  assert.match(info.label, /이웃 3대/);
  assert.match(classify(current).reason, /3대/);
});

test('tile descriptions retain every reported name and count even unnamed neighbors', () => {
  const describe = neighbors => PowerMatrix.neighborDescription(
    PowerMatrix.neighborIdentity({...row, lldp_neighbors: neighbors})
  );
  assert.equal(describe([]), '');
  assert.equal(describe([ap]), 'AP 이름 · ' + ap.system_name);
  assert.match(describe([ap, {system_name: 'Desk'}]), /이웃 2대 · 서쪽 회의실 \/ Desk$/);
  assert.match(describe([{}, {}]), /이웃 2대 · 이름 없음$/);
});

test('empty, unnamed, failed and stale LLDP observations keep distinct provenance', () => {
  const info = fields => PowerMatrix.neighborIdentity({...row, ...fields});
  assert.equal(info({lldp_neighbors: []}).neighborCount, 0);
  const unnamed = info({lldp_neighbors: [{...ap, system_name: 'N/A'}]});
  assert.deepEqual(unnamed.names, []);
  assert.equal(unnamed.neighborCount, 1);
  assert.equal(unnamed.apName, '');
  for (const fields of [
    {lldp_status:'failed'}, {lldp_status:'pending'}, {status:'cancelled'}, {controlUsed:true},
    {refreshInvalidated:true}, {complete:false}, {lldp_neighbors:null, ap_names:'stale'},
  ]) {
    const observed = info(fields);
    assert.deepEqual(observed.names, []);
    assert.equal(observed.neighborCount, null);
    assert.equal(observed.apName, '');
    assert.notEqual(classify({...row, ...fields}).state, 'normal');
  }
  assert.match(classify({...row, lldp_status:'failed'}).reason, /LLDP.*실패/);
});
