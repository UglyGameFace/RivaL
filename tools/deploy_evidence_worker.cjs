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
    // Upload note (proven 2026-11-19, build aaee7f79): the settings API
    // returns this binding WITHOUT its value, and re-sending it verbatim
    // in an upload is rejected (HTTP 400, error 10021 'invalid or missing
    // text property') — every board deploy failed at the PUT from the
    // dashboard secret deploy (2026-11-18) until uploadBindings() below
    // translated it to an inherit directive.
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
    ],
    // Optional: the alert-throttle Durable Object binding is created by
    // the upload itself (planUpload appends it with a one-time SQLite
    // class migration) the first time it is missing, and must be
    // preserved once it exists. The collector falls back to its KV
    // check-and-set whenever the binding is absent, so either state is
    // a working production state.
    optionalBindings: [
      { type: 'durable_object_namespace', name: 'ALERT_THROTTLE',
        class_name: 'AlertThrottleDO' },
      // The D1 database for the KV migration (2026-11-22): created by
      // the account owner in the dashboard and bound as EVIDENCE_DB.
      // Optional in this contract because the collector works with or
      // without it (without it, storage stays on KV — the pre-migration
      // state), but once present it must be preserved exactly: the
      // 2026-11-22 collector build failed closed here because this
      // binding was unknown to the contract.
      { type: 'd1', name: 'EVIDENCE_DB',
        database_id: 'ba3d9030-9c4a-4dda-a2c7-aef618686682' }
    ]
  }
});

function bindingKey(binding) {
  if (binding.type === 'durable_object_namespace') {
    // A Durable Object binding's identity is the class it points at.
    // The settings API reports Cloudflare-assigned facts about the
    // namespace alongside it (its namespace id, the owning script's
    // name); those are read-back state, not part of the contract the
    // upload declares, so they never enter the key. (First DO deploy,
    // build 8f231a66 on 2026-11-20: the upload landed and the object
    // served live, but this check then refused the deployment because
    // the returned binding's key carried the assigned namespace id
    // and could not match the declared spec.)
    return [binding.type, binding.name, binding.class_name || ''].join(':');
  }
  return [binding.type, binding.name, binding.service || '',
    binding.environment || '', binding.namespace_id || '',
    binding.database_id || '',
    binding.class_name || ''].join(':');
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
    if (spec.type === 'durable_object_namespace' || spec.type === 'd1') {
      // The settings API's returned field set for Durable Object
      // bindings has not been observed in this account yet; the
      // identity fields (type, name, class_name — all compared by
      // bindingKey above) are the contract, so extra returned fields
      // are tolerated for this type only.
      assert.ok(Object.keys(spec).every(key => key in binding),
        'Platform-assigned binding is missing a contract field; refusing upload');
      continue;
    }
    assert.deepEqual(Object.keys(binding).sort(), Object.keys(spec).sort(),
      'Unrecognized Worker binding configuration; refusing upload');
  }
}
/* Bindings as an upload must carry them. The settings API never returns
   secret values, so a stored secret_text binding arrives text-less; the
   upload API rejects a text-less secret_text outright (HTTP 400, error
   10021). The supported carry-forward is an inherit directive naming the
   binding, which keeps the existing value on the new version — the same
   mechanism Wrangler uses for dashboard-set secrets. A secret_text that
   DOES carry a value is an explicit set and passes through untouched, as
   does every non-secret binding. Checks keep using the stored shape;
   only the upload payload is translated. */
function uploadBindings(bindings) {
  return bindings.map(binding =>
    binding && binding.type === 'secret_text' && typeof binding.text !== 'string'
      ? { type: 'inherit', name: binding.name }
      : binding);
}

/* Compute the upload payload's bindings and any one-time migrations.
   A declared Durable Object binding that production does not have yet
   is appended to the upload with a new_sqlite_classes migration (the
   SQLite storage backend is the free-plan one); once it exists it is
   carried forward like every other binding and no migration is sent
   again. */
function planUpload(settingsBindings, target) {
  // A stored Durable Object binding comes back from the settings API
  // carrying Cloudflare-assigned fields (namespace id, script name);
  // the upload declares the binding in its contract shape — type,
  // name and class — exactly as the first, migration-carrying upload
  // did. Every other binding passes through uploadBindings unchanged.
  const bindings = uploadBindings(settingsBindings).map(binding => {
    if (binding && binding.type === 'durable_object_namespace') {
      return { type: binding.type, name: binding.name, class_name: binding.class_name };
    }
    if (binding && binding.type === 'd1') {
      // Same treatment as the Durable Object above: the settings API
      // may report account-assigned facts (e.g. the database name)
      // alongside a D1 binding; the upload declares only the contract
      // shape — type, name, database id.
      return { type: binding.type, name: binding.name, database_id: binding.database_id };
    }
    return binding;
  });
  const newClasses = [];
  for (const spec of target.optionalBindings || []) {
    if (spec.type !== 'durable_object_namespace') continue;
    if (!bindings.some(binding => bindingKey(binding) === bindingKey(spec))) {
      bindings.push({ type: spec.type, name: spec.name, class_name: spec.class_name });
      newClasses.push(spec.class_name);
    }
  }
  return {
    bindings,
    migrations: newClasses.length ? { new_sqlite_classes: newClasses } : null
  };
}

function checkSource(source, target) {
  assert.ok(source.length > (target.module ? 15000 : 160000), 'Worker source unexpectedly truncated');
  if (target.module) {
    assert.ok(source.includes('export default {'), 'Collector module entrypoint missing');
    assert.ok(source.includes('export class AlertThrottleDO'),
      'Collector alert-throttle Durable Object class missing');
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
  const plan = planUpload(settings.bindings, target);
  const metadata = {
    compatibility_date: EXPECTED_DATE,
    bindings: plan.bindings,
    ...(plan.migrations ? { migrations: plan.migrations } : {}),
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
module.exports = { checkBindings, checkSource, uploadBindings, planUpload, EXPECTED };
