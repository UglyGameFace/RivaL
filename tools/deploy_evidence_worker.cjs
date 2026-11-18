#!/usr/bin/env node
/**
 * Deploy exactly one existing RivaL Evidence Worker from the checked-out source.
 * No dependencies; intended for Cloudflare Workers Builds after a build token has
 * been configured. Never runs against RivaL's separate public trading site.
 */
'use strict';

const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');

const ROOT = path.resolve(__dirname, '..');
const EXPECTED_DATE = '2026-11-01';
const EXPECTED = Object.freeze({
  board: {
    worker: 'rival-evidence-board',
    file: 'web/evidence-board/worker.js',
    module: false,
    bindings: [
      { type: 'service', name: 'RIVAL_EVIDENCE_RECEIPTS',
        service: 'rival-evidence-collector', environment: 'production' },
      { type: 'service', name: 'RIVAL_MARKET_FEED',
        service: 'rival-public-site', environment: 'production' }
    ],
    // Optional: set once by the account owner as a Worker Secret in the
    // Cloudflare dashboard. Deploys must preserve it once set, must not
    // require it before it is set, and must accept no other extra binding.
    // A plain-text variable of the same name is NOT accepted: the webhook
    // URL is a credential and is stored as a secret or not at all.
    optionalBindings: [
      { type: 'secret_text', name: 'ALERT_WEBHOOK_URL' }
    ]
  },
  collector: {
    worker: 'rival-evidence-collector',
    file: 'web/evidence-board/collector.js',
    module: true,
    bindings: [
      { type: 'kv_namespace', name: 'EVIDENCE_KV',
        namespace_id: 'aff5366d8e0f4529b89fc6dc89729c0c' }
    ]
  }
});

function bindingKey(binding) {
  return [binding.type, binding.name, binding.service || '',
    binding.environment || '', binding.namespace_id || ''].join(':');
}
function checkBindings(actual, expected, optional = []) {
  assert.ok(Array.isArray(actual), 'Remote Worker bindings unavailable');
  const allowed = [...expected, ...optional];
  const actualKeys = actual.map(bindingKey);
  const occurrences = (key) => actualKeys.filter(k => k === key).length;
  const requiredOk = expected.every(spec => occurrences(bindingKey(spec)) === 1);
  const noExtras = actualKeys.every(key =>
    allowed.some(spec => bindingKey(spec) === key));
  const noDupes = new Set(actualKeys).size === actualKeys.length;
  assert.ok(requiredOk && noExtras && noDupes,
    'Deployed Worker bindings differ from the known production contract; refusing upload');
  for (const binding of actual) {
    const spec = allowed.find(candidate => bindingKey(candidate) === bindingKey(binding));
    assert.deepEqual(Object.keys(binding).sort(), Object.keys(spec).sort(),
      'Unrecognized Worker binding configuration; refusing upload');
  }
}
function checkSource(source, target) {
  assert.ok(source.length > (target.module ? 15000 : 160000), 'Worker source unexpectedly truncated');
  if (target.module) {
    assert.ok(source.includes('export default {'), 'Collector module entrypoint missing');
    assert.ok(source.includes("'schedule','workflow_dispatch','push','workflow_run'"),
      'Collector would reject trusted release-triggered signed receipts');
  } else {
    assert.ok(source.includes("addEventListener('fetch'"), 'Classic Worker fetch listener missing');
    assert.ok(source.includes('RIVAL_MARKET_FEED') && source.includes('RIVAL_EVIDENCE_RECEIPTS'),
      'Existing public market and signed evidence service bindings required');
  }
}

async function api(url, token, options = {}) {
  const res = await fetch(url, {
    ...options, headers: { Authorization: 'Bearer ' + token, ...options.headers }
  });
  if (!res.ok) throw new Error('Cloudflare API returned HTTP ' + res.status +
    ' for ' + (options.method || 'GET'));
  if (options.raw) return res.text();
  const data = await res.json();
  if (!data || data.success !== true) {
    throw new Error('Cloudflare API rejected the requested operation');
  }
  return data.result;
}

async function deploy(targetKey, dryRun) {
  const target = EXPECTED[targetKey];
  assert.ok(target, 'Usage: node tools/deploy_evidence_worker.cjs board|collector [--check]');
  const source = await fs.readFile(path.join(ROOT, target.file), 'utf8');
  checkSource(source, target);
  if (dryRun) {
    console.log('Offline source validation passed: ' + target.worker);
    return;
  }

  // Workers Builds must perform deployments from the trusted production ref.
  if (process.env.WORKERS_CI_BRANCH && process.env.WORKERS_CI_BRANCH !== 'main') {
    throw new Error('Only the main branch can deploy production Workers');
  }
  const token = process.env.CLOUDFLARE_API_TOKEN;
  const account = process.env.CLOUDFLARE_ACCOUNT_ID;
  assert.ok(token && /^[a-f0-9]{32}$/.test(account || ''),
    'A Cloudflare Builds user API token and account ID are required');
  const base = 'https://api.cloudflare.com/client/v4/accounts/' + account +
    '/workers/scripts/' + target.worker;
  const settings = await api(base + '/settings', token, { method: 'GET' });
  assert.equal(settings.compatibility_date, EXPECTED_DATE,
    'Worker compatibility date changed; review before deploying');
  checkBindings(settings.bindings, target.bindings, target.optionalBindings);
  assert.ok(Array.isArray(settings.compatibility_flags) &&
    settings.compatibility_flags.length === 0, 'Unreviewed compatibility flags');
  assert.ok(!settings.logpush && Object.keys(settings.placement || {}).length === 0,
    'Unreviewed Cloudflare placement/log configuration');
  assert.ok(!settings.tail_consumers?.length, 'Unreviewed tail consumers');
  assert.ok(!settings.tags?.length, 'Unreviewed Worker tags');

  const filename = target.module ? 'collector.js' : 'worker.js';
  const metadata = {
    compatibility_date: EXPECTED_DATE,
    bindings: settings.bindings,
    [target.module ? 'main_module' : 'body_part']: filename
  };
  const form = new FormData();
  form.append('metadata', new Blob([JSON.stringify(metadata)], { type: 'application/json' }));
  form.append(filename, new Blob([source], {
    type: target.module ? 'application/javascript+module' : 'application/javascript'
  }), filename);

  await api(base, token, { method: 'PUT', body: form });
  const deployed = await api(base, token, { method: 'GET', raw: true });
  // Module GET may be a multipart source payload; exact JS must occur within it.
  const same = target.module ? deployed.includes(source) : deployed === source;
  assert.ok(same, 'Post-deploy source verification failed');
  const after = await api(base + '/settings', token, { method: 'GET' });
  assert.equal(after.compatibility_date, EXPECTED_DATE);
  checkBindings(after.bindings, target.bindings, target.optionalBindings);
  console.log('Cloudflare source and production bindings verified: ' + target.worker);
}

if (require.main === module) {
  const args = process.argv.slice(2);
  deploy(args[0], args.includes('--check')).catch(err => {
    // Never print source, headers, tokens or Cloudflare response bodies.
    console.error('Evidence Worker deployment refused: ' + err.message);
    process.exitCode = 1;
  });
}
module.exports = { checkBindings, checkSource, EXPECTED };
