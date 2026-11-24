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

// Exercise the real reservation controller with a transport that loses the POST
// acknowledgement. The only automatic recovery traffic must be read-only.
function receiptHarness(handler, storage=new Map()) {
  const element = () => ({
    dataset: {}, hidden: true, textContent: '', append() {}, replaceChildren() {},
  });
  const ids = new Map(), calls = [], actions = [];
  const window = {addEventListener() {}, portTools: {matchesInventory: () => true}};
  const context = {
    window, document: {addEventListener() {}}, ui: {appReady:true, busy:false},
    sessionStorage: {
      getItem: key => storage.get(key) || null,
      setItem: (key,value) => storage.set(key,value),
      removeItem: key => storage.delete(key),
    },
    $: id => {
      if (!ids.has(id)) ids.set(id,element());
      return ids.get(id);
    },
    elem: element,
    button: (name,fn) => { actions.push({name,fn}); return element(); },
    operationNames: {poe_off:'Power OFF'}, jobNames:{},
    updateControlTarget() {}, notify() {}, delay: async () => {},
    portKey: row => row.id, receivePorts() {},
    setTimeout() {}, clearTimeout() {},
    api: async (path,body) => {
      calls.push({path,body});
      if (path==='/api/manual-queue') return {items:[]};
      if (path==='/api/workspace') return {groups:[]};
      return handler(path,body);
    },
  };
  runInNewContext(controller,context);
  return {queue:window.manualQueue, calls, actions, ids, storage};
}
const requestId='a'.repeat(32);
const powerBody={requestId,operation:'poe_off',targets:[]};
const accepted={id:requestId,accepted:true,state:'queued',wasQueued:true,historyOnly:false};

test('lost acknowledgement and transient reads recover the original ID without a second POST', async () => {
  let reads=0;
  const h=receiptHarness(async (path,body)=>{
    if (body) throw new Error('lost response');
    assert.equal(path,'/api/manual-receipts/'+requestId);
    if (++reads<3) throw new Error('temporarily unavailable');
    return accepted;
  });
  const receipt=await h.queue.submit('control.quick-execute',powerBody);
  assert.equal(receipt.id,requestId);
  assert.equal(receipt.state,'queued');
  assert.equal(receipt.recovered,true);
  assert.equal(reads,3);
  assert.equal(h.calls.filter(c=>c.path==='/api/jobs').length,1);
  assert.equal(h.storage.size,0);
});

test('five missing reads leave a recoverable lock; reload and close never replay the job', async () => {
  const missing=Object.assign(new Error('missing'),{status:404,code:'receipt_not_found'});
  const h=receiptHarness(async (path,body)=>{
    if (body) throw new Error('lost response');
    throw missing;
  });
  await assert.rejects(h.queue.submit('control.quick-execute',powerBody),/접수 미확인/);
  assert.equal(h.calls.filter(c=>c.path.startsWith('/api/manual-receipts/')).length,5);
  await assert.rejects(h.queue.submit('control.quick-execute',{...powerBody,requestId:'b'.repeat(32)}),/이전 요청/);
  assert.equal(h.calls.filter(c=>c.path==='/api/jobs').length,1);
  assert.equal(h.storage.size,1);
  const next=receiptHarness(async (path,body)=>{
    if (path.endsWith('/close')) return {...accepted,accepted:false,state:'not_accepted',historyOnly:true};
    assert.equal(body,undefined);
    throw missing;
  },h.storage);
  next.queue.ready();
  await new Promise(resolve=>setImmediate(resolve));
  const close=next.actions.find(a=>a.name==='미접수 요청 종료');
  assert.ok(close);
  await close.fn();
  assert.equal(next.storage.size,0);
  assert.equal(next.calls.filter(c=>c.path==='/api/jobs').length,0);
  assert.equal(next.calls.filter(c=>c.path.endsWith('/close')).length,1);
});

test('explicit validation or busy rejection releases the local confirmation lock', async () => {
  const conflict=Object.assign(new Error('busy'),{status:409,serverRejection:true,conflicts:['other']});
  const h=receiptHarness(async ()=>{throw conflict;});
  await assert.rejects(h.queue.submit('control.quick-execute',powerBody),/busy/);
  assert.equal(h.storage.size,0);
  assert.equal(h.calls.length,1);
});
