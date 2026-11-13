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
 host:{status:'verified',xmarket_fresh_inputs:8,xmarket_expected_inputs:8,xmarket_imported_scopes:0,xmarket_shadow_rows:0},
 database:{status:'verified_read',weight_updates_observed:102,qualified_paper_scopes:0,evaluated_paper_scopes:12},
 drive:{status:'verified_reports',latest_report_at:at,reports:[{asset:'BTC',horizon:5,status:'blocked',
  pm_status:'unverified',kalshi_status:'unverified',pm_pairs:0,kalshi_pairs:0,eligible_rows:0,
  reason:'no_prospective_export',report_modified_at:at}]},
 performance:{scopes:[{asset:'BTC',horizon:15,paired_outcomes:285,model_brier:0.227,
  baseline_brier:0.235,after_cost_qualified:false}]},
 intelligence:{status:'verified',prs:[],runs:[]}
};
assert.equal(cx.prs().length,2);
assert.match(cx.rows(),/Keep Evidence Board/);
assert.match(cx.ciPage(),/RivaL Evidence Board source receipt/);
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
console.log('Evidence Board source-backed UI smoke passed');
