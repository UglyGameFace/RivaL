/* Alert webhook must deliver to Discord: the POST body needs a `content`
   field (Discord rejects a body with no content/embeds with HTTP 400
   "Cannot send an empty message"), while keeping the structured fields
   generic JSON receivers already get. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'evidence-board', 'worker.js'), 'utf8');
const calls = [];
const cacheStore = new Map();
const context = {
  URL, Request, Response, Date, console,
  ALERT_WEBHOOK_URL: 'https://example.invalid/webhook-placeholder',
  fetch: async (url, opts) => { calls.push({url, opts}); return {ok: true}; },
  addEventListener() {},
  caches: {default: {
    match: async (key) => { const v = cacheStore.get(String(key)); return v ? {json: async () => v} : undefined; },
    put: async (key, resp) => { cacheStore.set(String(key), await resp.json()); }
  }},
  RIVAL_EVIDENCE_RECEIPTS: {fetch: async () => { throw Error('Unexpected upstream fetch'); }}
};
vm.createContext(context);
new vm.Script(source, {filename: 'evidence-board-worker.js'}).runInContext(context, {timeout: 3500});

async function send(alerts) {
  await vm.runInContext(`maybeSendAlert(${JSON.stringify(alerts)})`, context);
}
function lastBody() {
  assert.equal(calls.length > 0, true, 'expected the webhook to be called');
  return JSON.parse(calls[calls.length - 1].opts.body);
}

async function main() {
  // The /api/status handler must hand the send to the fetch event's
  // waitUntil; a send that outlives nothing is cancelled with the response.
  assert.ok(source.includes('handle(e.request,e)'),
    'fetch handler must pass the event so alerts can use waitUntil');
  assert.ok(source.includes('event.waitUntil(alertSend)'),
    '/api/status must register the alert send with waitUntil');

  // No alerts -> no POST at all.
  await send([]);
  assert.equal(calls.length, 0, 'empty alert list must not POST');

  // Critical alert -> one POST with both content and structured fields.
  await send([{level: 'critical', code: 'collection_stale'}]);
  assert.equal(calls.length, 1, 'critical alert must POST once');
  assert.equal(calls[0].url, 'https://example.invalid/webhook-placeholder');
  assert.equal(calls[0].opts.method, 'POST');
  assert.equal(calls[0].opts.headers['content-type'], 'application/json');
  let body = lastBody();
  assert.equal(body.service, 'rival-evidence-board');
  assert.equal(body.level, 'critical');
  assert.deepEqual(body.alerts, ['collection_stale']);
  assert.equal(typeof body.at, 'string');
  assert.equal(typeof body.content, 'string');
  assert.ok(body.content.length > 0, 'content must not be empty (Discord 400s on an empty message)');
  assert.ok(body.content.includes('CRITICAL'), 'content must carry the level');
  assert.ok(body.content.includes('collection_stale'), 'content must carry the alert codes');
  assert.ok(body.content.includes(body.at), 'content must carry the timestamp');

  // Same level within the hour -> throttled, no second POST.
  await send([{level: 'critical', code: 'collection_stale'}]);
  assert.equal(calls.length, 1, 'same level within an hour must stay throttled');

  // Different level -> POSTs again, with the new level in the content.
  await send([{level: 'warning', code: 'test_delivery'}]);
  assert.equal(calls.length, 2, 'a new level must POST again');
  body = lastBody();
  assert.equal(body.level, 'warning');
  assert.deepEqual(body.alerts, ['test_delivery']);
  assert.ok(body.content.includes('WARNING'));
  assert.ok(body.content.includes('test_delivery'));

  // Delivery is awaited: the send must not settle before the webhook fetch
  // completes. The 2026-11-17 outage alert was lost because the send was
  // fire-and-forget and the worker finished before Discord was reached.
  let releaseFetch;
  const gate = new Promise((resolve) => { releaseFetch = resolve; });
  const realFetch = context.fetch;
  context.fetch = async (url, opts) => { calls.push({url, opts}); await gate; return {ok: true}; };
  let settled = false;
  const pending = vm.runInContext(
    `maybeSendAlert(${JSON.stringify([{level: 'critical', code: 'delivery_gate'}])})`, context)
    .then(() => { settled = true; });
  await new Promise((resolve) => setTimeout(resolve, 25));
  assert.equal(settled, false, 'alert send must wait for the webhook fetch to complete');
  releaseFetch();
  await pending;
  assert.equal(settled, true, 'alert send must settle once the webhook fetch completes');
  context.fetch = realFetch;

  // Durable dedupe: the in-isolate throttle alone re-sent one continuing
  // alert from every fresh isolate. The worker now asks the collector to
  // check-and-set the alert set's identity before sending.
  const bindingCalls = [];
  let throttleAnswer = {send: false};
  context.RIVAL_EVIDENCE_RECEIPTS = {fetch: async (url, opts) => {
    bindingCalls.push({url, opts});
    return {ok: true, json: async () => throttleAnswer};
  }};
  const beforeDurable = calls.length;
  await send([{level: 'warning', code: 'durable_gate', since: '2026-11-19T00:00:00.000Z'}]);
  assert.equal(calls.length, beforeDurable,
    'collector reports the identity already sent -> no POST from this isolate either');
  assert.equal(bindingCalls.length, 1, 'the throttle must be consulted exactly once per send decision');
  assert.ok(String(bindingCalls[0].url).includes('/alert-throttle'),
    'the throttle call must target the collector alert-throttle route');
  assert.ok(JSON.parse(bindingCalls[0].opts.body).key.includes('durable_gate'),
    'the throttle key must identify the alert set');
  assert.equal(vm.runInContext('alertDiag.consult', context), 'suppressed',
    'the denial must be visible in the delivery diagnostics');
  throttleAnswer = {send: true};
  await send([{level: 'critical', code: 'durable_open', since: '2026-11-19T00:05:00.000Z'}]);
  assert.equal(calls.length, beforeDurable + 1,
    'collector allows the identity -> the alert POSTs');
  assert.equal(bindingCalls.length, 2, 'an allowed send also records the identity first');
  assert.equal(vm.runInContext('alertDiag.consult', context), 'allowed',
    'the approval must be visible in the delivery diagnostics');
  assert.equal(vm.runInContext('BOARD_BUILD', context), '2026-11-19-attempt-gap-1',
    'the build marker must name this build');

  // Consult-failure fallback: when the collector itself is failing (its
  // KV writes died with the daily quota, so every consult 500s), failing
  // open on every poll spammed the channel for hours. The first send of
  // an identity still posts; the fallback records it in the shared cache
  // and a repeat of the SAME identity is suppressed even from a fresh
  // isolate (alertMemory cleared to simulate one), while a different
  // identity still posts.
  context.RIVAL_EVIDENCE_RECEIPTS = {fetch: async () => ({ok: false, status: 500, json: async () => ({})})};
  const clearMemory = () => vm.runInContext('alertMemory={level:null,at:0}', context);
  clearMemory();
  const beforeFallback = calls.length;
  await send([{level: 'warning', code: 'fallback_gate'}]);
  assert.equal(calls.length, beforeFallback + 1,
    'consult failing -> the first send of an identity still posts');
  assert.equal(vm.runInContext('alertDiag.fallback', context), 'recorded',
    'the fallback send must be visible in the delivery diagnostics');
  clearMemory();
  await send([{level: 'warning', code: 'fallback_gate'}]);
  assert.equal(calls.length, beforeFallback + 1,
    'consult failing + fallback record -> a repeat of the same identity is suppressed');
  assert.equal(vm.runInContext('alertDiag.fallback', context), 'suppressed',
    'the fallback suppression must be visible in the delivery diagnostics');
  clearMemory();
  await send([{level: 'warning', code: 'fallback_other'}]);
  assert.equal(calls.length, beforeFallback + 2,
    'a different identity still posts under the fallback');

  // The identity is the alert SET, never a timestamp: a source alert's
  // `since` is its latest failed check and advances on every receipt, so
  // keying on it would re-send at receipt cadence forever.
  const identityOf = expr => vm.runInContext(expr, context);
  const idA = identityOf(`alertIdentity('warning', [{code: 'source_failed:drive', since: '2026-11-19T01:00:00.000Z'}])`);
  const idB = identityOf(`alertIdentity('warning', [{code: 'source_failed:drive', since: '2026-11-19T02:37:00.000Z'}])`);
  assert.equal(idA, idB, 'identity must not rotate when only the since timestamp moves');
  const idC = identityOf(`alertIdentity('warning', [{code: 'source_failed:drive'}, {code: 'source_failed:github'}])`);
  assert.notEqual(idA, idC, 'a changed alert set must be a new identity');
  const idD = identityOf(`alertIdentity('critical', [{code: 'source_failed:drive'}])`);
  assert.notEqual(idA, idD, 'a level change must be a new identity');

  console.log('alert webhook body OK (content + structured fields, throttle intact, delivery awaited, durable dedupe consulted)');
}

main().catch((e) => { console.error(e); process.exit(1); });
