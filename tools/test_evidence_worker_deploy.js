'use strict';
// Local-only deployment contract regressions. Never calls a network or uploads.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { checkBindings, checkSource, uploadBindings, planUpload, EXPECTED } = require('./deploy_evidence_worker.cjs');

const root = path.resolve(__dirname, '..');
assert.deepEqual(Object.keys(EXPECTED).sort(), ['board', 'collector']);
for (const [kind, target] of Object.entries(EXPECTED)) {
  const source = fs.readFileSync(path.join(root, target.file), 'utf8');
  checkSource(source, target);
  checkBindings(target.bindings, target.bindings);
  checkBindings(target.bindings.slice().reverse(), target.bindings);
  assert.throws(() => checkBindings([], target.bindings), /bindings differ/);
  assert.throws(() => checkBindings([...target.bindings,
    { type: 'secret_text', name: 'EXPOSE_PRIVATE_SECRETS' }], target.bindings),
  /bindings differ/);
  assert.throws(() => checkSource(source.slice(0, 100), target), /truncated/);
  assert.ok(!target.worker.includes('public-site'),
    'Public RivaL market site is never part of this deploy automation');
  if (kind === 'collector') {
    assert.throws(() => checkBindings([{ ...target.bindings[0],
      namespace_id: 'wrong-namespace' }], target.bindings), /bindings differ/);
  } else {
    assert.throws(() => checkBindings([{ ...target.bindings[0],
      service: 'rival-public-site' }, target.bindings[1]], target.bindings),
    /bindings differ/);
  }
}
// The board may additionally carry the owner's ALERT_WEBHOOK_URL secret:
// deploys must preserve it once set, must not require it before it is set,
// and must still refuse any other extra binding (or the same name as a
// plain-text variable — the webhook URL is a credential).
const board = EXPECTED.board;
assert.ok(Array.isArray(board.optionalBindings));
assert.deepEqual(board.optionalBindings, [{ type: 'secret_text', name: 'ALERT_WEBHOOK_URL' }]);
checkBindings([...board.bindings, ...board.optionalBindings],
  board.bindings, board.optionalBindings);
checkBindings(board.bindings, board.bindings, board.optionalBindings);
assert.throws(() => checkBindings([...board.bindings, ...board.optionalBindings,
  { type: 'secret_text', name: 'EXPOSE_PRIVATE_SECRETS' }],
  board.bindings, board.optionalBindings), /bindings differ/);
assert.throws(() => checkBindings([...board.bindings,
  { type: 'plain_text', name: 'ALERT_WEBHOOK_URL', text: 'x' }],
  board.bindings, board.optionalBindings), /bindings differ/);
assert.throws(() => checkBindings([...board.bindings, ...board.optionalBindings],
  board.bindings), /bindings differ/);

// Upload translation (build aaee7f79, 2026-11-19): the settings API
// returns the owner's secret without a value, and an upload carrying a
// text-less secret_text is rejected (HTTP 400 / error 10021) — the exact
// failure that froze board deploys after the dashboard set the secret.
// The upload payload must carry the secret as an inherit directive and
// leave every other binding byte-identical.
const storedBoard = [...board.bindings, ...board.optionalBindings];
assert.deepEqual(uploadBindings(storedBoard), [
  ...board.bindings, { type: 'inherit', name: 'ALERT_WEBHOOK_URL' }],
  'a stored secret must upload as inherit, everything else unchanged');
assert.deepEqual(uploadBindings(board.bindings), board.bindings,
  'with no secret set, the upload payload is the stored bindings verbatim');
assert.deepEqual(
  uploadBindings([{ type: 'secret_text', name: 'NEW_SECRET', text: 'v' }]),
  [{ type: 'secret_text', name: 'NEW_SECRET', text: 'v' }],
  'a secret carrying a value is an explicit set and passes through');
assert.deepEqual(uploadBindings(EXPECTED.collector.bindings),
  EXPECTED.collector.bindings,
  'the collector payload is untouched by the translation');

