/* Source-backed Evidence Board regression. No network, secrets or writes. */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'..','web','evidence-board','worker.js'),'utf8');
const prefix='const HTML = ';
const begin=source.indexOf(prefix)+prefix.length;
const end=source.indexOf(';\nconst REPOS = ',begin);
assert.ok(begin>=prefix.length && end>begin,'Worker must contain its HTML');
const html=JSON.parse(source.slice(begin,end));
const start=html.indexOf("'use strict';",html.indexOf('<script>'));
const stop=html.indexOf('</script>',start);
assert.ok(start>0 && stop>start,'Browser script must exist');
const browser=html.slice(start,stop);
new vm.Script(browser,{filename:'evidence-board.inline.js'});
const cut=browser.indexOf('var boardLastRefresh=Date.now()');
assert.ok(cut>0,'The browser poller should initialize after the view definitions');
const document={
  drawer:'',body:{style:{}},
  getElementById(id){return{
    set innerHTML(value){document[id]=value},
    get innerHTML(){return document[id]},
    classList:{add(){},remove(){}},style:{}
  }},
  addEventListener(){}
};
const cx={document,Date,Intl,URLSearchParams,Math,Number,String,Object,Array,JSON,RegExp,console};
vm.createContext(cx);
vm.runInContext(browser.slice(0,cut),cx,{timeout:3500});
const at='2026-11-13T02:44:29.415Z';
cx.state.prs=[
 {number:553,title:'Keep Evidence Board signed receipts current',state:'closed',merged_at:at,
  merge_commit_sha:'9cfd852c0b2610bd463935b1ef59682ae4f61a20',head_sha:'abcdef',
  html_url:'https://github.com/UglyGameFace/RivaL/pull/553',updated_at:at},
 {number:557,title:'Synthetic recent change for fixture',state:'open',merged_at:null,
  html_url:'https://github.com/UglyGameFace/RivaL/pull/557',updated_at:at}
];
cx.state.runs=[
 {id:43350851273,name:'RivaL Evidence Board source receipt',conclusion:'success',status:'completed',
  html_url:'https://github.com/UglyGameFace/RivaL/actions/runs/43350851273',
  created_at:at,head_sha:'9cfd852c0b2610bd463935b1ef59682ae4f61a20'}
];
cx.state.snap=at;
cx.state.source='snapshot';
cx.state.evidence={
 captured_at:at,
 provenance:{method:'github-actions-oidc',verified:true,source_url:'https://github.com/UglyGameFace/RivaL/actions/runs/43350851273'},
 host:{status:'verified',xmarket_fresh_inputs:8,xmarket_expected_inputs:8,xmarket_imported_scopes:0,xmarket_shadow_rows:0,
  kucoin_gap_recovery:{status:'recorded',assets_observed:1,queued_exact_gaps_14d:2,due_exact_gaps_14d:1},
  pending_backlog:{status:'recorded',pending_paths_14d:240,due_pending_paths_14d:40,unique_due_targets_14d:20},
  live_call_delivery:{status:'recorded',active_channels:0,
   recent_accepted_delivery_rows:0,advance_model:'advance-candle-logistic-lead5-v1'}},
 database:{status:'verified_read',weight_updates_observed:102,qualified_paper_scopes:0,evaluated_paper_scopes:12},
 drive:{status:'verified_reports',latest_report_at:at,reports:[{asset:'BTC',horizon:5,status:'blocked',
  pm_status:'unverified',kalshi_status:'unverified',pm_pairs:0,kalshi_pairs:0,eligible_rows:0,
  reason:'no_prospective_export',report_modified_at:at,
  family:'xmarket_v1',model_version:null}]},
 performance:{scopes:[{asset:'BTC',horizon:15,paired_outcomes:285,model_brier:0.227,
  baseline_brier:0.235,after_cost_qualified:false}]}
};
assert.match(cx.sourcePanel(),/KuCoin exact source gaps/);
assert.match(cx.opsXMarket(),/No fitted model/);
assert.match(cx.opsXMarketHostScopes(),/Host per-scope evidence not published/);
cx.state.evidence.host.xmarket_scope_progress=[
 {asset:'BTC',horizon:15,status:'shadow_issuing',stored_artifacts:2,
  shadow_rows_24h:77,fresh_native_sources:2,expected_native_sources:2,
  latest_shadow_at:at,missing_native_sources:[],stale_native_sources:[]},
 {asset:'ETH',horizon:5,status:'waiting_for_fit',stored_artifacts:0,
  shadow_rows_24h:0,fresh_native_sources:1,expected_native_sources:2,
  latest_shadow_at:null,missing_native_sources:['kalshi'],stale_native_sources:[]}
];
assert.match(cx.opsXMarket(),/Host issuance by asset and timeframe/);
assert.match(cx.opsXMarket(),/BTC · 15m/);
assert.match(cx.opsXMarket(),/Research shadows issuing/);
assert.match(cx.opsXMarket(),/Research shadows issued · 24h: 77/);
assert.match(cx.opsXMarket(),/ETH · 5m/);
assert.match(cx.opsXMarket(),/Waiting for eligible fit/);
assert.match(cx.opsXMarket(),/Missing: kalshi/);
assert.doesNotMatch(cx.opsXMarketHostScopes(),/model_weights|PRIVATE/);
delete cx.state.evidence.host.xmarket_scope_progress;
cx.v2scope('BTC',5);
assert.match(document.drawer,/Report model identity/);
assert.match(document.drawer,/No fitted model in chosen report/);
cx.state.evidence.drive.reports[0].status='fitted';
cx.state.evidence.drive.reports[0].model_version='xmarket-offline-v1';
assert.equal(cx.v2rows()[0].model_version,'xmarket-offline-v1');
assert.match(cx.opsXMarket(),/xmarket_v1 \/ xmarket-offline-v1/);
cx.v2scope('BTC',5);
assert.match(document.drawer,/xmarket_v1 \/ xmarket-offline-v1/);
cx.state.evidence.drive.reports[0].status='blocked';
cx.state.evidence.drive.reports[0].model_version=null;
assert.match(cx.sourcePanel(),/2 queued/);
assert.match(cx.sourcePanel(),/1 due/);
assert.match(cx.sourcePanel(),/Pending path backlog/);
assert.match(cx.sourcePanel(),/40 due/);
assert.match(cx.sourcePanel(),/20 unique due targets/);
assert.match(cx.sourcePanel(),/Discord Live Calls/);
assert.match(cx.sourcePanel(),/0 subscribed channels/);
assert.match(cx.sourcePanel(),/advance-candle-logistic-lead5-v1/);
assert.equal(cx.prs().length,2);
// PR-triggered CI uses the head SHA, not the merge commit SHA.
cx.state.prs[1].head_sha='def987';
cx.state.runs.push({id:43350851274,name:'CI',conclusion:'success',status:'completed',
  html_url:'https://github.com/UglyGameFace/RivaL/actions/runs/43350851274',
  created_at:at,head_sha:'def987'});
