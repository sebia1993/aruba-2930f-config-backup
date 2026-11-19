const {strict: assert} = require('node:assert');
const {test} = require('node:test');
require('../static/power-matrix.js');

const {classify} = globalThis.PowerMatrix;
const ap = {system_name: '서쪽 회의실', model: 'AP-535', system_description: 'Aruba AP-535'};
const row = {status: 'ok', complete: true, watts: 1.2, poe_enabled: true,
  poe_status: 'Delivering', lldp_status: 'ok', lldp_neighbors: [ap]};

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
});

test('a failed LLDP read, placeholders, ordinary neighbors and ambiguous rows do not confirm an AP', () => {
  for (const fields of [
    {lldp_status: 'failed'}, {lldp_status: 'cancelled'}, {lldp_error_code: 'COMMAND_TIMEOUT'},
    {lldp_neighbors: [ap, {system_name: 'office-phone'}]}, {lldp_neighbors: []},
    {lldp_neighbors: [{...ap, system_name: 'N/A'}]}, {lldp_neighbors: [{...ap, system_name: '이름 없음'}]},
    {lldp_neighbors: [{system_name: 'AP-535', system_description: 'Phone'}]},
    {lldp_neighbors: [{...ap, system_name: '00:11:22:33:44:55'}]},
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
