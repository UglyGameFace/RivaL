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
console.log('Evidence Worker offline deployment contract tests passed');
