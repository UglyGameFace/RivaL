const AUD='https://rival-evidence-collector.staytrippynlw.workers.dev';
const REPO='UglyGameFace/RivaL';
const WORKFLOW=REPO+'/.github/workflows/evidence-board-receipt.yml@refs/heads/main';
const ALLOWED_ORIGIN='https://rival-evidence-board.staytrippynlw.workers.dev';
function reply(o,status=200,cache='no-store'){
  return new Response(JSON.stringify(o),{status,headers:{'content-type':'application/json; charset=utf-8','cache-control':cache,'x-content-type-options':'nosniff','access-control-allow-origin':ALLOWED_ORIGIN,'vary':'Origin'}});
}
function decodeB64(part) {
  const s=part.replace(/-/g,'+').replace(/_/g,'/');
  const raw=atob(s+'='.repeat((4-s.length%4)%4));
  return Uint8Array.from(raw,c=>c.charCodeAt(0));
}
async function verifyGithubOIDC(token) {
  if(typeof token!=='string'||token.length>9000||token.length<300)throw Error('invalid_token');
  const parts=token.split('.');
  if(parts.length!==3)throw Error('invalid_token');
  let hdr,clm;
  try{hdr=JSON.parse(new TextDecoder().decode(decodeB64(parts[0])));clm=JSON.parse(new TextDecoder().decode(decodeB64(parts[1])))}catch{throw Error('malformed_token')}
  if(hdr.alg!=='RS256'||typeof hdr.kid!=='string')throw Error('unsupported_signature');
  const now=Math.floor(Date.now()/1000);
  if(clm.iss!=='https://token.actions.githubusercontent.com'||clm.aud!==AUD||
     clm.repository!==REPO||clm.ref!=='refs/heads/main'||clm.workflow_ref!==WORKFLOW||
     !['schedule','workflow_dispatch','push'].includes(clm.event_name)||
     !Number.isInteger(clm.exp)||clm.exp<=now||clm.exp>now+1800||
     !Number.isInteger(clm.iat)||clm.iat>now+60||now-clm.iat>1800||
     !/^\d{5,18}$/.test(String(clm.run_id||'')))throw Error('claim_mismatch');
  const jwksResponse=await fetch('https://token.actions.githubusercontent.com/.well-known/jwks',{cf:{cacheEverything:true,cacheTtl:3600}});
  if(!jwksResponse.ok)throw Error('issuer_keys_unavailable');
  const jwks=await jwksResponse.json();
  const found=Array.isArray(jwks.keys)?jwks.keys.find(k=>k.kid===hdr.kid&&k.kty==='RSA'&&k.use==='sig'):null;
  if(!found)throw Error('signing_key_unknown');
  const key=await crypto.subtle.importKey('jwk',found,{name:'RSASSA-PKCS1-v1_5',hash:'SHA-256'},false,['verify']);
  const verified=await crypto.subtle.verify('RSASSA-PKCS1-v1_5',key,decodeB64(parts[2]),new TextEncoder().encode(parts[0]+'.'+parts[1]));
  if(!verified)throw Error('signature_mismatch');
  return {runId:String(clm.run_id),sha:String(clm.sha||''),issuedAt:clm.iat};
}
function numberOrNull(v,max=10000000){
  return Number.isSafeInteger(v)&&v>=0&&v<=max?v:null;
}
function oneOf(v,values){return values.includes(v)?v:'unknown'}
function isoOrNull(v){
  if(typeof v!=='string'||v.length>40||!/^\d{4}-\d\d-\d\dT/.test(v))return null;
  const n=Date.parse(v);return Number.isFinite(n)?new Date(n).toISOString():null;
}

function cleanXmarketExports(value){
  if(!value||typeof value!=='object')return null;
  const assets=['BTC','ETH','SOL','XRP'];
  return {published:(Array.isArray(value.published)?value.published:[]).slice(0,4)
    .filter(x=>x&&assets.includes(x.asset)).map(x=>({asset:x.asset,
      exported_days:numberOrNull(x.exported_days),
      latest_exported_day:typeof x.latest_exported_day==='string'&&/^\d{4}-\d\d-\d\d$/.test(x.latest_exported_day)?x.latest_exported_day:null})),
    pending_by_asset:(Array.isArray(value.pending_by_asset)?value.pending_by_asset:[]).slice(0,4)
      .filter(x=>x&&assets.includes(x.asset)).map(x=>({asset:x.asset,days:numberOrNull(x.days)})),
    recent_completed_days_pending:numberOrNull(value.recent_completed_days_pending,12),
    eligible_before:isoOrNull(value.eligible_before),
    does_not_prove_independent_drive_restore:true};
}

