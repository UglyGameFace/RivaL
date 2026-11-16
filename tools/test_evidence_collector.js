/* Exercise the actual receiver, including real RSA signature verification. */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {webcrypto}=require('node:crypto');
const source=fs.readFileSync(path.join(__dirname,'..','web/evidence-board/collector.js'),'utf8');

async function main(){
 const keys=await webcrypto.subtle.generateKey({name:'RSASSA-PKCS1-v1_5',
  modulusLength:2048,publicExponent:new Uint8Array([1,0,1]),hash:'SHA-256'},true,['sign','verify']);
 const jwk={...await webcrypto.subtle.exportKey('jwk',keys.publicKey),kid:'fixture',use:'sig'};
 const cx={Request,Response,URL,Date,TextDecoder,TextEncoder,Uint8Array,atob,crypto:webcrypto,
  fetch:async url=>{
   assert.equal(url,'https://token.actions.githubusercontent.com/.well-known/jwks');
   return Response.json({keys:[jwk]});
  }};
 vm.createContext(cx);
 vm.runInContext(source.replace('export default {','globalThis.worker = {'),cx);
 const now=new Date().toISOString(),epoch=Math.floor(Date.now()/1000);
 const claims={iss:'https://token.actions.githubusercontent.com',
  aud:'https://rival-evidence-collector.staytrippynlw.workers.dev',repository:'UglyGameFace/RivaL',
  ref:'refs/heads/main',workflow_ref:'UglyGameFace/RivaL/.github/workflows/evidence-board-receipt.yml@refs/heads/main',
  event_name:'schedule',iat:epoch,exp:epoch+600,run_id:'123456789'};
 const sign=async c=>{
  const encode=x=>Buffer.from(JSON.stringify(x)).toString('base64url');
  const message=encode({alg:'RS256',kid:'fixture'})+'.'+encode(c);
  const signature=await webcrypto.subtle.sign('RSASSA-PKCS1-v1_5',keys.privateKey,new TextEncoder().encode(message));
  return message+'.'+Buffer.from(signature).toString('base64url');
 };
 const issued=new Date(Date.now()-120000).toISOString();
 const target=new Date(Date.now()+180000).toISOString();
 const oldTarget=new Date(Date.now()-120000).toISOString();
 const olderIssue=new Date(Date.now()-300000).toISOString();
 const payload={schema_version:1,captured_at:now,host:{
  recent_forward_calls:{status:'observed',captured_at:now,window_hours:6,not_trading_calls:true,
   rows:[{asset:'BTC',horizon:5,venue:'okx',quote:'USDT',issued_at:issued,target_close_at:target,
    probability_up:0.62,baseline_up:0.52,direction:'UP',status:'open',outcome_up:null,
    lead_seconds:300,model_version:'prospective-logistic-persistent-v3',
    raw_contract:'PRIVATE',weights_sha256:'PRIVATE',discord_user_id:'PRIVATE'},
    {asset:'SOL',horizon:5,venue:'okx',quote:'USDT',issued_at:olderIssue,target_close_at:oldTarget,
     probability_up:0.51,baseline_up:0.49,direction:'UP',status:'ungraded',outcome_up:null,
     lead_seconds:180,contract:'PRIVATE',weights_sha256:'PRIVATE'},
    {asset:'ETH',horizon:5,venue:'bad/PRIVATE',quote:'USD',issued_at:issued,target_close_at:target,
     probability_up:0.6,baseline_up:0.5,direction:'UP',status:'open',outcome_up:null,lead_seconds:300}]},
  xmarket_stage:'waiting_for_completed_day_export',
  live_call_delivery:{status:'recorded',active_channels:0,
   recent_accepted_delivery_rows:0,advance_model:'advance-candle-logistic-lead5-v1',
   guild_id:'PRIVATE'},
  kucoin_gap_recovery:{status:'recorded',assets_observed:1,
   queued_exact_gaps_14d:2,due_exact_gaps_14d:1,
   attempted_exact_gaps_14d:1,bulk_upstream_empty_assets:1,contract:'PRIVATE'},
  xmarket_exports:{recent_completed_days_pending:4,eligible_before:now,
   published:[{asset:'BTC',exported_days:1,latest_exported_day:'2026-11-12',drive_file_id:'PRIVATE'}],
   pending_by_asset:[{asset:'ETH',days:1}],password:'PRIVATE'}},
  drive:{reports:[{asset:'BTC',horizon:5,reason:'insufficient_exact_pairs'}]},
  performance:{status:'observed',captured_at:now,sampling_policy:'per-asset-horizon-close-v3',
   scopes:[{asset:'BTC',horizon:5,model_version:'prospective-logistic-persistent-v3',
    paired_outcomes:100,model_brier:.2,baseline_brier:.3,unique_target_windows:20,venue_count:5,
    within_target_candle_pairs:100,minimum_lead_seconds:180,outcome_clock:'target_5m_candle_close'}]}};
 const records=new Map();
 const env={EVIDENCE_KV:{get:async key=>records.has(key)?JSON.parse(records.get(key)):null,
  put:async(key,value)=>{records.set(key,value);}}};
 const make=(token,body=payload)=>new Request(claims.aud+'/ingest',{method:'POST',
  headers:token?{Authorization:'Bearer '+token}:{},body:JSON.stringify(body)});
 assert.equal((await cx.worker.fetch(make(null),env)).status,401);
 assert.equal((await cx.worker.fetch(make(await sign({...claims,ref:'refs/heads/other'})),env)).status,403);
 const valid=await sign(claims);
 const trustedRelease=await cx.verifyGithubOIDC(await sign({
  ...claims,event_name:'workflow_run',run_id:'123456790'}));
 assert.equal(trustedRelease.runId,'123456790',
  'Trusted main post-release publisher should be eligible for signed receipt');
 await assert.rejects(cx.verifyGithubOIDC(await sign({
  ...claims,event_name:'pull_request',run_id:'123456791'})),/claim_mismatch/,
  'Untrusted PR event must not bypass signed collector');

 const parts=valid.split('.');
 const corrupted=Buffer.from(parts[2],'base64url');corrupted[0]^=1;parts[2]=corrupted.toString('base64url');
 assert.equal((await cx.worker.fetch(make(parts.join('.')),env)).status,403);
 assert.equal(records.size,0,'Unverified callers must never change receipts');
 assert.equal((await cx.worker.fetch(make(valid),env)).status,202);
 const receipt=JSON.parse(records.get('latest'));
 assert.equal(receipt.provenance.verified,true);
 assert.equal(receipt.host.recent_forward_calls.status,'observed');
 assert.equal(receipt.host.recent_forward_calls.rows.length,2);
 assert.equal(receipt.host.recent_forward_calls.rows[1].asset,'SOL');
 assert.equal(receipt.host.recent_forward_calls.rows[1].status,'ungraded');
 assert.equal(receipt.host.recent_forward_calls.rows[1].outcome_up,null);
 assert.equal(receipt.host.recent_forward_calls.rows[0].probability_up,0.62);
 assert.equal(receipt.host.recent_forward_calls.rows[0].direction,'UP');
 assert.equal(receipt.host.recent_forward_calls.rows[0].outcome_up,null);
 assert.ok(!Object.hasOwn(receipt.host.recent_forward_calls.rows[0],'raw_contract'));

 assert.equal(receipt.host.xmarket_exports.recent_completed_days_pending,4);
 assert.equal(receipt.host.xmarket_stage,'waiting_for_completed_day_export');
 assert.equal(receipt.host.live_call_delivery.active_channels,0);
 assert.equal(receipt.host.live_call_delivery.recent_accepted_delivery_rows,0);
 assert.equal(receipt.host.live_call_delivery.advance_model,'advance-candle-logistic-lead5-v1');
 assert.ok(!Object.hasOwn(receipt.host.live_call_delivery,'guild_id'));
 assert.equal(receipt.host.live_call_delivery.delivery_is_discord_api_acceptance_only,true);
 assert.equal(receipt.host.kucoin_gap_recovery.queued_exact_gaps_14d,2);
 assert.equal(receipt.host.kucoin_gap_recovery.due_exact_gaps_14d,1);
 assert.equal(receipt.host.kucoin_gap_recovery.does_not_prove_source_recovery,true);
 assert.ok(!Object.hasOwn(receipt.host.kucoin_gap_recovery,'contract'));
 assert.equal(receipt.host.xmarket_exports.published[0].latest_exported_day,'2026-11-12');
 assert.equal(receipt.drive.reports[0].reason,'insufficient_exact_pairs');
 assert.equal(receipt.performance.model,'prospective-logistic-persistent-v3');
 assert.equal(receipt.performance.status,'observed');
 assert.equal(receipt.performance.after_cost_qualified,false,
  'The scoring model name does not authorize a trading edge');
 assert.equal(receipt.performance.scopes[0].horizon,5);
 assert.equal(receipt.performance.scopes[0].unique_target_windows,20);
 assert.equal(receipt.performance.sampling_policy,'per-asset-horizon-close-v3');
 assert.equal(receipt.performance.scopes[0].minimum_lead_seconds,180);
 assert.equal(receipt.performance.scopes[0].within_target_candle_pairs,100);
 assert.equal(receipt.performance.after_cost_qualified,false);
 assert.ok(!records.get('latest').includes('PRIVATE'));
 assert.equal(records.get('run:'+claims.run_id),records.get('latest'));
 const unavailable=cx.build({schema_version:1,captured_at:now},{runId:'123456789'});
 assert.equal(unavailable.host.xmarket_exports,null);
 assert.equal(unavailable.host.recent_forward_calls.status,'read_unavailable');
 assert.equal(unavailable.host.xmarket_imported_scopes,null);
 assert.equal(unavailable.performance.status,'unavailable');
 assert.equal(unavailable.performance.scopes.length,0,
  'A model ID must not be mistaken for an observed scored outcome');
 console.log('Evidence collector signed round-trip, redaction and rejection regressions passed');
}
main().catch(error=>{console.error(error);process.exitCode=1});
