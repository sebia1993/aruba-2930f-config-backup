'use strict';

const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { runInNewContext } = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');

const controller = readFileSync(join(__dirname, '../static/manual-queue.js'), 'utf8');

test('a slow workspace response cannot overwrite a newer completed reservation', async () => {
  const element = () => ({
    dataset: {},
    hidden: true,
    append() {},
    replaceChildren() {},
  });
  const ids = new Map();
  const window = { addEventListener() {}, portTools: { matchesInventory: () => true } };
  const rows = [];
  const request = id => ({ id, action: 'control.quick-execute', operation: 'poe_off', wasQueued: true, targets: [] });
  const first = request('a');
  const next = request('b');
  let items = [{ ...first, state: 'running' }, { ...next, state: 'queued' }];
  let requests = 0;
  let releaseFirst;
  const firstSnapshot = new Promise(resolve => { releaseFirst = resolve; });
  let markStarted;
  const started = new Promise(resolve => { markStarted = resolve; });
  const snapshot = id => ({ groups: [], query: { rows: [{ id }] } });

  runInNewContext(controller, {
    window, document: { addEventListener() {} }, ui: { appReady: true, busy: false },
    $: id => {
      if (!ids.has(id)) ids.set(id, element());
      return ids.get(id);
    },
    elem: element, button: element, operationNames: { poe_off: 'Power OFF' },
    updateControlTarget() {},
    portKey: row => row.id,
    receivePorts: query => rows.push(query.rows[0].id),
    api: async path => {
      if (path === '/api/manual-queue') return { items };
      assert.equal(path, '/api/workspace');
      if (++requests === 1) {
        markStarted();
        return firstSnapshot;
      }
      return snapshot('b');
    },
  });

  const queue = window.manualQueue;
  await queue.refresh();
  items = [{ ...first, state: 'done' }, { ...next, state: 'running' }];
  const slow = queue.refresh();
  await started;
  items = [{ ...first, state: 'done' }, { ...next, state: 'done' }];
  await queue.refresh();
  releaseFirst(snapshot('a'));
  await slow;
  await queue.refresh();
  assert.deepEqual(rows, ['b']);
  assert.equal(requests, 2);
});
