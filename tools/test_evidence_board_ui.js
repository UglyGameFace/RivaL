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
cx.state.intPrs=[];
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
  kucoin_gap_recovery:{status:'recorded',assets_observed:1,queued_exact_gaps_14d:2,due_exact_gaps_14d:1}},
 database:{status:'verified_read',weight_updates_observed:102,qualified_paper_scopes:0,evaluated_paper_scopes:12},
 drive:{status:'verified_reports',latest_report_at:at,reports:[{asset:'BTC',horizon:5,status:'blocked',
  pm_status:'unverified',kalshi_status:'unverified',pm_pairs:0,kalshi_pairs:0,eligible_rows:0,
  reason:'no_prospective_export',report_modified_at:at}]},
 performance:{scopes:[{asset:'BTC',horizon:15,paired_outcomes:285,model_brier:0.227,
  baseline_brier:0.235,after_cost_qualified:false}]},
 intelligence:{status:'verified',prs:[],runs:[]}
};
assert.match(cx.sourcePanel(),/KuCoin exact source gaps/);
assert.match(cx.sourcePanel(),/2 queued/);
assert.match(cx.sourcePanel(),/1 due/);
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
assert.deepEqual(Array.from(cx.NAV),['Command','Markets','Signals','Models','XMarket','Intelligence','Diagnostics']);
assert.match(cx.opsCommand(),/See what the bot is actually doing/);
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

assert.match(cx.opsSignals(),/Coverage is not an 80% prediction-accuracy rule/);
assert.match(cx.opsModels(),/Are RivaL predictions improving/);
assert.match(cx.opsXMarket(),/From market inputs to usable shadow predictions/);
assert.match(cx.opsXMarket(),/7 rejected by offline gates/);
assert.match(cx.opsIntelligenceView(),/Is the model actually serving/);
assert.match(cx.opsDiagnostics(),/Diagnostics and source evidence/);
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

console.log('RivaL operations dashboard UI regressions passed');

console.log('Evidence Board source-backed UI smoke passed');