function cleanGithub(g,identity) {
  if(!g||g.status!=='verified'||!Array.isArray(g.prs)||!Array.isArray(g.runs))return null;
  const fetched=isoOrNull(g.fetched_at);
  if(!fetched||Math.abs(Date.now()-Date.parse(fetched))>3600000)return null;
  const sha=x=>typeof x==='string'&&/^[0-9a-fA-F]{40}$/.test(x)?x.toLowerCase():null;
  const pickDate=isoOrNull;
  const prs=g.prs.slice(0,55).map(p=>{
    const n=p&&p.number;
    if(!Number.isSafeInteger(n)||n<1||n>1000000)return null;
    return {number:n,title:String(p.title||'').slice(0,160),
      state:['open','closed'].includes(p.state)?p.state:'unknown',
      merged_at:pickDate(p.merged_at),merge_commit_sha:sha(p.merge_commit_sha),
      created_at:pickDate(p.created_at),updated_at:pickDate(p.updated_at),
      html_url:'https://github.com/UglyGameFace/RivaL/pull/'+n,
      head_sha:sha(p.head_sha),base_sha:sha(p.base_sha)};
  }).filter(Boolean);
  const runs=g.runs.slice(0,70).map(r=>{
    const n=r&&r.id;
    if(!Number.isSafeInteger(n)||n<1||n>999999999999999)return null;
    return {id:n,name:String(r.name||'').slice(0,120),
      event:String(r.event||'unknown').slice(0,45),
      status:['queued','in_progress','completed','waiting','pending'].includes(r.status)?r.status:'unknown',
      conclusion:['success','failure','cancelled','skipped','neutral','timed_out','action_required','stale',null].includes(r.conclusion)?r.conclusion:'unknown',
      head_sha:sha(r.head_sha),created_at:pickDate(r.created_at),
      updated_at:pickDate(r.updated_at),
      html_url:'https://github.com/UglyGameFace/RivaL/actions/runs/'+n,
      run_attempt:Number.isSafeInteger(r.run_attempt)?r.run_attempt:null};
  }).filter(Boolean);
  if(!prs.length||!runs.length)return null;
  return {schema_version:1,method:'github-actions-oidc',repository:REPO,fetched_at:fetched,
    source_run_id:identity.runId,prs,runs,
    limitations:['PR merge is not host deployment','A green workflow is not model fit or predictive edge']};
}


function cleanIntelligence(source, host) {
  const out={status:'unavailable',repository:'UglyGameFace/RivaL-Intelligence',
    fetched_at:null,head_sha:null,prs:[],runs:[],
    host_status:oneOf(host&&host.status,['online','offline','unavailable']),
    inference_verified:false};
  if(!source||source.status!=='verified'||source.repository!==out.repository)return out;
  const sha=s=>typeof s==='string'&&/^[0-9a-fA-F]{40}$/.test(s)?s.toLowerCase():null;
  const time=isoOrNull(source.fetched_at);
  if(!time||!sha(source.head_sha))return out;
  out.status='verified';out.fetched_at=time;out.head_sha=sha(source.head_sha);
  out.prs=(Array.isArray(source.prs)?source.prs:[]).slice(0,12).map(p=>{
    if(!Number.isSafeInteger(p.number)||p.number<=0||p.number>1000000)return null;
    return {number:p.number,title:String(p.title||'').slice(0,160),
      state:oneOf(p.state,['open','closed']),merged_at:isoOrNull(p.merged_at),
      html_url:'https://github.com/UglyGameFace/RivaL-Intelligence/pull/'+p.number};
  }).filter(Boolean);
  out.runs=(Array.isArray(source.runs)?source.runs:[]).slice(0,15).map(r=>{
    if(!Number.isSafeInteger(r.id)||r.id<=0||r.id>999999999999999)return null;
    return {id:r.id,name:String(r.name||'').slice(0,120),
      status:oneOf(r.status,['queued','in_progress','completed','pending','waiting']),
      conclusion:oneOf(r.conclusion,['success','failure','cancelled','skipped','unknown']),
      created_at:isoOrNull(r.created_at),head_sha:sha(r.head_sha),
      html_url:'https://github.com/UglyGameFace/RivaL-Intelligence/actions/runs/'+r.id};
  }).filter(Boolean);
  return out;
}
function cleanPerformance(p) {
  const out={status:'unavailable',captured_at:null,scopes:[],window_hours:24,
    model:'prospective-logistic-persistent-v3',after_cost_qualified:false,
    caveat:'Paired Brier is predictive scoring only; after-cost trading advantage not proven'};
  if(!p||!['observed','partial','no_pairs'].includes(p.status))return out;
  const at=isoOrNull(p.captured_at);
  if(!at)return out;
  out.status=p.status;out.captured_at=at;
  out.sampling_policy=['per-asset-horizon-v2','per-asset-horizon-close-v3'].includes(p.sampling_policy)?p.sampling_policy:'per-asset-v1';
  out.sample_limit_per_scope=out.sampling_policy!=='per-asset-v1'?500:null;
  out.scopes=(Array.isArray(p.scopes)?p.scopes:[]).slice(0,12).map(s=>{
    const n=numberOrNull(s.paired_outcomes,500),m=s.model_brier,b=s.baseline_brier;
    if(!['BTC','ETH','SOL','XRP'].includes(s.asset)||![5,15,60].includes(s.horizon)||
       s.model_version!==out.model||n===null||n===0||
       typeof m!=='number'||typeof b!=='number'||!Number.isFinite(m)||!Number.isFinite(b)||
       m<0||m>1||b<0||b>1)return null;
    return {asset:s.asset,horizon:s.horizon,paired_outcomes:n,
      model_brier:+m.toFixed(6),baseline_brier:+b.toFixed(6),
      sampling_policy:out.sampling_policy,
      unique_target_windows:numberOrNull(s.unique_target_windows,n),
      venue_count:numberOrNull(s.venue_count,20),
      within_target_candle_pairs:numberOrNull(s.within_target_candle_pairs,n),
      minimum_lead_seconds:typeof s.minimum_lead_seconds==='number'&&Number.isFinite(s.minimum_lead_seconds)&&s.minimum_lead_seconds>0&&s.minimum_lead_seconds<=7200?+s.minimum_lead_seconds.toFixed(3):null,
      outcome_clock:s.outcome_clock==='target_5m_candle_close'?s.outcome_clock:null,
      after_cost_qualified:false,sample_bounded:true};
  }).filter(Boolean);
  return out;
}