// The collector may additionally carry the alert-throttle Durable
// Object binding: created by the upload itself the first time it is
// missing (planUpload appends it with a one-time SQLite class
// migration), preserved once it exists, and refused under any other
// class name — the binding names the class the collector exports.
const collector = EXPECTED.collector;
assert.ok(Array.isArray(collector.optionalBindings));
assert.deepEqual(collector.optionalBindings,
  [{ type: 'durable_object_namespace', name: 'ALERT_THROTTLE', class_name: 'AlertThrottleDO' }]);
checkBindings(collector.bindings, collector.bindings, collector.optionalBindings);
checkBindings([...collector.bindings, ...collector.optionalBindings],
  collector.bindings, collector.optionalBindings);
assert.throws(() => checkBindings([...collector.bindings,
  { type: 'durable_object_namespace', name: 'ALERT_THROTTLE', class_name: 'OtherClass' }],
  collector.bindings, collector.optionalBindings), /bindings differ/);
// Extra returned fields on a Durable Object binding are tolerated (its
// contract is the identity fields, all compared by bindingKey); a
// binding missing its class name does not match the contract at all.
checkBindings([...collector.bindings,
  { type: 'durable_object_namespace', name: 'ALERT_THROTTLE', class_name: 'AlertThrottleDO', script_name: 'rival-evidence-collector' }],
  collector.bindings, collector.optionalBindings);
assert.throws(() => checkBindings([...collector.bindings,
  { type: 'durable_object_namespace', name: 'ALERT_THROTTLE' }],
  collector.bindings, collector.optionalBindings), /bindings differ/);
// Production read-back shape (build 8f231a66, 2026-11-20): once the
// object exists, the settings API returns its binding carrying the
// Cloudflare-assigned namespace id and the owning script's name.
// That shape must satisfy the contract — the first Durable Object
// deploy landed and served, then this check refused it — while the
// same shape under another class name is still refused.
const storedCollector = [...collector.bindings,
  { type: 'durable_object_namespace', name: 'ALERT_THROTTLE',
    class_name: 'AlertThrottleDO', script_name: 'rival-evidence-collector',
    namespace_id: 'b3b0c1a2d4e5f60718293a4b5c6d7e8f' }];
checkBindings(storedCollector, collector.bindings, collector.optionalBindings);
assert.throws(() => checkBindings([...collector.bindings,
  { type: 'durable_object_namespace', name: 'ALERT_THROTTLE', class_name: 'OtherClass',
    script_name: 'rival-evidence-collector', namespace_id: 'b3b0c1a2d4e5f60718293a4b5c6d7e8f' }],
  collector.bindings, collector.optionalBindings), /bindings differ/);
// The steady-state plan for the stored shape: the binding is
// recognised as the declared one (no duplicate append, no repeated
// migration) and rides in the upload in its declared shape.
const storedPlan = planUpload(storedCollector, collector);
assert.deepEqual(storedPlan.bindings,
  [...collector.bindings, ...collector.optionalBindings],
  'a stored Durable Object binding uploads in its declared shape');
assert.equal(storedPlan.migrations, null,
  'no migration is sent for the stored Durable Object binding');

const bootstrapPlan = planUpload(collector.bindings, collector);
assert.deepEqual(bootstrapPlan.bindings,
  [...collector.bindings, ...collector.optionalBindings],
  'a missing Durable Object binding is appended to the upload');
assert.deepEqual(bootstrapPlan.migrations, { new_sqlite_classes: ['AlertThrottleDO'] },
  'the first upload carries the one-time SQLite class migration');
const existingPlan = planUpload([...collector.bindings, ...collector.optionalBindings], collector);
assert.deepEqual(existingPlan.bindings,
  [...collector.bindings, ...collector.optionalBindings],
  'an existing Durable Object binding is carried forward verbatim');
assert.equal(existingPlan.migrations, null,
  'no migration is sent once the class exists');
const boardPlan = planUpload(board.bindings, board);
assert.deepEqual(boardPlan.bindings, board.bindings,
  'the board upload plan is unchanged');
assert.equal(boardPlan.migrations, null, 'the board never gains migrations');

console.log('Evidence Worker offline deployment contract tests passed');
