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
const context = {
  URL, Request, Response, Date, console,
  ALERT_WEBHOOK_URL: 'https://example.invalid/webhook-placeholder',
  fetch: async (url, opts) => { calls.push({url, opts}); return {ok: true}; },
  addEventListener() {},
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

  console.log('alert webhook body OK (content + structured fields, throttle intact, delivery awaited)');
}

main().catch((e) => { console.error(e); process.exit(1); });
