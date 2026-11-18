'use strict';
// Local-only deployment contract regressions. Never calls a network or uploads.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { checkBindings, checkSource, EXPECTED } = require('./deploy_evidence_worker.cjs');

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

console.log('Evidence Worker offline deployment contract tests passed');
