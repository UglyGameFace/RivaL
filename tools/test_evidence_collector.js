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
 const opened=new Date(Date.now()-120000).toISOString();
 const oldTarget=new Date(Date.now()-120000).toISOString();
 const oldOpened=new Date(Date.now()-420000).toISOString();
 const olderIssue=new Date(Date.now()-300000).toISOString();
 const payload={schema_version:1,captured_at:now,
  collection:{event:'schedule',run_id:'123456789',run_attempt:1,
   attempted_at:new Date(Date.now()-60000).toISOString(),coalesced:false,
   run_url:'PRIVATE'},
  sources:{
   discloud_forecast:{status:'stale',reason:'stale_or_clock_invalid',checked_at:now,freshness_seconds:900},
   discloud_learning:{status:'ok',reason:null,checked_at:now,freshness_seconds:900},
   postgres:{status:'ok',reason:null,checked_at:now,freshness_seconds:3600},
   drive:{status:'failed_read',reason:'incomplete_listing',checked_at:now,freshness_seconds:86400},
   github:{status:'not_configured',reason:'credentials_not_configured',checked_at:now,freshness_seconds:3600},
   intelligence:{status:'ok',reason:null,checked_at:now,freshness_seconds:3600},
   intelligence_host:{status:'ok',reason:null,checked_at:now,freshness_seconds:3600},
   performance:{status:'measured_zero',reason:'no_pairs_measured',checked_at:now,freshness_seconds:3600},
   attacker_source:{status:'ok',reason:'PRIVATE',checked_at:now},
   bad_reason:{status:'ok',reason:'DROP TABLE',checked_at:now}},
  host:{
  recent_forward_calls:{status:'observed',captured_at:now,window_hours:6,not_trading_calls:true,
   rows:[{asset:'BTC',horizon:5,venue:'okx',quote:'USDT',issued_at:issued,
    target_opened_at:opened,target_close_at:target,settled_at:null,
    probability_up:0.62,baseline_up:0.52,direction:'UP',status:'open',outcome_up:null,
    lead_seconds:300,model_version:'prospective-logistic-persistent-v3',
    raw_contract:'PRIVATE',weights_sha256:'PRIVATE',discord_user_id:'PRIVATE'},
    {asset:'SOL',horizon:5,venue:'okx',quote:'USDT',issued_at:olderIssue,
     target_opened_at:oldOpened,target_close_at:oldTarget,settled_at:null,
     probability_up:0.51,baseline_up:0.49,direction:'UP',status:'ungraded',outcome_up:null,
     lead_seconds:180,contract:'PRIVATE',weights_sha256:'PRIVATE'},
    {asset:'ETH',horizon:5,venue:'bad/PRIVATE',quote:'USD',issued_at:issued,target_close_at:target,
     probability_up:0.6,baseline_up:0.5,direction:'UP',status:'open',outcome_up:null,lead_seconds:300}]},
  xmarket_stage:'waiting_for_completed_day_export',
  xmarket_scope_progress:[
   {asset:'BTC',horizon:15,status:'shadow_issuing',stored_artifacts:2,
    shadow_rows_24h:99,latest_shadow_at:now,latest_imported_at:now,
    fresh_native_sources:2,missing_native_sources:[],stale_native_sources:[],
    model_weights:'PRIVATE',guild_id:'PRIVATE'},
   {asset:'ETH',horizon:60,status:'waiting_for_fit',stored_artifacts:0,
    shadow_rows_24h:0,latest_shadow_at:null,latest_imported_at:null,
    fresh_native_sources:1,missing_native_sources:['kalshi','PRIVATE'],
    stale_native_sources:['polymarket'],private_funding_key:'PRIVATE'},
   {asset:'DOGE',horizon:5,status:'shadow_issuing',model_weights:'PRIVATE'}
  ],
  live_call_delivery:{status:'recorded',active_channels:0,
   recent_accepted_delivery_rows:0,advance_model:'advance-candle-logistic-lead5-v1',
   guild_id:'PRIVATE'},
  kucoin_gap_recovery:{status:'recorded',assets_observed:1,
   queued_exact_gaps_14d:2,due_exact_gaps_14d:1,
   attempted_exact_gaps_14d:1,bulk_upstream_empty_assets:1,contract:'PRIVATE'},
  pending_backlog:{status:'recorded',pending_paths_14d:240,
   due_pending_paths_14d:40,unique_due_targets_14d:20,contract:'PRIVATE'},
  cycle_diagnostics:{status:'recorded',cycles_24h:288,
   mean_selected_streams_24h:33.6,latest_selected_streams:34,
   mean_target_expired_24h:0.5,latest_target_expired:0,
   mean_duplicate_forecasts_24h:1,latest_duplicate_forecasts:0,
   mean_insufficient_training_24h:1.5,latest_insufficient_training:3,
   mean_stale_last_candle_24h:0.5,latest_stale_last_candle:0,
   mean_short_history_24h:1,latest_short_history:2,
   latest_issued_forecasts:96,contract:'PRIVATE'},
  xmarket_exports:{recent_completed_days_pending:4,eligible_before:now,
   published:[{asset:'BTC',exported_days:1,latest_exported_day:'2026-11-12',drive_file_id:'PRIVATE'}],
   pending_by_asset:[{asset:'ETH',days:1}],password:'PRIVATE'}},
  drive:{reports:[{asset:'BTC',horizon:5,reason:'insufficient_exact_pairs',
   family:'xmarket_v1',model_version:'xmarket-offline-v1',status:'blocked'},
   {asset:'ETH',horizon:15,status:'fitted',
    family:'xmarket_v1',model_version:'xmarket-offline-v1'}]},
  performance:{status:'observed',captured_at:now,sampling_policy:'per-asset-horizon-close-v3',
   scopes:[{asset:'BTC',horizon:5,model_version:'prospective-logistic-persistent-v3',
    paired_outcomes:100,model_brier:.2,baseline_brier:.3,unique_target_windows:20,venue_count:5,
    within_target_candle_pairs:100,minimum_lead_seconds:180,outcome_clock:'target_5m_candle_close'}]},
  intelligence_metrics:{status:'observed',probe:'llm_console_v1_status',captured_at:now,
   model:'LiquidAI/LFM3-1.7B-GGUF:Q4_K_M',ready:true,inference_verified:false,
   source_sha:'a'.repeat(40),repair_state:'healthy',repair_count:2,
   runtime_context_tokens:8192,
   memory:{current_mb:1190,limit_mb:4088,peak_mb:1300,oom_kills:0,
    gateway_rss_mb:60,backend_rss_mb:1100},
   status_latency_ms:42.5,status_http:200,
   tokens_per_second:12.5,latency_ms_p95:800,
   token:'PRIVATE',prompt:'PRIVATE',endpoint:'PRIVATE'}};
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
 assert.equal(receipt.host.xmarket_scope_progress.length,2);
 assert.equal(receipt.host.xmarket_scope_progress[0].asset,'BTC');
 assert.equal(receipt.host.xmarket_scope_progress[0].horizon,15);
 assert.equal(receipt.host.xmarket_scope_progress[0].shadow_rows_24h,99);
 assert.equal(receipt.host.xmarket_scope_progress[0].proves_live_edge,false);
 assert.equal(receipt.host.xmarket_scope_progress[1].asset,'ETH');
 assert.equal(receipt.host.xmarket_scope_progress[1].stored_artifacts,0);
 assert.deepEqual(Array.from(receipt.host.xmarket_scope_progress[1].missing_native_sources),['kalshi']);
 assert.ok(!JSON.stringify(receipt.host.xmarket_scope_progress).includes('PRIVATE'),
  'Host-only model details and private IDs must never leave the signed receiver');
 assert.equal(receipt.host.live_call_delivery.active_channels,0);
 assert.equal(receipt.host.live_call_delivery.recent_accepted_delivery_rows,0);
 assert.equal(receipt.host.live_call_delivery.advance_model,'advance-candle-logistic-lead5-v1');
 assert.ok(!Object.hasOwn(receipt.host.live_call_delivery,'guild_id'));
 assert.equal(receipt.host.live_call_delivery.delivery_is_discord_api_acceptance_only,true);
 assert.equal(receipt.host.kucoin_gap_recovery.queued_exact_gaps_14d,2);
 assert.equal(receipt.host.kucoin_gap_recovery.due_exact_gaps_14d,1);
 assert.equal(receipt.host.kucoin_gap_recovery.does_not_prove_source_recovery,true);
 assert.ok(!Object.hasOwn(receipt.host.kucoin_gap_recovery,'contract'));
 assert.equal(receipt.host.pending_backlog.status,'recorded');
 assert.equal(receipt.host.pending_backlog.pending_paths_14d,240);
 assert.equal(receipt.host.pending_backlog.due_pending_paths_14d,40);
 assert.equal(receipt.host.pending_backlog.unique_due_targets_14d,20);
 assert.ok(!Object.hasOwn(receipt.host.pending_backlog,'contract'));
 assert.equal(receipt.host.cycle_diagnostics.status,'recorded');
 assert.equal(receipt.host.cycle_diagnostics.cycles_24h,288);
 assert.equal(receipt.host.cycle_diagnostics.mean_selected_streams_24h,33.6);
 assert.equal(receipt.host.cycle_diagnostics.latest_selected_streams,34);
 assert.equal(receipt.host.cycle_diagnostics.mean_insufficient_training_24h,1.5);
 assert.equal(receipt.host.cycle_diagnostics.latest_insufficient_training,3);
 assert.equal(receipt.host.cycle_diagnostics.latest_issued_forecasts,96);
 assert.ok(!Object.hasOwn(receipt.host.cycle_diagnostics,'contract'));
 assert.equal(receipt.host.xmarket_exports.published[0].latest_exported_day,'2026-11-12');
 assert.equal(receipt.drive.reports[0].reason,'insufficient_exact_pairs');
 assert.equal(receipt.drive.reports[0].model_version,null,
  'A blocked scope cannot claim a fitted artifact');
 assert.equal(receipt.drive.reports[1].family,'xmarket_v1');
 assert.equal(receipt.drive.reports[1].model_version,'xmarket-offline-v1');
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
 // D3: authenticated LFM3 metrics are allowlisted, never healthy by default.
 assert.equal(receipt.intelligence_metrics.status,'observed');
 assert.equal(receipt.intelligence_metrics.model,'LiquidAI/LFM3-1.7B-GGUF:Q4_K_M');
 assert.equal(receipt.intelligence_metrics.ready,true);
 assert.equal(receipt.intelligence_metrics.inference_verified,false,
  'A status-endpoint read must never be reported as verified generation');
 assert.equal(receipt.intelligence_metrics.memory_pressure_pct,29.1);
 assert.equal(receipt.intelligence_metrics.memory.oom_kills,0);
 assert.equal(receipt.intelligence_metrics.repair_state,'healthy');
 assert.equal(receipt.intelligence_metrics.tokens_per_second,12.5);
 assert.ok(!JSON.stringify(receipt.intelligence_metrics).includes('PRIVATE'),
  'Prompts, tokens and private endpoints must never leave the signed receiver');
 // F4: the probe marker is carried into storage (null when absent), and
 // F13: intelligence.inference_verified agrees with the metrics probe gate.
 assert.equal(receipt.intelligence_metrics.generation_probe,null,
  'No probe marker in the payload means no marker in storage');
 assert.equal(receipt.intelligence.inference_verified,false,
  'intelligence.inference_verified must agree with the metrics probe gate');
 const verifiedMetrics={status:'observed',probe:'llm_console_v1_status',captured_at:now,
  model:'LiquidAI/LFM3-1.7B-GGUF:Q4_K_M',ready:true,inference_verified:true,
  generation_probe:'authenticated_generation',
  source_sha:'a'.repeat(40),repair_state:'healthy',repair_count:2,
  runtime_context_tokens:8192,
  memory:{current_mb:1190,limit_mb:4088,peak_mb:1300,oom_kills:0,
   gateway_rss_mb:60,backend_rss_mb:1100},
  status_latency_ms:42.5,status_http:200,
  tokens_per_second:12.5,latency_ms_p95:800};
 const verified=cx.build({schema_version:1,captured_at:now,
  intelligence_metrics:verifiedMetrics},{runId:'123456789'});
 assert.equal(verified.intelligence_metrics.inference_verified,true);
 assert.equal(verified.intelligence_metrics.generation_probe,'authenticated_generation',
  'An authenticated probe marker must survive ingest into storage');
 assert.equal(verified.intelligence.inference_verified,true,
  'A verified probe must be reported as verified, not permanently false');
 const tampered=cx.build({schema_version:1,captured_at:now,
  intelligence_metrics:{...verifiedMetrics,generation_probe:'tampered'}},
  {runId:'123456789'});
 assert.equal(tampered.intelligence_metrics.inference_verified,false,
  'A tampered probe marker must not verify generation');
 assert.equal(tampered.intelligence_metrics.generation_probe,null);
 assert.equal(tampered.intelligence.inference_verified,false);
 // F8: Signals admission is exactly 6h, matching the window_hours===6
 // contract and the UI label; the 2-minute future tolerance is unchanged.
 const signalRow=(issued,asset)=>({asset,horizon:5,venue:'okx',quote:'USDT',
  issued_at:issued,target_opened_at:issued,
  target_close_at:new Date(Date.parse(issued)+300000).toISOString(),
  settled_at:null,probability_up:0.6,baseline_up:0.5,direction:'UP',
  status:'ungraded',outcome_up:null,lead_seconds:300});
 const signals=cx.build({schema_version:1,captured_at:now,host:{
  recent_forward_calls:{status:'observed',captured_at:now,window_hours:6,
   rows:[signalRow(new Date(Date.now()-6.5*3600000).toISOString(),'SOL'),
    signalRow(new Date(Date.now()-5.5*3600000).toISOString(),'BTC')]}}},
  {runId:'123456789'});
 assert.equal(signals.host.recent_forward_calls.rows.length,1,
  'A row issued 6.5h ago is outside the 6h Signals window');
 assert.equal(signals.host.recent_forward_calls.rows[0].asset,'BTC');
 // F2: future-dated captures are rejected (5-minute clock-skew allowance);
 // the 24h past bound is unchanged.
 assert.throws(()=>cx.build({schema_version:1,
  captured_at:new Date(Date.now()+3600000).toISOString()},{runId:'1'}),
  /invalid_capture_timestamp/,'A receipt dated 1h in the future must be rejected');
 assert.throws(()=>cx.build({schema_version:1,
  captured_at:new Date(Date.now()-25*3600000).toISOString()},{runId:'1'}),
  /invalid_capture_timestamp/,'A receipt older than 24h must be rejected');
 assert.doesNotThrow(()=>cx.build({schema_version:1,
  captured_at:new Date(Date.now()+120000).toISOString()},{runId:'1'}),
  'A capture 2 minutes in the future is within clock-skew allowance');
 // F12: a source with a malformed reason is a failed read with a sanitized
 // reason, never a dropped source.
 const malformed=cx.build({schema_version:1,captured_at:now,sources:{
  drive:{status:'ok',reason:'!!!',checked_at:now},
  postgres:{status:'ok',reason:42,checked_at:now},
  github:{status:'ok',reason:'UPPER Case-Reason!',checked_at:now}}},{runId:'1'});
 assert.equal(malformed.sources.drive.status,'failed_read');
 assert.equal(malformed.sources.drive.reason,'malformed_reason');
 assert.equal(malformed.sources.postgres.status,'failed_read');
 assert.equal(malformed.sources.postgres.reason,'malformed_reason');
 assert.equal(malformed.sources.github.status,'failed_read');
 assert.equal(malformed.sources.github.reason,'upper_case_reason');
 // Merge addendum: a probe that ran but could not read status stays
 // distinguishable in storage from a probe that never ran — the collector
 // carries the probe label, the validated failure label, and the read time
 // through even on the unavailable path.
 const statusFailed=cx.build({schema_version:1,captured_at:now,
  intelligence_metrics:{status:'unavailable',probe:'llm_console_v1_status',
   status_error:'status_http_503',captured_at:now}},{runId:'123456789'});
 assert.equal(statusFailed.intelligence_metrics.status,'unavailable');
 assert.equal(statusFailed.intelligence_metrics.probe,'llm_console_v1_status',
  'A probe that emitted a marker must not be reported as never emitted');
 assert.equal(statusFailed.intelligence_metrics.status_error,'status_http_503');
 assert.equal(statusFailed.intelligence_metrics.captured_at,now);
 assert.equal(statusFailed.intelligence_metrics.inference_verified,false);
 const neverRan=cx.build({schema_version:1,captured_at:now,
  intelligence_metrics:{status:'unavailable'}},{runId:'123456789'});
 assert.equal(neverRan.intelligence_metrics.probe,'host_probe_not_emitted');
 assert.equal(neverRan.intelligence_metrics.status_error,null);
 assert.equal(neverRan.intelligence_metrics.captured_at,null);
 // D3: history points are versioned and carry bounded Drive/LLM trend inputs.
 const series=JSON.parse(records.get('metrics-series'));
 assert.equal(series.length,1);
 assert.equal(series[0].schema_version,1,'Trend comparisons must gate on receipt schema version');
 assert.equal(series[0].run_id,claims.run_id);
 assert.equal(series[0].drive.status,'unknown');
 assert.equal(series[0].drive.blocked,1);
 assert.equal(series[0].drive.fitted,1);
 assert.equal(series[0].llm.status,'observed');
 assert.equal(series[0].llm.inference_verified,false);
 assert.equal(series[0].llm.memory_pressure_pct,29.1);
 assert.equal(series[0].llm.oom_kills,0);
 const noMetrics=cx.build({schema_version:1,captured_at:now},{runId:'123456789'});
 assert.equal(noMetrics.intelligence_metrics.status,'unavailable');
 assert.equal(noMetrics.intelligence_metrics.inference_verified,false);
 assert.equal(noMetrics.intelligence_metrics.memory_pressure_pct,null);
 assert.ok(!records.get('latest').includes('PRIVATE'));
 assert.equal(records.get('run:'+claims.run_id),records.get('latest'));
 assert.equal(receipt.collection.event,'schedule');
 assert.equal(receipt.collection.run_id,'123456789');
 assert.equal(receipt.collection.run_attempt,1);
 assert.equal(receipt.collection.coalesced,false);
 assert.ok(!Object.hasOwn(receipt.collection,'run_url'));
 assert.equal(receipt.sources.discloud_forecast.status,'stale');
 assert.equal(receipt.sources.discloud_forecast.reason,'stale_or_clock_invalid');
 assert.equal(receipt.sources.discloud_forecast.freshness_seconds,900);
 assert.equal(receipt.sources.drive.status,'failed_read');
 assert.equal(receipt.sources.performance.status,'measured_zero');
 assert.ok(!Object.hasOwn(receipt.sources,'attacker_source'));
 // bad_reason is dropped by the source-name allowlist (unknown name), not
 // by its reason: the malformed-reason fallback (F12) is covered below
 // with allowlisted source names.
 assert.ok(!Object.hasOwn(receipt.sources,'bad_reason'));
 assert.ok(!records.get('latest').includes('PRIVATE'));
 const health=JSON.parse(records.get('source-health'));
 assert.equal(health.discloud_forecast.status,'stale');
 assert.equal(health.discloud_forecast.last_error_at,now);
 assert.equal(health.discloud_forecast.last_success_at,null);
 assert.equal(health.discloud_forecast.consecutive_failures,1);
 assert.equal(health.discloud_learning.status,'ok');
 assert.equal(health.discloud_learning.last_success_at,now);
 assert.equal(health.discloud_learning.consecutive_failures,0);
 assert.equal(health.drive.consecutive_failures,1);
 const lastPublication=JSON.parse(records.get('last-publication'));
 assert.equal(lastPublication.run_id,'123456789');
 assert.equal(lastPublication.event,'schedule');
 assert.equal(JSON.parse(records.get('last-schedule-attempt')).outcome,'accepted');
 const attemptsBefore=JSON.parse(records.get('attempt-history'));
 assert.equal(attemptsBefore.length,1);
 assert.equal(attemptsBefore[0].outcome,'accepted');
 // A failed run reports itself through the same OIDC gate. The attempt is
 // recorded and visible, but the last good snapshot is never overwritten.
 const makeAttempt=(token,body)=>new Request(claims.aud+'/ingest-attempt',
  {method:'POST',headers:token?{Authorization:'Bearer '+token}:{},
   body:JSON.stringify(body)});
 assert.equal((await cx.worker.fetch(makeAttempt(null,{kind:'collection-attempt'}),env)).status,401);
 const failedAttempt={kind:'collection-attempt',event:'schedule',
  run_id:'123456792',run_attempt:1,attempted_at:new Date().toISOString(),
  outcome:'failed',reason:'receiver_http_403_authorization_required'};
 assert.equal((await cx.worker.fetch(makeAttempt(await sign({...claims,event_name:'schedule',run_id:'123456792'}),failedAttempt),env)).status,202);
 assert.equal(JSON.parse(records.get('latest')).provenance.run_id,'123456789',
  'A failed attempt must never overwrite the last good snapshot');
 const lastAttempt=JSON.parse(records.get('last-attempt'));
 assert.equal(lastAttempt.outcome,'failed');
 assert.equal(lastAttempt.reason,'receiver_http_403_authorization_required');
 assert.equal(lastAttempt.run_id,'123456792');
 const attemptsAfter=JSON.parse(records.get('attempt-history'));
 assert.equal(attemptsAfter.length,2);
 assert.equal(attemptsAfter[1].outcome,'failed');
 const badReason={...failedAttempt,reason:'DROP TABLE; evil'};
 assert.equal((await cx.worker.fetch(makeAttempt(await sign({...claims,event_name:'schedule',run_id:'123456793'}),badReason),env)).status,400);
 const diagResponse=await cx.worker.fetch(new Request(claims.aud+'/diagnostics'),env);
 assert.equal(diagResponse.status,200);
 const diag=await diagResponse.json();
 assert.equal(diag.collection.last_publication.run_id,'123456789');
 assert.equal(diag.collection.last_attempt.outcome,'failed');
 assert.equal(diag.counters.failed_attempts,1);
 assert.equal(diag.counters.accepted_attempts,1);
 assert.equal(diag.counters.sources_failing,3);
 assert.equal(diag.sources.drive.consecutive_failures,1);
 assert.equal(diag.attempts.length,2);
 // A second good receipt clears the per-source failure streak while
 // keeping the last error timestamp for the incident record.
 const recovered={...payload,sources:{...payload.sources,
  drive:{status:'ok',reason:null,checked_at:now,freshness_seconds:86400}},
  captured_at:new Date(Date.now()+60000).toISOString()};
 assert.equal((await cx.worker.fetch(make(valid, recovered),env)).status,202);
 const healthAfter=JSON.parse(records.get('source-health'));
 assert.equal(healthAfter.drive.status,'ok');
 assert.equal(healthAfter.drive.consecutive_failures,0);
 assert.equal(healthAfter.drive.last_success_at,now);
 assert.ok(healthAfter.drive.last_error_at,
  'Recovery must not erase when the source last failed');
 assert.equal(receipt.host.recent_forward_calls.rows[0].target_opened_at,opened);
 assert.equal(receipt.host.recent_forward_calls.rows_per_scope,3);
 // D2: artifact identity, paper gates, lab queue, quotes and watchdog.
 const extended=cx.build({schema_version:1,captured_at:now,host:{
  forecast_status:'fresh',
  model_artifacts:{status:'recorded',incumbent:{
   family:'prospective-logistic',model_version:'prospective-logistic-persistent-v3',
   serving_status:'serving',lineage_status:'weight_updates_observed',
   weights_sha256_prefix:'abcdef012345',seed_sha256_prefix:'0123456789ab',
   online_training_updates:84,sampled_forecasts:12,sampled_streams:3,
   verified_weight_updates:2,
   lab:{status:'recorded',sampled_streams:4,streams_with_training_updates:2,
    latest_checkpoint_at:now},proves_improvement:false},
   xmarket:[{asset:'BTC',horizon:15,artifact_sha256_prefix:'deadbeef1234',
    trained_through:now,latest_imported_at:now,stored_artifacts:2}]},
  research_loop:{status:'recorded',enabled:true,drive_folder_ids_present:true,
   recent_by_status:{screened:3,discarded:1},recent_submitted_to_drive:1},
  paper_proof:{status:'recorded',evaluated_scopes:71,fresh_scopes:12,
   qualified_paper_scopes:0,
   best_exact_source:{asset:'XRP',venue:'binance',quote:'USDT',horizon_minutes:15,
    fresh:true,targets:165,days:22,eligible_quotes:160,closed_quotes:144,
    quote_coverage:.9,qualified_paper:false,
    checks:{future_outcomes:true,beats_baselines:true,quote_coverage:true,
     after_cost_paper:false,valid_records:true},
    trades:60,gain:-0.002,severe_lower_bps:-3.5,promotion_status:'held'},
   xmarket_v1:{status:'recorded',evaluated_scopes:71,fresh_scopes:12,
    qualified_paper_scopes:0,
    scopes:[{asset:'BTC',venue:'hyperliquid',quote:'USDC',horizon_minutes:15,
     fresh:true,status:'recorded',qualified_paper:false,targets:40,days:1,
     eligible_quotes:27,closed_quotes:0,quote_coverage:0,
     missing_quote_windows:27,
     checks:{future_outcomes:false,beats_baselines:false,quote_coverage:false,
      after_cost_paper:false,valid_records:true}}],
    scope_handoff:{status:'recorded',fitted_scopes:5,issuing_scopes:5,
     issuing_scopes_with_fresh_source_evaluation:3,
     issuing_scopes_missing_supported_source_evaluation:2,
     scopes:[{asset:'BTC',horizon_minutes:15,issued_24h:77,
      status:'source_evaluation_recorded',source_evaluations:7,
      fresh_valid_source_evaluations:7,quote_gate_passing_sources:0,
      qualified_paper_sources:0}]}}},
  xmarket_quotes:{status:'recorded',window_start:now,captured_at:now,
   scopes:[{asset:'BTC',horizon_minutes:15,venue:'okx',quote:'USDT',
    forecast_rows:10,decisive_forecast_rows:4,entry_rows:3,closed_exit_rows:2,
    missed_exit_rows:1,elapsed_unmatched_decisive_rows:0,
    latest_entry_at:now,latest_exit_at:now}]},
  xmarket_watchdog:{status:'fresh',operational_status:'degraded',
   census_status:'recorded',scope_checked_at:now,fitted_scopes:5,
   issuing_scopes:4,silent_scopes:1,open_silence_incidents:1,
   resolved_silence_incidents:2,
   scopes:[{asset:'BTC',horizon_minutes:15,status:'issuing',
    incumbent_rows:10,shadow_rows:7}]}},
  drive:{reports:[{asset:'BTC',horizon:5,status:'blocked',family:'xmarket_v1',
   model_version:null,artifact_sha256_prefix:'abcdef012345',
   gate_failures:['split too small: train=0 val=0 test=12']}]}},
  {runId:'123456789'});
 assert.equal(extended.host.model_artifacts.status,'recorded');
 assert.equal(extended.host.model_artifacts.incumbent.model_version,
  'prospective-logistic-persistent-v3');
 assert.equal(extended.host.model_artifacts.incumbent.weights_sha256_prefix,
  'abcdef012345');
 assert.equal(extended.host.model_artifacts.xmarket[0].artifact_sha256_prefix,
  'deadbeef1234');
 assert.equal(extended.host.research_loop.recent_by_status.screened,3);
 assert.equal(extended.host.paper_proof.qualified_paper_scopes,0);
 assert.equal(extended.host.paper_proof.best_exact_source.promotion_status,'held');
 assert.equal(extended.host.paper_proof.best_exact_source.severe_lower_bps,-3.5);
 assert.equal(extended.host.paper_proof.xmarket_v1.scopes[0].sources_evaluated,1);
 assert.equal(extended.host.paper_proof.xmarket_v1.scopes[0].failing_checks.quote_coverage,1);
 assert.equal(extended.host.paper_proof.xmarket_v1.scope_handoff.issuing_scopes,5);
 assert.equal(extended.host.xmarket_quotes.scopes[0].entry_rows,3);
 assert.equal(extended.host.xmarket_watchdog.operational_status,'degraded');
 assert.equal(extended.drive.reports[0].artifact_sha256_prefix,'abcdef012345');
 assert.deepEqual(extended.drive.reports[0].gate_failures,
  ['split too small: train=0 val=0 test=12']);
 assert.ok(!JSON.stringify(extended).includes('PRIVATE'));
 // F2 at the receiver boundary: a future-dated receipt is a 400, and the
 // stored snapshot is untouched.
 const futurePayload={...payload,captured_at:new Date(Date.now()+3600000).toISOString()};
 assert.equal((await cx.worker.fetch(make(valid,futurePayload),env)).status,400);
 assert.equal(JSON.parse(records.get('latest')).provenance.run_id,'123456789',
  'A rejected future-dated receipt must never overwrite the last good snapshot');
 // F12 end to end: a known source with a malformed reason is ingested as a
 // failed read and counted in diagnostics, not dropped to unknown.
 const f12Payload={...payload,
  captured_at:new Date(Date.now()+120000).toISOString(),
  sources:{...payload.sources,
   drive:{status:'ok',reason:'DROP TABLE',checked_at:now,freshness_seconds:86400}}};
 assert.equal((await cx.worker.fetch(make(await sign({...claims,run_id:'123456795'}),
  f12Payload),env)).status,202);
 const diag2=await (await cx.worker.fetch(new Request(claims.aud+'/diagnostics'),env)).json();
 assert.equal(diag2.sources.drive.status,'failed_read');
 assert.equal(diag2.sources.drive.last_reason,'drop_table');
 assert.equal(diag2.sources.drive.consecutive_failures,1);
 assert.equal(diag2.counters.sources_failing,3);
 // D4: paper trade counts are bounded like every other count (10M), and
 // full 12-scope XMarket coverage survives the receiver -- the old cap of
 // 8 nulled a legitimate 12 fresh / 12 expected.
 const bounded=cx.build({schema_version:1,captured_at:now,host:{
  xmarket_fresh_inputs:12,xmarket_expected_inputs:12,
  paper_proof:{status:'recorded',evaluated_scopes:71,fresh_scopes:12,
   qualified_paper_scopes:0,
   best_exact_source:{asset:'BTC',venue:'polymarket',quote:'USDT',
    horizon_minutes:5,fresh:true,targets:165,days:22,eligible_quotes:160,
    closed_quotes:144,quote_coverage:0.9,qualified_paper:false,
    checks:{future_outcomes:true,beats_baselines:true,quote_coverage:true,
     after_cost_paper:false,valid_records:true},
    trades:10000001,gain:0.001,severe_lower_bps:1,
    promotion_status:'held'}}}},{runId:'123456789'});
 assert.equal(bounded.host.xmarket_fresh_inputs,12);
 assert.equal(bounded.host.xmarket_expected_inputs,12);
 assert.ok(!Object.hasOwn(bounded.host.paper_proof.best_exact_source,'trades'));
 const unavailable=cx.build({schema_version:1,captured_at:now},{runId:'123456789'});
 assert.equal(unavailable.host.xmarket_exports,null);
 assert.equal(unavailable.host.recent_forward_calls.status,'read_unavailable');
 assert.equal(unavailable.host.xmarket_imported_scopes,null);
 assert.equal(unavailable.performance.status,'unavailable');
 assert.equal(unavailable.performance.scopes.length,0,
  'A model ID must not be mistaken for an observed scored outcome');
 // Alert dedupe throttle: the board worker's durable "already sent"
 // record. First sight of an identity may send; the same identity is
 // suppressed for an hour; a different key is a different identity (which
 // key a condition maps to is the worker's policy); an expired record no
 // longer suppresses; malformed keys are rejected.
 const throttleReq=key=>new Request(claims.aud+'/alert-throttle',
  {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({key})});
 const driveIdentity='warning|source_failed:drive';
 let throttleRes=await cx.worker.fetch(throttleReq(driveIdentity),env);
 assert.equal(throttleRes.status,200);
 assert.equal((await throttleRes.json()).send,true,'first sight of an identity may send');
 throttleRes=await cx.worker.fetch(throttleReq(driveIdentity),env);
 assert.equal((await throttleRes.json()).send,false,'the same identity within the hour is suppressed');
 throttleRes=await cx.worker.fetch(throttleReq('warning|source_failed:github'),env);
 assert.equal((await throttleRes.json()).send,true,'the endpoint treats keys opaquely: a different key is a different identity');
 records.set('alert-throttle:warning|old-identity',JSON.stringify({at:Date.now()-2*3600000}));
 throttleRes=await cx.worker.fetch(throttleReq('warning|old-identity'),env);
 assert.equal((await throttleRes.json()).send,true,'a record older than the window no longer suppresses');
 throttleRes=await cx.worker.fetch(throttleReq('bad key with spaces'),env);
 assert.equal(throttleRes.status,400,'a malformed key is rejected');
 console.log('Evidence collector signed round-trip, redaction and rejection regressions passed');
}
main().catch(error=>{console.error(error);process.exitCode=1});