cx.state.filters.workflow='CI';
cx.state.filters.run='43350851274';
assert.equal(cx.prs().filter(cx.pass).length,1,'PR head SHA should correlate with workflow run filters');
assert.equal(cx.prs().filter(cx.pass)[0].n,557);
cx.state.filters.workflow='';
cx.state.filters.run='';
assert.match(cx.rows(),/Keep Evidence Board/);
assert.match(cx.ciPage(),/RivaL Evidence Board source receipt/);
assert.match(cx.ciPage(),/were pending/);
assert.match(cx.ciPage(),/current states may differ/);
assert.match(cx.runItem(cx.state.runs[0]),/at capture/);
assert.match(cx.auditPage(),/Synthetic recent change/);
assert.equal(cx.v2rows().length,1);
const panel=cx.experimentsSourcePanel();
assert.match(panel,/Real model evidence/);
assert.match(panel,/Observed paired scoring scopes/);
for(const view of ['All','Pending CI','Merged · host unverified','Missing artifacts',
 'Cold start','Shadow results','Promotion gates','Claims']){
 cx.state.view=view;
 const q=cx.workQueues();
 assert.ok(q.content.length>50,view+' must explain the available evidence');
 assert.ok(q.buttons.includes(view),view+' should have a working selector');
}
cx.v2scope('BTC',5);
assert.match(document.drawer,/Open signed publisher run/);
assert.match(document.drawer,/Polymarket history status/);
assert.match(document.drawer,/No matching prospective export/);
cx.openExperiment('RivaL',553);
assert.match(document.drawer,/Keep Evidence Board/);
assert.match(document.drawer,/9cfd852c/);
assert.match(source,/publicGithubRunDetails/);
assert.match(source,/req\.method!=='GET'&&req\.method!=='HEAD'/);
assert.match(source,/not-in-signed-snapshot/);
assert.match(source,/RIVAL_EVIDENCE_RECEIPTS\.fetch/);
assert.doesNotMatch(source,/return tracked\.map\(/);
assert.match(cx.xmarketExportBox(),/breakdown is not available/);
cx.state.evidence.host.xmarket_stage='waiting_for_completed_day_export';
cx.state.evidence.host.xmarket_exports={recent_completed_days_pending:4,
 eligible_before:at,published:[],pending_by_asset:[{asset:'BTC',days:1}]};
assert.match(cx.xmarketExportBox(),/Waiting for completed-day export/);
assert.match(cx.xmarketExportBox(),/awaiting publication: 4/);
cx.state.evidence.performance.sampling_policy='per-asset-horizon-close-v3';
cx.state.evidence.performance.scopes[0].within_target_candle_pairs=285;
cx.state.evidence.performance.scopes[0].minimum_lead_seconds=180;
cx.state.history={points:[
 {captured_at:at,performance:{sampling_policy:'per-asset-v1',scopes:cx.state.evidence.performance.scopes}},
 {captured_at:at,performance:{sampling_policy:'per-asset-horizon-v2',scopes:cx.state.evidence.performance.scopes}},
 {captured_at:at,performance:{sampling_policy:'per-asset-horizon-close-v3',scopes:cx.state.evidence.performance.scopes}}
]};
assert.doesNotMatch(cx.scoreBox(),/<polyline/,'Changed sampling policies cannot become a trend');
assert.match(cx.scoreBox(),/500 rows per asset and timeframe/);
assert.match(cx.scoreBox(),/Minimum remaining time: 180s/);
assert.match(cx.v2pipeline(),/Provider comparison scopes/);
cx.state.evidence.drive.reports=Array.from({length:12},(_,i)=>({
 asset:['BTC','ETH','SOL','XRP'][Math.floor(i/3)],horizon:[5,15,60][i%3],
 status:'blocked',gate_passed:false,pm_pairs:91,kalshi_pairs:145,eligible_rows:0,
 report_modified_at:at
}));
cx.state.evidence.drive.status='verified_reports';
cx.state.evidence.drive.latest_report_at=at;
cx.state.training={reports:[],verified_at:'2026-11-12T01:00:00Z'};
assert.match(cx.proofGrid(),/Eligible historical training overlap/);
assert.match(cx.proofGrid(),/No eligible rows in this dated report set/);
assert.doesNotMatch(cx.proofGrid(),/Matching historical bars/);
assert.doesNotMatch(cx.proofGrid(),/VERIFIED/,'Provider comparison pairs cannot prove a training join');
cx.state.evidence.host.xmarket_imported_scopes=5;
cx.state.evidence.drive.reports=cx.state.evidence.drive.reports.map((r,i)=>({
  ...r,status:i<7?'rejected':'blocked',eligible_rows:i<7?17000:0,
  gate_passed:false,reason:i<7?'offline_gates_failed':'insufficient_labelled_rows'
}));
assert.match(cx.overview(),/0\/12 passing fits/);
assert.match(cx.overview(),/7 rejected by offline gates/);
assert.match(cx.experimentsSourcePanel(),/Fit gates not passed/);
assert.match(cx.experimentsSourcePanel(),/7 rejected in dated reports/);
assert.match(cx.evidencePage(),/Why the dated reports have not qualified/);
assert.match(cx.evidencePage(),/7 rejected by offline gates/);
assert.match(cx.evidencePage(),/5 blocked/);
assert.match(cx.evidencePage(),/eligible-row counts/);
assert.doesNotMatch(cx.evidencePage(),/The 12 Nov 12 reports do contain historical price candidates/);
cx.v2scope('BTC',5);
assert.match(document.drawer,/OFFLINE GATES REJECTED IN THIS REPORT/);
assert.match(cx.v2matrix(),/Blocked or rejected in that report/);
assert.match(cx.v2pipeline(),/artifact-to-report lineage unverified/);

assert.match(cx.v2pipeline(),/not eligible training overlap/);
assert.doesNotMatch(cx.v2pipeline(),/Matched historical scopes/);
assert.match(cx.sourceBanner(),/PERIODIC EVIDENCE BOARD v2.2/);
assert.match(cx.evidencePage(),/Why is the Brier trend not an accuracy or profit graph/);
cx.state.page='CI queue';
assert.match(cx.filters(),/Workflow name/);
assert.doesNotMatch(cx.filters(),/Search code PR/,'CI filters must not pretend to filter by PR title');
assert.doesNotMatch(cx.filters(),/Polymarket/,'CI view must not display dead market/asset selectors');
assert.match(cx.filters(),/in_progress/);
cx.state.page='Experiments';
assert.doesNotMatch(cx.filters(),/in_progress/,'PR status options must not be workflow statuses');
assert.match(cx.filters(),/Search code PR or SHA/);
assert.match(source,/liveness_only:true/);
assert.match(source,/evidence_health:'not_checked'/);
assert.match(source,/age>=0&&age<1500/,'Future-dated receipts cannot pass freshness');
assert.match(cx.sourcePanel(),/SIGNED RECEIPT/);
assert.match(source,/if\(url.pathname==='\/api\/status'\)/);
assert.doesNotMatch(source,/signed auto-updates/);

// The primary product UI must be an operations dashboard, not a PR index.
assert.deepEqual(Array.from(cx.NAV),['Command','Markets','Signals','Models','Grading','XMarket','Diagnostics']);
// Live grading page: interim grades render from the receipt section, and
// a missing section degrades to an honest unavailable message.
cx.state.evidence.live_grading={schema:'rival-live-grading-v1',computed_at:at,tests:[
 {id:'fair_value_polymarket',title:'Fair value — Polymarket',kind:'fair_value',venue:'polymarket',
  status:'ACCRUAL',verdict:null,
  floors:{recording_days:{have:5,need:7},evaluated_windows:{have:90,need:2500},triggered_calls:{have:30,need:250}},
  legs:{paired_brier_corrected:{value:-0.0028,bar:null,first:null,second:null,ci95:{lower:-0.004,upper:-0.0005,days:5},passing_now:true,rule:'corrected ruler'}},
  calls:{n:30,mean_net:0.015,win_rate:0.6},per_asset:{BTC:{calls:30,delta_brier:-0.0028,call_mean_net:0.015}},note:'interim only'},
 {id:'n3',title:'N3',kind:'n3',venue:'polymarket',status:'external_recorder',live_counters:null,
  registered:{band:[0.9,0.97],deadline:'2027-03-31',rule:'band rule'},note:'external'}],
 live_model:{title:'Live model',baseline_kind:'smoothed_historical_up_rate',last_7d:{settled:12000,unique_target_windows:1334,delta_log_loss:0.006,delta_log_loss_ci95:{lower:0.004,upper:0.008,days:7},delta_brier:0.001,accuracy:0.5034,baseline_accuracy:0.5123,days:7},last_24h:null,per_horizon_7d:{},note:'rolling'}};
assert.match(cx.opsGrading(),/graded as settlements land/);
assert.match(cx.opsGrading(),/90 \/ 2,500/);
assert.match(cx.opsGrading(),/passing so far/);
assert.match(cx.opsGrading(),/Live model — rolling grade/);
assert.match(cx.opsGrading(),/Baseline: Historical UP frequency/);
assert.match(cx.opsGrading(),/1,334/);
assert.match(cx.opsGrading(),/50.3% vs 51.2%/);
assert.match(cx.opsGrading(),/-0.002800/);
assert.match(cx.opsGrading(),/-0.000500/);
assert.equal(cx.opsGradeNum(-0.00283),'-0.002830');
assert.equal(cx.opsGradeNum(0.000123),'0.000123');
assert.equal(cx.opsGradeNum(null),'—');
cx.state.evidence.live_grading.candidates={status:'recorded',snapshot_at:at,
 counts:{total:2,passed:0,killed:1,not_measurable:1,data_blocked:0,evaluated:0},
 candidates:[{id:'llm-mask-btc-trend',display:'AI-suggested mask · BTC',screen:'G-D',
  status:'NOT_MEASURABLE',verdict:null,graded_rows:0,calls:0,days:0,
  blocker:'its exchange features were not recorded; no grader reads them yet',progress:null,floors:{},cluster_unit:'asset_target_clock',evaluated_at:at},
 {id:'gen-book-all',display:'Book model · all book signals',screen:'G-A',
  status:'EVALUATED',verdict:'KILL',graded_rows:2100,calls:2000,days:14,
  blocker:null,progress:null,floors:{},evaluated_at:at}],note:'recorded by the last workbench run'};
assert.match(cx.opsGrading(),/Candidate factory/);
assert.match(cx.opsGrading(),/no grader reads them yet/);
assert.match(cx.opsGrading(),/1 not measurable yet/);
assert.match(cx.opsGrading(),/Cluster unit: coin \/ target clock, shared across venues/);
cx.state.evidence.live_grading={status:'read_unavailable'};
assert.match(cx.opsGrading(),/unavailable in the latest receipt/);
cx.state.evidence.live_grading=null;
assert.match(cx.opsCommand(),/See what the bot is actually doing/);
assert.match(cx.opsCommand(),/Evidence integrity and freshness/);
assert.match(cx.opsEvidenceReadiness(),/Signed receipt/);
assert.match(cx.opsEvidenceReadiness(),/Discloud forecasting/);
assert.match(cx.opsEvidenceReadiness(),/PostgreSQL read/);
assert.match(cx.opsEvidenceReadiness(),/Private Drive reports/);
assert.match(cx.opsEvidenceReadiness(),/Public market quotes/);
assert.doesNotMatch(cx.opsEvidenceReadiness(),/LFM3/,
  'The readiness panel must not reference the retired LFM3 service');
const evidenceTimestamp=cx.state.evidence.captured_at;
cx.state.evidence.captured_at=new Date(Date.now()-60000).toISOString();
cx.state.receiptError='';
assert.equal(cx.sourceCurrent(),true,'A verified current receipt is admissible before a failed refresh');
assert.match(cx.opsEvidenceReadiness(),/Verified at last check/);
cx.state.receiptError='Evidence cache temporarily unavailable';
assert.equal(cx.sourceCurrent(),false,'Failed refresh must invalidate the recent-health badge');
assert.match(cx.opsSourceBanner(),/Last saved source check failed/);
assert.match(cx.opsEvidenceReadiness(),/Unverified \/ delayed/);
assert.match(cx.sourceBanner(),/LAST EVIDENCE CHECK FAILED/);
cx.state.receiptError='';
cx.state.evidence.captured_at=evidenceTimestamp;
assert.doesNotMatch(cx.opsCommand(),/Tracked changes|Pending workflows|Work queues/);
assert.match(cx.opsSignals(),/Recently issued research forecasts/);
assert.match(cx.opsSignals(),/Per-call research forecasts not in the latest signed receipt/);
cx.state.evidence.host.recent_forward_calls={
 status:'observed',captured_at:at,window_hours:6,not_trading_calls:true,
 rows:[{asset:'BTC',horizon:5,venue:'okx',quote:'USDT',
  issued_at:new Date(Date.now()-120000).toISOString(),
  target_close_at:new Date(Date.now()+180000).toISOString(),
  probability_up:0.62,baseline_up:0.52,direction:'UP',
  status:'open',outcome_up:null,lead_seconds:300}]
};
assert.match(cx.opsSignals(),/62.0%/);
assert.match(cx.opsSignals(),/52.0% UP/);
assert.match(cx.opsSignals(),/Target pending/);
assert.match(cx.opsSignals(),/Target closes \(5m candle\)/);
assert.match(cx.opsSignals(),/not a guarantee of a 60-minute-ahead forecast/);
assert.match(cx.opsCommand(),/Latest research forecasts/);
assert.match(cx.opsCommand(),/62.0%/);
cx.state.evidence.host.recent_forward_calls.rows[0].status='settled';
cx.state.evidence.host.recent_forward_calls.rows[0].outcome_up=1;
assert.match(cx.opsSignals(),/Actual UP/);
cx.state.evidence.host.recent_forward_calls={status:'read_unavailable',captured_at:at,window_hours:6,rows:[]};
assert.match(cx.opsSignals(),/not in the latest signed receipt/);

// D2: artifact identity, paper gates, lab queue, watchdog, longer history.
cx.state.evidence.host.xmarket_scope_progress=[
 {asset:'BTC',horizon:15,status:'shadow_issuing',stored_artifacts:2,
  shadow_rows_24h:77,fresh_native_sources:2,expected_native_sources:2,
  latest_shadow_at:at,latest_imported_at:at,latest_artifact_sha256_prefix:'deadbeef1234',
  latest_trained_through:at,missing_native_sources:[],stale_native_sources:[]},
 {asset:'ETH',horizon:5,status:'waiting_for_fit',stored_artifacts:0,
  shadow_rows_24h:0,fresh_native_sources:1,expected_native_sources:2,
  latest_shadow_at:null,latest_imported_at:null,
  missing_native_sources:['kalshi'],stale_native_sources:[]}
];
cx.state.evidence.host.model_artifacts={status:'recorded',incumbent:{
 family:'prospective-logistic',model_version:'prospective-logistic-persistent-v3',
 serving_status:'serving',lineage_status:'weight_updates_observed',
 weights_sha256_prefix:'abcdef012345',seed_sha256_prefix:'0123456789ab',
 online_training_updates:84,sampled_forecasts:12,sampled_streams:3,
 verified_weight_updates:2,
 lab:{status:'recorded',sampled_streams:4,streams_with_training_updates:2,latest_checkpoint_at:at}},
 xmarket:[{asset:'BTC',horizon:15,artifact_sha256_prefix:'deadbeef1234',
  trained_through:at,latest_imported_at:at,stored_artifacts:2}]};
cx.state.evidence.host.research_loop={status:'recorded',enabled:true,
 drive_folder_ids_present:true,recent_by_status:{screened:3,discarded:1},
 recent_submitted_to_drive:1};
cx.state.evidence.host.paper_proof={status:'recorded',evaluated_scopes:71,
 fresh_scopes:12,qualified_paper_scopes:0,
 best_exact_source:{asset:'XRP',venue:'binance',quote:'USDT',horizon_minutes:15,
  fresh:true,targets:165,days:22,eligible_quotes:160,closed_quotes:144,
  quote_coverage:.9,qualified_paper:false,
  checks:{future_outcomes:true,beats_baselines:true,quote_coverage:true,
   after_cost_paper:false,valid_records:true},
  trades:60,gain:-0.002,severe_lower_bps:-3.5,promotion_status:'held'},
 xmarket_v1:{status:'recorded',evaluated_scopes:71,fresh_scopes:12,
  qualified_paper_scopes:0,
  scopes:[{asset:'BTC',horizon:15,sources_evaluated:2,fresh_valid_sources:2,
   qualified_sources:0,min_targets:40,min_days:1,min_coverage:0,
   failing_checks:{quote_coverage:2,after_cost_paper:2},venues:['binance','hyperliquid']}],
  scope_handoff:{status:'recorded',fitted_scopes:5,issuing_scopes:5,
   issuing_scopes_with_fresh_source_evaluation:3,
   issuing_scopes_missing_supported_source_evaluation:2,scopes:[]}}};
cx.state.evidence.host.xmarket_quotes={status:'recorded',window_start:at,
 captured_at:at,scopes:[{asset:'BTC',horizon:15,forecast_rows:15,
  decisive_forecast_rows:6,entry_rows:4,closed_exit_rows:3,missed_exit_rows:1,
  elapsed_unmatched_decisive_rows:0,latest_entry_at:at,latest_exit_at:at,
  venues:[{venue:'okx',quote:'USDT',entry_rows:4,closed_exit_rows:3,missed_exit_rows:1}]}]};
cx.state.evidence.host.xmarket_watchdog={status:'fresh',operational_status:'degraded',
 census_status:'recorded',scope_checked_at:at,fitted_scopes:5,issuing_scopes:4,
 silent_scopes:1,open_silence_incidents:1,resolved_silence_incidents:2,
 scopes:[{asset:'BTC',horizon:15,status:'issuing',incumbent_rows:10,shadow_rows:7}]};
assert.match(cx.opsModels(),/Weight digest prefix/);
assert.match(cx.opsModels(),/abcdef012345/);
assert.match(cx.opsModels(),/Model Lab/);
assert.match(cx.opsModels(),/screened · 3/);
assert.match(cx.opsModels(),/deadbeef1234/);
assert.match(cx.opsSignals(),/Paper-evaluation evidence/);
assert.match(cx.opsSignals(),/0 of 71/);
assert.match(cx.opsSignals(),/Why no scope qualified/);
assert.match(cx.opsSignals(),/Promotion record: held/);
assert.match(cx.opsSignals(),/observed fees, spread, slippage, funding/);
assert.match(cx.opsXMarket(),/Digest prefixes linked per scope below/);
assert.match(cx.opsXMarket(),/Paper evaluation by scope/);
assert.match(cx.opsXMarket(),/Issuance watchdog/);
assert.match(cx.opsXMarket(),/degraded/);
assert.match(cx.opsXMarket(),/4 entries/);
assert.match(cx.opsXMarket(),/deadbeef1234/);
cx.state.evidence.host.recent_forward_calls={
 status:'observed',captured_at:at,window_hours:6,rows_per_scope:3,not_trading_calls:true,
 rows:[
  {asset:'BTC',horizon:5,venue:'okx',quote:'USDT',
   issued_at:new Date(Date.now()-120000).toISOString(),
   target_opened_at:new Date(Date.now()-120000).toISOString(),
   target_close_at:new Date(Date.now()+180000).toISOString(),settled_at:null,
   probability_up:0.62,baseline_up:0.52,direction:'UP',status:'open',outcome_up:null,lead_seconds:300},
  {asset:'BTC',horizon:5,venue:'okx',quote:'USDT',
   issued_at:new Date(Date.now()-300000).toISOString(),
   target_opened_at:new Date(Date.now()-420000).toISOString(),
   target_close_at:new Date(Date.now()-120000).toISOString(),
   settled_at:new Date(Date.now()-60000).toISOString(),
   probability_up:0.58,baseline_up:0.51,direction:'UP',status:'settled',outcome_up:0,lead_seconds:180}
 ]};
assert.match(cx.opsSignals(),/Target opens \(5m candle\)/);
assert.match(cx.opsSignals(),/58.0%/);
assert.match(cx.opsSignals(),/Next-target readiness/);
assert.match(cx.opsSignals(),/1 of 12 scopes/);
cx.opsSigFilter('outcome','settled');
assert.match(cx.opsSignals(),/58.0%/);
assert.doesNotMatch(cx.opsSignals(),/62.0%/);
cx.opsSigFilter('outcome','all');
assert.match(cx.opsSignals(),/62.0%/);
cx.v2scope('BTC',5);
assert.match(document.drawer,/Drive report artifact prefix/);
assert.match(document.drawer,/No host-imported artifact prefix published/);
assert.match(document.drawer,/Gate failures/);

assert.match(cx.opsSignals(),/Coverage is not an 80% prediction-accuracy rule/);
assert.match(cx.opsModels(),/Are RivaL predictions improving/);
assert.match(cx.opsXMarket(),/From market inputs to usable shadow predictions/);
assert.match(cx.opsXMarket(),/7 rejected by offline gates/);
assert.equal(cx.opsIntelligenceView,undefined,
  'The Intelligence view must not exist');
assert.match(cx.opsDiagnostics(),/Diagnostics and source evidence/);
assert.match(cx.opsDiagnostics(),/Loading collection attempts/,
  'Diagnostics must show the reliability section while /api/diagnostics loads');
cx.state.diagnostics={
  collection:{last_scheduled_attempt_at:new Date(Date.now()-13*3600000).toISOString(),
    last_successful_publication_at:new Date(Date.now()-120000).toISOString(),
    last_attempt_at:new Date(Date.now()-60000).toISOString(),
    last_attempt_outcome:'failed',last_attempt_reason:'receiver_http_403_authorization_required',
    schedule_gap_seconds:46800,target_cadence_seconds:600},
  sources:[
    {name:'discloud_forecast',status:'ok',reason:null,last_success_at:new Date(Date.now()-120000).toISOString(),last_error_at:null,consecutive_failures:0,freshness_seconds:900,fresh:true},
    {name:'drive',status:'failed_read',reason:'incomplete_listing',last_success_at:null,last_error_at:new Date(Date.now()-60000).toISOString(),consecutive_failures:3,freshness_seconds:86400,fresh:false},
    {name:'performance',status:'measured_zero',reason:'no_pairs_measured',last_success_at:new Date(Date.now()-120000).toISOString(),last_error_at:null,consecutive_failures:0,freshness_seconds:3600,fresh:true}],
  alerts:[{code:'source_failed:drive',level:'warning',message:'Source drive is failed_read',since:new Date(Date.now()-60000).toISOString()}],
  incidents:{total_attempts:5,failed_attempts:2,coalesced_attempts:1,accepted_attempts:2,sources_failing:1,sources_tracked:3},
  attempts:[{run_id:'123456792',event:'schedule',attempted_at:new Date(Date.now()-60000).toISOString(),received_at:new Date(Date.now()-60000).toISOString(),outcome:'failed',reason:'receiver_http_403_authorization_required'}]};
assert.match(cx.opsDiagnostics(),/Collection reliability/);
assert.match(cx.opsDiagnostics(),/Last scheduled attempt/);
assert.match(cx.opsDiagnostics(),/Last successful signed publication/);
assert.match(cx.opsDiagnostics(),/receiver_http_403_authorization_required/,
  'The last failure reason must be visible in Diagnostics');
assert.match(cx.opsDiagnostics(),/drive/);
assert.match(cx.opsDiagnostics(),/incomplete_listing/);
assert.match(cx.opsDiagnostics(),/3 consecutive failures/);
assert.match(cx.opsDiagnostics(),/OK · zero measured/,
  'A genuinely measured zero must not read as a failure');
assert.match(cx.opsDiagnostics(),/Scheduled collection is stale/,
  'Historical fallback data must be labelled stale, never current');
assert.match(cx.opsDiagnostics(),/2 failed · 1 coalesced · 1 sources failing/);
assert.match(cx.opsMore(),/Everything else in RivaL/);
assert.match(cx.opsSourceBanner(),/production receipt/);
cx.ops.perpsStatus='ok';
cx.ops.perps={
 status:'ok',generated_at:Date.now(),
 venues:[{id:'okx',name:'OKX',status:'ok'},{id:'binance',name:'Binance',status:'blocked'}],
 assets:[
  {asset:'BTC',price:64000,change_24h_pct:-3.5,venues_used:8,dispersion_bps:6.1,
   volume_24h_usd:2000000000,open_interest_usd:500000000,
   venues:[{venue:'okx',status:'ok',mid:64000,spread_bps:.15,funding:-.00005}]},
  {asset:'ETH',price:2020,change_24h_pct:2,venues_used:7},
  {asset:'SOL',price:130,change_24h_pct:-2,venues_used:5},
  {asset:'XRP',price:1.4,change_24h_pct:0,venues_used:3}
 ]
};
cx.ops.predictions.BTC={
 asset:'BTC',polymarket:{status:'ok',generated_at:Date.now(),contracts:[
 {asset:'BTC',horizon:5,title:'BTC',status:'ok',closes:Date.now()+300000,
  up_ask:.52,down_ask:null,up_ask_depth:100,down_ask_depth:0,
  url:'https://polymarket.com/event/btc-test'}
 ]},
 kalshi:{status:'ok',generated_at:Date.now(),quote_source:'kalshi_market_summary',
  markets:[{title:'BTC threshold',ticker:'BTCX',yes_bid:.48,yes_ask:.54,
   volume_24h:500,close_time:Date.now()+300000}],open_count:1}
};
// Source exchange clocks can be a fraction of a second ahead of this browser's clock.
cx.ops.perps.generated_at=Date.now()+500;
assert.equal(cx.opsMarketFresh(),true,'Sub-second source clock lead must not falsely mark quotes dated');
assert.match(cx.opsAge(cx.ops.perps.generated_at),/under 1m ago/);
cx.ops.perps.generated_at=Date.now()+60000;
assert.equal(cx.opsMarketFresh(),false,'Future source timestamps beyond bounded tolerance must not count as live');
cx.ops.perps.generated_at=Date.now();
assert.match(cx.opsCommand(),/\$64,000/);
assert.match(cx.opsMarkets(),/Polymarket contracts/);
assert.match(cx.opsMarkets(),/Kalshi crypto markets/);
assert.match(cx.opsMarkets(),/No quote/,'Absent quote side must not be shown as zero');
assert.match(cx.opsMarkets(),/Binance|okx/);
cx.ops.predictions.BTC.polymarket.contracts=[];
assert.match(cx.opsMarkets(),/No current Polymarket contract book returned/);
cx.ops.predictions.BTC.kalshi.markets=[];
assert.match(cx.opsMarkets(),/No Kalshi market summaries returned/);
cx.ops.predictions.BTC=undefined;
assert.match(cx.opsMarkets(),/Polymarket CLOB snapshot unavailable/);
cx.ops.perps=null;cx.ops.perpsStatus='unavailable';
assert.match(cx.opsMarkets(),/Exchange quotes unavailable/);
assert.match(cx.opsCommand(),/Live public venue quotes are unavailable/);
assert.match(source,/RIVAL_MARKET_FEED\.fetch/);
assert.match(source,/\/api\/market\/perps/);
assert.match(source,/\/api\/market\/predictions/);

// Signed scoring version must be disclosed without pretending it is an XMarket artifact.
cx.state.evidence.performance.status='observed';
cx.state.evidence.performance.model='prospective-logistic-persistent-v3';
assert.equal(cx.opsModelId(),'prospective-logistic-persistent-v3');
assert.match(cx.opsModels(),/Scored incumbent model/);
assert.match(cx.opsModels(),/prospective-logistic-persistent-v3/);
assert.match(cx.opsModelCard(cx.state.evidence.performance.scopes[0]),/prospective-logistic-persistent-v3/);
assert.match(cx.opsCommand(),/Which actual model is scored/);
assert.match(cx.opsCommand(),/XMarket challengers are separate/);
// Without a published artifact identity the board must say so plainly.
delete cx.state.evidence.host.model_artifacts;
assert.match(cx.opsXMarket(),/no signed import-artifact-to-served-model identity/);
cx.opsOpenScore('BTC',15);
assert.match(document.drawer,/Scored model version/);
assert.match(document.drawer,/prospective-logistic-persistent-v3/);
cx.state.evidence.performance.model='<unsafe-html>';
assert.equal(cx.opsModelId(),null,'Untrusted source model IDs must never render as markup');
assert.doesNotMatch(cx.opsModels(),/unsafe-html/);
cx.state.evidence.performance.model='prospective-logistic-persistent-v3';

const lastGoodTime=new Date(Date.now()-120000).toISOString();
const lastGoodCall={
 asset:'BTC',horizon:15,venue:'hyperliquid',quote:'USDC',
 model_version:'prospective-logistic-persistent-v3',
 issued_at:new Date(Date.now()-180000).toISOString(),
 target_close_at:new Date(Date.now()+300000).toISOString(),
 probability_up:0.64,baseline_up:0.51,direction:'UP',status:'open',outcome_up:null
};
cx.state.history={source:'signed-github-actions',points:[
 {run_id:'43801752054',captured_at:lastGoodTime,
  host:{status:'verified',forecast_issued:102,learning_settled:103,
   xmarket_fresh_inputs:8,xmarket_expected_inputs:8,
   xmarket_imported_scopes:5,xmarket_shadow_rows:4092,
   recent_forward_calls:{status:'observed',captured_at:lastGoodTime,rows:[lastGoodCall]}},
  database:{status:'verified_read',qualified_paper_scopes:0,evaluated_paper_scopes:12}},
 {run_id:'43801822488',captured_at:new Date().toISOString(),
  host:{status:'unavailable',forecast_issued:null},
  database:{status:'unavailable',qualified_paper_scopes:null}}
]};
cx.state.evidence.host={status:'unavailable',forecast_status:'read_unavailable',
 recent_forward_calls:{status:'read_unavailable',rows:[]}};
cx.state.evidence.database={status:'unavailable',qualified_paper_scopes:null};
const oldHost=cx.opsHomeObservation();
assert.equal(oldHost.historical,true);
assert.equal(oldHost.run_id,'43801752054');
assert.equal(oldHost.host.forecast_issued,102);
assert.match(cx.opsCommand(),/xmarket_v1/,
 'Only verified host-sampled XMarket family shadows should have a family label');
assert.equal(oldHost.database.qualified_paper_scopes,0);
assert.match(cx.opsCommand(),/Latest signed host read failed/);
assert.match(cx.opsCommand(),/Previous verified call snapshot, not current/);
assert.match(cx.opsCommand(),/prospective-logistic-persistent-v3/);
assert.match(cx.opsAlerts(),/Latest signed host\/database read unavailable/);
assert.match(cx.opsForecastSection(3),/64.0%/);
assert.match(cx.opsForecastSection(3),/prospective-logistic-persistent-v3/);
cx.state.history.points=[{run_id:'stale',captured_at:new Date(Date.now()-2*86400000).toISOString(),
 host:oldHost.host,database:oldHost.database}];
assert.equal(cx.opsHomeObservation().historical,false,'Two-day-old host data must never be offered as recent fallback');
assert.match(cx.opsCommand(),/Read unavailable/);
assert.doesNotMatch(cx.opsCommand(),/Latest signed host read failed/);
cx.state.evidence.host={...oldHost.host};
cx.state.evidence.database={...oldHost.database};
assert.equal(cx.opsHomeObservation().historical,false,'A verified current host read must supersede historical fallback');

// D3: market-specific prediction evidence — implied probability vs the
// signed RivaL forecast, rejected quotes, DraftKings disconnected.
cx.ops.perpsStatus='ok';
cx.ops.perps={
 status:'ok',generated_at:Date.now(),
 venues:[{id:'okx',name:'OKX',status:'ok',latency_ms:25},{id:'binance',name:'Binance',status:'blocked',http_status:403}],
 assets:[{asset:'BTC',price:64000,change_24h_pct:-3.5,venues_used:8,dispersion_bps:6.1,
  volume_24h_usd:2000000000,open_interest_usd:500000000,
  venues:[{venue:'okx',status:'ok',mid:64000,bid:63999,ask:64001,spread_bps:.15,funding:-.00005,
   open_interest_usd:450000000,quote_age_ms:1518,quote_kind:'indicative'}]},
  {asset:'ETH',price:2020,change_24h_pct:2,venues_used:7},
  {asset:'SOL',price:130,change_24h_pct:-2,venues_used:5},
  {asset:'XRP',price:1.4,change_24h_pct:0,venues_used:3}]
};
cx.ops.predictions.BTC={
 asset:'BTC',
 draftkings:{status:'disconnected',reason:'no_authorized_verified_source',contracts:[]},
 polymarket:{status:'ok',generated_at:Date.now(),contracts:[
  {asset:'BTC',horizon:15,title:'BTC 15m',status:'ok',closes:Date.now()+900000,
   accepting_orders:true,up_ask:.64,down_ask:.37,up_bid:.60,down_bid:.36,
   up_ask_depth:100,down_ask_depth:200,book_age_ms:50,
   market_up_prob:.62,quote_state:'observed',usable:true,
   resolution_rule:null,resolution_rule_source:'not_carried_by_feed',
   settlement:null,model_linkage:'proximity_only_no_contract_id'}
 ]},
 kalshi:{status:'ok',generated_at:Date.now(),quote_source:'kalshi_market_summary',
  markets:[{title:'BTC threshold',ticker:'BTCX',yes_bid:.48,yes_ask:.54,last:.53,
   volume_24h:500,oi:1200,close_time:Date.now()+300000,
   yes_implied_prob:.51,quote_kind:'market_summary_not_executable',executable:false,
   resolution_rule:null,model_linkage:'proximity_only_no_contract_id'}],open_count:1}
};
const d3markets=cx.opsMarkets();
assert.match(d3markets,/Market-implied UP/);
assert.match(d3markets,/64.0%/,'Signed forecast probability must sit beside the market-implied probability');
assert.match(d3markets,/Model minus market/);
assert.match(d3markets,/\+2\.0 pts/);
assert.match(d3markets,/Resolution rule: not carried/);
assert.match(d3markets,/not a verified contract link/);
assert.match(d3markets,/DraftKings Predictions is disconnected/);
assert.match(d3markets,/Source contribution and gaps/);
assert.match(d3markets,/Quote age/);
assert.match(d3markets,/1,518 ms/);
assert.match(d3markets,/Bid \/ Ask/);
assert.match(d3markets,/Open interest/);
assert.match(d3markets,/YES implied/);
assert.match(d3markets,/market-summary fields, not an independently verified executable order book/);
assert.match(d3markets,/0 qualified of 12 evaluated/);
assert.doesNotMatch(d3markets,/No quote.*0\.0|0\.0¢/,'Missing quotes must never render as zero prices');
// Expired Polymarket contracts are rejected as baselines, not plotted.
cx.ops.predictions.BTC.polymarket.contracts[0].quote_state='expired';
cx.ops.predictions.BTC.polymarket.contracts[0].usable=false;
cx.ops.predictions.BTC.polymarket.contracts[0].closes=Date.now()-60000;
assert.match(cx.opsMarkets(),/Expired/);
assert.match(cx.opsMarkets(),/contract not usable as a baseline/);

// D3: Diagnostics Trends — version/policy gated history with filters.
const trendPoint=(at,policy,paired)=>({run_id:'t'+at,schema_version:1,captured_at:at,
 host:{status:'verified',forecast_issued:100,learning_settled:99,xmarket_shadow_rows:4000},
 database:{status:'verified_read',qualified_paper_scopes:0,evaluated_paper_scopes:12},
 drive:{status:'verified_reports',accepted:1,rejected:0,blocked:2},
 performance:{model:'prospective-logistic-persistent-v3',sampling_policy:policy,
  scopes:[{asset:'BTC',horizon:5,paired_outcomes:paired,model_brier:.23,baseline_brier:.25,unique_target_windows:50}]}});
const nowIso=new Date().toISOString();
const hourAgo=new Date(Date.now()-3600000).toISOString();
const twoHoursAgo=new Date(Date.now()-7200000).toISOString();
cx.state.history={source:'signed-github-actions',points:[
 trendPoint(twoHoursAgo,'per-asset-horizon-close-v3',140),
 trendPoint(hourAgo,'per-asset-horizon-close-v3',150),
 trendPoint(nowIso,'per-asset-horizon-v2',150)]};
cx.ops.diag='Trends';
const trends=cx.opsDiagnostics();
assert.match(trends,/History across signed receipts/);
assert.match(trends,/Qualification progress/);
assert.match(trends,/Mixed paired-scoring sampling policies/);
assert.match(trends,/From date/);
assert.match(trends,/150 paired outcomes of 150 required/);
assert.match(trends,/80% matched quote coverage/);
assert.match(trends,/Not measured per scope/);
assert.match(trends,/Retention gap/);
cx.state.history.points[2].performance.sampling_policy='per-asset-horizon-close-v3';
assert.doesNotMatch(cx.opsDiagnostics(),/Mixed paired-scoring sampling policies/);
cx.state.history.points[2].schema_version=2;
assert.match(cx.opsDiagnostics(),/Mixed receipt schema versions/);
cx.state.history.points[2].schema_version=1;
cx.ops.trend.asset='ETH';
assert.match(cx.opsDiagnostics(),/No retained history matches these filters/);
cx.ops.trend.asset='';
assert.match(cx.opsDiagnostics(),/History across signed receipts/);

console.log('RivaL operations dashboard UI regressions passed');

console.log('Evidence Board source-backed UI smoke passed');
