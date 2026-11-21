/* Production status must mean authenticated source health, not a recent timestamp alone. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname,'..','web','evidence-board','worker.js'),'utf8');
let receipt;
let diagOverride = null;
let upstreamOk = true;
const context = {
  URL, Request, Response, Date, console, fetch: async () => { throw Error('Unexpected external fetch'); },
  addEventListener() {},
  RIVAL_EVIDENCE_RECEIPTS: {
    fetch: async (url) => ({ok:upstreamOk, status:upstreamOk?200:503,
      json:async()=>(diagOverride&&String(url).includes('diagnostics'))?diagOverride:receipt})
  }
};
vm.createContext(context);
new vm.Script(source, {filename:'evidence-board-worker.js'}).runInContext(context,{timeout:3500});
async function status(){
  const response = await vm.runInContext("handle(new Request('https://evidence.the420lobby.com/api/status'))",context);
  return {code:response.status, body:await response.json()};
}
async function diagnostics(){
  const response = await vm.runInContext("handle(new Request('https://evidence.the420lobby.com/api/diagnostics'))",context);
  return {code:response.status, body:await response.json()};
}
function sourceNamed(body,name){return body.sources.find(s=>s.name===name)}
function alertCodes(body){return body.alerts.map(a=>a.code)}
async function main(){
  const now = new Date(Date.now()-120000).toISOString();
  receipt = {
    captured_at:now, provenance:{method:'github-actions-oidc',verified:true},
    host:{status:'verified',forecast_status:'fresh',learning_status:'fresh'},
    database:{status:'verified_read'}, drive:{status:'verified_reports'}
  };
  let r=await status();
  assert.equal(r.code,200,'Recent signed and authenticated host/DB reads are checked');
  assert.equal(r.body.data_health,'checked');
  assert.equal(r.body.evidence_recent,true);
  assert.equal(r.body.host_read_verified,true);
  assert.equal(r.body.database_read_verified,true);
  assert.equal(r.body.drive_reports_verified,true);
  assert.equal(r.body.after_cost_qualified_not_inferred,true);
  receipt={...receipt,host:{...receipt.host,status:'unavailable'}};
  r=await status();
  assert.equal(r.code,503,'Recent signed snapshot must not mask host failure');
  assert.equal(r.body.data_health,'partial');
  assert.equal(r.body.evidence_recent,true);
  assert.equal(r.body.host_read_verified,false);
  receipt={...receipt,host:{status:'verified',forecast_status:'stale_or_clock_invalid',learning_status:'fresh'}};
  r=await status();
  assert.equal(r.code,503,'Stale forecast worker is not production health');
  assert.equal(r.body.forecast_fresh,false);
  receipt={...receipt,host:{status:'verified',forecast_status:'fresh',learning_status:'fresh'},
    database:{status:'unavailable'}};
  r=await status();
  assert.equal(r.code,503,'Unverified database cannot inherit green from host');
  assert.equal(r.body.database_read_verified,false);
  receipt={...receipt,database:{status:'verified_read'},
    captured_at:new Date(Date.now()-26*60000).toISOString()};
  r=await status();
  assert.equal(r.code,503,'Old signed receipts must fail closed');
  assert.equal(r.body.data_health,'delayed_or_unverified');
  receipt={...receipt,captured_at:new Date(Date.now()+60000).toISOString()};
  r=await status();
  assert.equal(r.code,503,'Future-dated evidence must fail closed');
  receipt={...receipt,captured_at:now,provenance:{method:'github-actions-oidc',verified:false}};
  r=await status();
  assert.equal(r.code,503,'Unsigned evidence cannot be healthy');
  upstreamOk=false;
  r=await status();
  assert.equal(r.code,503,'Collector outage cannot be interpreted as success');
  assert.equal(r.body.data_health,'unavailable');
  upstreamOk=true;
  // Delivery 1: collection provenance, per-source reliability and alerts.
  const attemptedAt=new Date(Date.now()-6*3600000).toISOString();
  receipt={captured_at:now,provenance:{method:'github-actions-oidc',verified:true},
    collection:{event:'schedule',run_id:'44002956865',run_attempt:1,
      attempted_at:attemptedAt,coalesced:false},
    sources:{
      discloud_forecast:{status:'ok',reason:null,checked_at:now,freshness_seconds:900},
      discloud_learning:{status:'ok',reason:null,checked_at:now,freshness_seconds:900},
      postgres:{status:'ok',reason:null,checked_at:now,freshness_seconds:3600},
      drive:{status:'failed_read',reason:'incomplete_listing',checked_at:now,freshness_seconds:86400},
      github:{status:'ok',reason:null,checked_at:now,freshness_seconds:3600},
      performance:{status:'measured_zero',reason:'no_pairs_measured',checked_at:now,freshness_seconds:3600}},
    host:{status:'verified',forecast_status:'fresh',learning_status:'fresh'},
    database:{status:'verified_read'},drive:{status:'verified_reports'}};
  r=await status();
  assert.equal(r.code,200);
  assert.equal(r.body.collection.last_scheduled_attempt_at,attemptedAt);
  assert.equal(r.body.collection.last_attempt_outcome,'accepted');
  assert.equal(r.body.collection.last_successful_publication_at,now);
  assert.equal(r.body.collection.target_cadence_seconds,600);
  assert.equal(r.body.sources.length,8,'All tracked upstreams are reported');
  assert.equal(sourceNamed(r.body,'drive').status,'failed_read');
  assert.equal(sourceNamed(r.body,'drive').reason,'incomplete_listing');
  assert.equal(sourceNamed(r.body,'drive').fresh,false);
  assert.equal(sourceNamed(r.body,'performance').status,'measured_zero');
  assert.ok(alertCodes(r.body).includes('source_failed:drive'),
    'A failed source stays visible as an alert on a 200 status');
  assert.ok(!alertCodes(r.body).includes('source_failed:performance'),
    'A genuinely measured zero is not a failure');
  assert.ok(!alertCodes(r.body).includes('schedule_gap'),
    'A 6h schedule gap is below the 12h alert bar');
  assert.equal(sourceNamed(r.body,'market_feed').status,'unknown');
  assert.equal(r.body.bindings.evidence_receipts_ok,true);
  assert.equal(r.body.bindings.market_feed_ok,false);
  assert.equal(r.body.snapshot_is_latest_only,true);
  receipt={...receipt,
    collection:{...receipt.collection,
      attempted_at:new Date(Date.now()-13*3600000).toISOString()},
    captured_at:new Date(Date.now()-2*3600000).toISOString()};
  r=await status();
  assert.equal(r.code,503,'A 2h-old receipt is a critical collection failure');
  assert.ok(alertCodes(r.body).includes('collection_stale'));
  assert.ok(alertCodes(r.body).includes('schedule_gap'));
  let d=await diagnostics();
  assert.equal(d.code,200,'Diagnostics stays readable while failures are shown');
  assert.equal(sourceNamed(d.body,'market_feed').status,'binding_missing');
  assert.equal(d.body.checks.missing_bindings,true);
  assert.equal(d.body.checks.stale_receipt,true);
  assert.equal(d.body.checks.invalid_snapshot,false);
  assert.deepEqual(d.body.attempts,[]);
  receipt={...receipt,captured_at:now,
    collection:{...receipt.collection,attempted_at:attemptedAt}};
  d=await diagnostics();
  assert.equal(d.code,200);
  assert.equal(d.body.checks.stale_receipt,false);
  assert.equal(d.body.checks.failed_collection_attempt,false);
  // The gap alert follows the last attempt of ANY trigger: a 20h-old
  // schedule attempt beside a 5-minute-old dispatch attempt is a healthy
  // pipeline, not a gap (the 2026-11-19 production case, where the
  // schedule_gap warning outlived the outage it belonged to).
  const savedReceipt=receipt;
  const recentAttempt=new Date(Date.now()-5*60000).toISOString();
  const oldSchedule=new Date(Date.now()-20*3600000).toISOString();
  diagOverride={collection:{
    last_attempt:{attempted_at:recentAttempt,outcome:'accepted'},
    last_schedule_attempt:{attempted_at:oldSchedule},
    last_publication:{captured_at:now}}};
  receipt={...receipt,captured_at:now,
    collection:{event:'workflow_dispatch',run_id:'44643698748',run_attempt:1,
      attempted_at:new Date(Date.now()-13*3600000).toISOString(),coalesced:false}};
  r=await status();
  assert.equal(r.code,200);
  assert.ok(!alertCodes(r.body).includes('schedule_gap'),
    'A recent attempt of any trigger clears the gap alert');
  assert.ok(r.body.collection.schedule_gap_seconds<3600,
    'Gap is measured from the last attempt, not the last schedule event');
  assert.equal(r.body.collection.last_attempt_at,recentAttempt);
  assert.equal(r.body.collection.last_scheduled_attempt_at,oldSchedule,
    'The raw schedule figure is still reported for Diagnostics');
  diagOverride=null;
  receipt=savedReceipt;
  upstreamOk=false;
  d=await diagnostics();
  assert.equal(d.code,503,'Diagnostics without a snapshot cannot invent health');
  console.log('Evidence Board source-health regressions passed');
}
main().catch(error=>{console.error(error);process.exitCode=1});