function build(payload,identity){
  if(!payload||payload.schema_version!==1||typeof payload!=='object')throw Error('invalid_schema');
  const received=new Date().toISOString(),at=isoOrNull(payload.captured_at);
  if(!at||Math.abs(Date.now()-Date.parse(at))>86400000)throw Error('invalid_capture_timestamp');
  const h=payload.host||{},db=payload.database||{},d=payload.drive||{};
  const reports=Array.isArray(d.reports)?d.reports.slice(0,12):[];
  const cleanReports=reports.map(r=>{
    if(!['BTC','ETH','SOL','XRP'].includes(r.asset)||![5,15,60].includes(r.horizon))return null;
    const s=['blocked','fitted','rejected','accepted','unverified','not_recorded','unknown'];
    return {
      asset:r.asset,horizon:r.horizon,
      status:oneOf(r.status,s),
      pm_status:oneOf(r.pm_status,['verified','unverified','blocked','unknown']),
      kalshi_status:oneOf(r.kalshi_status,['verified','unverified','blocked','unknown']),
      pm_pairs:numberOrNull(r.pm_pairs,1000000),kalshi_pairs:numberOrNull(r.kalshi_pairs,1000000),
      eligible_rows:numberOrNull(r.eligible_rows,10000000),
      gate_passed:r.gate_passed===true,
      report_modified_at:isoOrNull(r.report_modified_at),
      reason:oneOf(r.reason,['no_prospective_export','no_eligible_live_feature_bars',
        'no_independent_history','no_eligible_historical_feature_bars','no_exact_feature_bar_overlap',
        'insufficient_exact_pairs','price_mismatch','insufficient_overlap_coverage',
        'insufficient_training_split','insufficient_labelled_rows','offline_gates_failed',
        'offline_gates_passed','insufficient_overlap','missing_history','mismatch','http_429','unverified','unknown'])
    };
  }).filter(Boolean);
  return {
    schema_version:1,
    captured_at:at,received_at:received,
    provenance:{method:'github-actions-oidc',verified:true,repository:REPO,run_id:identity.runId,workflow:'evidence-board-receipt.yml',
      source_url:'https://github.com/UglyGameFace/RivaL/actions/runs/'+identity.runId},
    host:{status:oneOf(h.status,['verified','unverified','unavailable']),
      forecast_status:oneOf(h.forecast_status,['fresh','stale_or_clock_invalid','not_recorded','read_unavailable']),
      learning_status:oneOf(h.learning_status,['fresh','stale_or_clock_invalid','not_recorded','read_unavailable']),
      forecast_issued:numberOrNull(h.forecast_issued),
      learning_settled:numberOrNull(h.learning_settled),
      xmarket_fresh_inputs:numberOrNull(h.xmarket_fresh_inputs,8),
      xmarket_expected_inputs:numberOrNull(h.xmarket_expected_inputs,8),
      xmarket_shadow_rows:numberOrNull(h.xmarket_shadow_rows),
      xmarket_imported_scopes:numberOrNull(h.xmarket_imported_scopes,12),
      xmarket_stage:oneOf(h.xmarket_stage,['not_initialized','waiting_for_inputs',
        'waiting_for_completed_day_export','waiting_for_fit','imported_no_shadow_rows',
        'shadow_issuing','read_unavailable']),
      xmarket_exports:cleanXmarketExports(h.xmarket_exports)
    },
    database:{
      status:oneOf(db.status,['verified_read','unavailable']),
      weight_updates_observed:numberOrNull(db.weight_updates_observed),
      compared_streams:numberOrNull(db.compared_streams),
      qualified_paper_scopes:numberOrNull(db.qualified_paper_scopes),
      evaluated_paper_scopes:numberOrNull(db.evaluated_paper_scopes),
      model_updates_are_not_edge:true
    },
    drive:{
      status:oneOf(d.status,['verified_metadata','verified_reports','unavailable']),
      reports_considered:numberOrNull(d.reports_considered,12),
      reports:cleanReports, latest_report_at:isoOrNull(d.latest_report_at),
      file_metadata_is_not_import_proof:true
    },
    github:cleanGithub(payload.github,identity),
    intelligence:cleanIntelligence(payload.intelligence,payload.intelligence_host),
    performance:cleanPerformance(payload.performance),
    limitations:['This is a timestamped read-only receipt, not continuously live health','Changed weights do not prove an after-cost edge','Drive report existence does not prove host import or shadow issuance']
  };
}
export default {
 async fetch(req,env){
  const u=new URL(req.url);
  if(req.method==='GET'&&u.pathname==='/health')return reply({ok:true,receiver:'rival-evidence-collector',read_only_sources:true});
  if(req.method==='GET'&&u.pathname==='/github-snapshot'){const data=await env.EVIDENCE_KV.get('github-snapshot','json');return reply(data||{status:'unavailable',reason:'github_snapshot_not_yet_published'},200,'public,max-age=60');}
  if(req.method==='GET'&&u.pathname==='/training-snapshot'){const data=await env.EVIDENCE_KV.get('training-report-20261112','json');return reply(data||{status:'unavailable'},200,'public,max-age=3600');}
  if(req.method==='GET'&&u.pathname==='/history'){const records=await env.EVIDENCE_KV.get('metrics-series','json');return reply({source:'signed-github-actions',points:Array.isArray(records)?records:[]},200,'public,max-age=60');}
  if(req.method==='GET'&&u.pathname==='/latest'){
    const data=await env.EVIDENCE_KV.get('latest','json');
    return reply(data||{status:'not_configured',source:'GitHub-signed collector pending'},200,'public,max-age=45,stale-while-revalidate=60');
  }
  if(req.method==='POST'&&u.pathname==='/ingest'){
    const auth=req.headers.get('Authorization')||'';
    if(!auth.startsWith('Bearer '))return reply({error:'authorization_required'},401);
    let identity;
    try{identity=await verifyGithubOIDC(auth.slice(7))}catch(e){return reply({error:'unverified_github_identity',reason:e.message},403)}
    const length=Number(req.headers.get('content-length')||0);
    if(length>80000)return reply({error:'payload_too_large'},413);
    const raw=await req.text();
    if(raw.length>80000)return reply({error:'payload_too_large'},413);
    let clean;
    try{clean=build(JSON.parse(raw),identity)}catch(e){return reply({error:'invalid_receipt',reason:e.message},400)}
    const latest=await env.EVIDENCE_KV.get('latest','json');
    if(latest&&Date.parse(latest.captured_at)>Date.parse(clean.captured_at))return reply({status:'stale_ignored'},200);
    await env.EVIDENCE_KV.put('latest',JSON.stringify(clean));
    const series=await env.EVIDENCE_KV.get('metrics-series','json');
    let entries=Array.isArray(series)?series:[];
    entries=entries.filter(e=>e&&e.run_id!==identity.runId&&Date.parse(e.captured_at)<Date.parse(clean.captured_at));
    entries.push({run_id:identity.runId,captured_at:clean.captured_at,host:clean.host,database:clean.database,performance:clean.performance,intelligence:{status:clean.intelligence.status,host_status:clean.intelligence.host_status,head_sha:clean.intelligence.head_sha}});
    entries=entries.slice(-72);
    await env.EVIDENCE_KV.put('metrics-series',JSON.stringify(entries));

    if(clean.github){await env.EVIDENCE_KV.put('github-snapshot',JSON.stringify(clean.github));}
    await env.EVIDENCE_KV.put('run:'+identity.runId,JSON.stringify(clean),{expirationTtl:60*60*24*90});
    return reply({status:'accepted',run_id:identity.runId,captured_at:clean.captured_at},202);
  }
  return reply({error:'not_found_or_method_denied'},404);
 }
};

