/* Production status must mean authenticated source health, not a recent timestamp alone. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname,'..','web','evidence-board','worker.js'),'utf8');
let receipt;
let upstreamOk = true;
const context = {
  URL, Request, Response, Date, console, fetch: async () => { throw Error('Unexpected external fetch'); },
  addEventListener() {},
  RIVAL_EVIDENCE_RECEIPTS: {
    fetch: async () => ({ok:upstreamOk, status:upstreamOk?200:503, json:async()=>receipt})
  }
};
vm.createContext(context);
new vm.Script(source, {filename:'evidence-board-worker.js'}).runInContext(context,{timeout:3500});
async function status(){
  const response = await vm.runInContext("handle(new Request('https://evidence.the420lobby.com/api/status'))",context);
  return {code:response.status, body:await response.json()};
}
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
  console.log('Evidence Board source-health regressions passed');
}
main().catch(error=>{console.error(error);process.exitCode=1});
