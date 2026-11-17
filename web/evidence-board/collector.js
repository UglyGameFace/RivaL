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
     !['schedule','workflow_dispatch','push','workflow_run'].includes(clm.event_name)||
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


function cleanXmarketScopeProgress(value){
  if(!Array.isArray(value))return null;
  const assets=['BTC','ETH','SOL','XRP'],horizons=[5,15,60],
    stages=['not_initialized','waiting_for_inputs','waiting_for_completed_day_export',
      'waiting_for_fit','imported_no_shadow_rows','shadow_issuing','read_unavailable'];
  const seen=new Set(),rows=[];
  for(const r of value.slice(0,12)){
    if(!r||!assets.includes(r.asset)||!horizons.includes(r.horizon)||!stages.includes(r.status))continue;
    const key=r.asset+':'+r.horizon;
    if(seen.has(key))continue;
    seen.add(key);
    const native=x=>Array.isArray(x)?Array.from(new Set(x.filter(
      source=>['polymarket','kalshi'].includes(source)))).sort():[];
    rows.push({
      asset:r.asset,horizon:r.horizon,status:r.status,
      stored_artifacts:numberOrNull(r.stored_artifacts,100000),
      shadow_rows_24h:numberOrNull(r.shadow_rows_24h),
      latest_imported_at:isoOrNull(r.latest_imported_at),
      latest_artifact_sha256_prefix:typeof r.latest_artifact_sha256_prefix==='string'&&/^[0-9a-f]{12}$/.test(r.latest_artifact_sha256_prefix)?r.latest_artifact_sha256_prefix:null,
      latest_trained_through:isoOrNull(r.latest_trained_through),
      latest_shadow_at:isoOrNull(r.latest_shadow_at),
      fresh_native_sources:numberOrNull(r.fresh_native_sources,2),
      expected_native_sources:2,
      missing_native_sources:native(r.missing_native_sources),
      stale_native_sources:native(r.stale_native_sources),
      proves_live_edge:false
    });
  }
  return rows.sort((a,b)=>a.asset.localeCompare(b.asset)||a.horizon-b.horizon);
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


function cleanIntelligenceMetrics(value){
  // Authenticated LFM3 health only; a status-endpoint read never proves
  // generation. Unknown fields stay null, never zero or healthy.
  const empty={status:'unavailable',probe:'host_probe_not_emitted',captured_at:null,
    model:null,ready:null,inference_verified:false,source_sha:null,
    repair_state:null,repair_count:null,runtime_context_tokens:null,
    memory:null,memory_pressure_pct:null,status_latency_ms:null,status_http:null,
    latency_ms_p50:null,latency_ms_p95:null,tokens_per_second:null,
    requests_sampled:null,generation_errors:null,fallback_count:null,
    cpu_pct:null,dns_ok:null,upstream_ok:null,db_pressure:null};
  if(!value||typeof value!=='object'||value.status!=='observed')return empty;
  if(typeof value.model!=='string'||
     !/^[A-Za-z0-9][A-Za-z0-9\/_.-]{0,79}:[A-Za-z0-9_.-]{1,40}$/.test(value.model))return empty;
  if(typeof value.ready!=='boolean')return empty;
  const mem=value.memory&&typeof value.memory==='object'?value.memory:null;
  const memKeys=['current_mb','limit_mb','peak_mb','anon_mb','file_mb',
    'file_mapped_mb','gateway_rss_mb','backend_rss_mb'];
  const memory=mem?Object.fromEntries(memKeys.map(k=>[k,numberOrNull(mem[k])])
    .concat([['oom_kills',numberOrNull(mem.oom_kills,1000000)]])):null;
  const pressure=memory&&Number.isSafeInteger(memory.current_mb)&&
    Number.isSafeInteger(memory.limit_mb)&&memory.limit_mb>0
    ?+Math.min(memory.current_mb/memory.limit_mb*100,1000).toFixed(1):null;
  const num=(v,max)=>typeof v==='number'&&Number.isFinite(v)&&v>=0&&v<=max?+v.toFixed(3):null;
  const at=isoOrNull(value.captured_at);
  const http=value.status_http;
  if(http!==null&&http!==undefined&&http!==200)return empty;
  return {...empty,status:'observed',
    probe:value.probe==='llm_console_v1_status'?'llm_console_v1_status':'host_probe_not_emitted',
    captured_at:at,model:value.model.slice(0,120),ready:value.ready,
    inference_verified:value.inference_verified===true&&
      value.generation_probe==='authenticated_generation',
    source_sha:typeof value.source_sha==='string'&&/^[0-9a-f]{40}$/.test(value.source_sha)
      ?value.source_sha:null,
    repair_state:['healthy','warming','backend_health_check_failed','cooldown',
      'quarantined','repairing','repair_failed'].includes(value.repair_state)
      ?value.repair_state:null,
    repair_count:numberOrNull(value.repair_count,1000000),
    runtime_context_tokens:numberOrNull(value.runtime_context_tokens,1000000),
    memory,memory_pressure_pct:pressure,
    status_latency_ms:num(value.status_latency_ms,120000),
    status_http:http===200?200:null,
    latency_ms_p50:num(value.latency_ms_p50,600000),
    latency_ms_p95:num(value.latency_ms_p95,600000),
    tokens_per_second:num(value.tokens_per_second,100000),
    requests_sampled:numberOrNull(value.requests_sampled),
    generation_errors:numberOrNull(value.generation_errors),
    fallback_count:numberOrNull(value.fallback_count),
    cpu_pct:typeof value.cpu_pct==='number'&&Number.isFinite(value.cpu_pct)&&
      value.cpu_pct>=0&&value.cpu_pct<=100?+value.cpu_pct.toFixed(1):null,
    dns_ok:typeof value.dns_ok==='boolean'?value.dns_ok:null,
    upstream_ok:typeof value.upstream_ok==='boolean'?value.upstream_ok:null,
    db_pressure:['none','elevated','saturated'].includes(value.db_pressure)
      ?value.db_pressure:null};
}

function cleanForwardCalls(value){
 const empty={status:'read_unavailable',captured_at:null,window_hours:6,rows:[],rows_per_scope:3,not_trading_calls:true};
 if(!value||!['observed','no_recent_forecasts'].includes(value.status))return empty;
 const captured=isoOrNull(value.captured_at);
 if(!captured||value.window_hours!==6||!Array.isArray(value.rows))return empty;
 const counts={},rows=[];
 for(const x of value.rows.slice(0,36)){
   if(!x||!['BTC','ETH','SOL','XRP'].includes(x.asset)||![5,15,60].includes(x.horizon))continue;
   const key=x.asset+':'+x.horizon;
   if((counts[key]||0)>=3||typeof x.venue!=='string'||!/^[a-z0-9_-]{2,28}$/.test(x.venue)
     ||typeof x.quote!=='string'||!/^[A-Z]{2,8}$/.test(x.quote))continue;
   const issued=isoOrNull(x.issued_at),target=isoOrNull(x.target_close_at),
     opened=isoOrNull(x.target_opened_at),
     settled=x.settled_at==null?null:isoOrNull(x.settled_at);
   if(!issued||!target||!opened||(x.settled_at!=null&&!settled)
      ||Date.parse(target)-Date.parse(opened)!==300000
      ||Date.parse(target)<=Date.parse(issued)
      ||Date.parse(issued)>Date.parse(captured)+120000
      ||Date.parse(captured)-Date.parse(issued)>7*3600000)continue;
   if(typeof x.probability_up!=='number'||typeof x.baseline_up!=='number'
      ||!Number.isFinite(x.probability_up)||!Number.isFinite(x.baseline_up)
      ||x.probability_up<0||x.probability_up>1||x.baseline_up<0||x.baseline_up>1)continue;
   const direction=x.probability_up>0.5?'UP':x.probability_up<0.5?'DOWN':'NEUTRAL';
   if(x.direction!==direction||!['open','awaiting_settlement','settled','ungraded'].includes(x.status))continue;
   // Preserve original host event clock: an open target cannot have closed yet,
   // and a settled/ungraded/awaiting target cannot claim a future close.
   const closeTime=Date.parse(target),captureTime=Date.parse(captured);
   if((x.status==='open'&&closeTime<=captureTime)||
      (x.status!=='open'&&closeTime>captureTime))continue;
   if(x.status==='settled'&&!(x.outcome_up===0||x.outcome_up===1))continue;
   if(x.status!=='settled'&&x.outcome_up!==null)continue;
   if(x.status==='settled'&&(!settled||Date.parse(settled)<closeTime
      ||Date.parse(settled)-captureTime>120000))continue;
   if(x.status!=='settled'&&settled!==null)continue;
   if(typeof x.lead_seconds!=='number'||!Number.isFinite(x.lead_seconds)
      ||x.lead_seconds<=0||x.lead_seconds>7200)continue;
   counts[key]=(counts[key]||0)+1;rows.push({
     asset:x.asset,horizon:x.horizon,venue:x.venue,quote:x.quote,
     issued_at:issued,target_opened_at:opened,target_close_at:target,
     settled_at:x.status==='settled'?settled:null,
     probability_up:+x.probability_up.toFixed(6),baseline_up:+x.baseline_up.toFixed(6),
     direction,status:x.status,lead_seconds:+x.lead_seconds.toFixed(2),
     outcome_up:x.status==='settled'?x.outcome_up:null,
     model_version:'prospective-logistic-persistent-v3'
   });
 }
 return {status:rows.length?'observed':value.status==='no_recent_forecasts'?'no_recent_forecasts':'read_unavailable',
   captured_at:captured,window_hours:6,rows_per_scope:3,rows,not_trading_calls:true};
}

function cleanPaperChecks(v){
  if(!v||typeof v!=='object')return null;
  const keys=['future_outcomes','beats_baselines','quote_coverage','after_cost_paper','valid_records'];
  const out={};
  for(const k of keys){if(typeof v[k]!=='boolean')return null;out[k]=v[k];}
  return out;
}
function cleanPaperScopeRow(v){
  if(!v||typeof v!=='object')return null;
  if(!['BTC','ETH','SOL','XRP'].includes(v.asset)||![5,15,60].includes(v.horizon_minutes)
     ||typeof v.venue!=='string'||!/^[a-z0-9_-]{2,28}$/.test(v.venue)
     ||typeof v.quote!=='string'||!/^[A-Z]{2,8}$/.test(v.quote)
     ||typeof v.fresh!=='boolean'||typeof v.qualified_paper!=='boolean')return null;
  const checks=cleanPaperChecks(v.checks);
  if(!checks||typeof v.quote_coverage!=='number'||!Number.isFinite(v.quote_coverage)
     ||v.quote_coverage<0||v.quote_coverage>1)return null;
  const row={asset:v.asset,venue:v.venue,quote:v.quote,horizon_minutes:v.horizon_minutes,
    fresh:v.fresh,targets:numberOrNull(v.targets),days:numberOrNull(v.days),
    eligible_quotes:numberOrNull(v.eligible_quotes),closed_quotes:numberOrNull(v.closed_quotes),
    quote_coverage:+v.quote_coverage.toFixed(4),
    missing_quote_windows:numberOrNull(v.missing_quote_windows),
    qualified_paper:v.qualified_paper,checks,does_not_prove_real_fills:true};
  if(row.targets===null||row.days===null||row.eligible_quotes===null||row.closed_quotes===null)return null;
  if(Number.isSafeInteger(v.trades)&&v.trades>=0&&v.trades<=10000000)row.trades=v.trades;
  if(typeof v.gain==='number'&&Number.isFinite(v.gain)&&Math.abs(v.gain)<=10)row.gain=+v.gain.toFixed(6);
  if(typeof v.severe_lower_bps==='number'&&Number.isFinite(v.severe_lower_bps)&&Math.abs(v.severe_lower_bps)<=100000)row.severe_lower_bps=+v.severe_lower_bps.toFixed(6);
  if(['promoted_paper','demoted','held'].includes(v.promotion_status))row.promotion_status=v.promotion_status;
  return row;
}
function cleanPaperProof(p){
  const out={status:'unavailable',evaluated_scopes:null,fresh_scopes:null,qualified_paper_scopes:null,
    best_exact_source:null,
    xmarket_v1:{status:'unavailable',evaluated_scopes:null,fresh_scopes:null,qualified_paper_scopes:null,scopes:[],scope_handoff:null},
    does_not_prove_real_fills:true};
  if(!p||typeof p!=='object')return out;
  if(['recorded','no_evaluations','unavailable'].includes(p.status))out.status=p.status;
  out.evaluated_scopes=numberOrNull(p.evaluated_scopes);
  out.fresh_scopes=numberOrNull(p.fresh_scopes);
  out.qualified_paper_scopes=numberOrNull(p.qualified_paper_scopes);
  out.best_exact_source=cleanPaperScopeRow(p.best_exact_source);
  const xm=p.xmarket_v1;
  if(xm&&typeof xm==='object'){
    const x=out.xmarket_v1;
    if(['recorded','no_evaluations','unavailable'].includes(xm.status))x.status=xm.status;
    x.evaluated_scopes=numberOrNull(xm.evaluated_scopes);
    x.fresh_scopes=numberOrNull(xm.fresh_scopes);
    x.qualified_paper_scopes=numberOrNull(xm.qualified_paper_scopes);
    const grouped={};
    for(const r of Array.isArray(xm.scopes)?xm.scopes:[]){
      if(!r||!['BTC','ETH','SOL','XRP'].includes(r.asset)||![5,15,60].includes(r.horizon_minutes)
         ||typeof r.venue!=='string'||!/^[a-z0-9_-]{2,28}$/.test(r.venue)
         ||!['recorded','invalid_saved_evidence'].includes(r.status)
         ||typeof r.fresh!=='boolean'||typeof r.qualified_paper!=='boolean')continue;
      const key=r.asset+':'+r.horizon_minutes;
      const agg=grouped[key]||(grouped[key]={asset:r.asset,horizon:r.horizon_minutes,
        sources_evaluated:0,fresh_valid_sources:0,qualified_sources:0,venue_count:0,
        min_targets:null,min_days:null,min_coverage:null,failing_checks:{},_venues:new Set()});
      agg.sources_evaluated+=1;agg._venues.add(r.venue);agg.venue_count=agg._venues.size;
      if(r.status!=='recorded')continue;
      if(r.fresh){
        agg.fresh_valid_sources+=1;
        if(r.qualified_paper)agg.qualified_sources+=1;
        const checks=cleanPaperChecks(r.checks);
        if(checks)for(const [name,passed] of Object.entries(checks))
          if(!passed)agg.failing_checks[name]=(agg.failing_checks[name]||0)+1;
      }
      const t=numberOrNull(r.targets),d=numberOrNull(r.days);
      if(t!==null)agg.min_targets=agg.min_targets===null?t:Math.min(agg.min_targets,t);
      if(d!==null)agg.min_days=agg.min_days===null?d:Math.min(agg.min_days,d);
      if(typeof r.quote_coverage==='number'&&Number.isFinite(r.quote_coverage)
         &&r.quote_coverage>=0&&r.quote_coverage<=1)
        agg.min_coverage=agg.min_coverage===null?r.quote_coverage:Math.min(agg.min_coverage,r.quote_coverage);
    }
    x.scopes=Object.values(grouped).map(a=>{const { _venues, ...rest }=a;return {...rest,
      min_coverage:a.min_coverage===null?null:+a.min_coverage.toFixed(4)}})
      .sort((a,b)=>a.asset.localeCompare(b.asset)||a.horizon-b.horizon).slice(0,12);
    const ho=xm.scope_handoff;
    if(ho&&typeof ho==='object'&&ho.status==='recorded'){
      x.scope_handoff={fitted_scopes:numberOrNull(ho.fitted_scopes),
        issuing_scopes:numberOrNull(ho.issuing_scopes),
        issuing_scopes_with_fresh_source_evaluation:numberOrNull(ho.issuing_scopes_with_fresh_source_evaluation),
        issuing_scopes_missing_supported_source_evaluation:numberOrNull(ho.issuing_scopes_missing_supported_source_evaluation),
        does_not_prove_edge:true};
    }
  }
  return out;
}
function cleanModelArtifacts(v){
  // The publisher already reduced host lineage to sanitized identities;
  // the receiver only re-validates shape, never re-derives digests.
  const out={status:'unavailable',incumbent:null,xmarket:[]};
  if(!v||typeof v!=='object'||v.status!=='recorded')return out;
  const inc=v.incumbent,sha=x=>typeof x==='string'&&/^[0-9a-f]{12}$/.test(x)?x:null;
  if(inc&&typeof inc==='object'){
    const lab=inc.lab&&typeof inc.lab==='object'?inc.lab:{};
    out.status='recorded';
    out.incumbent={family:'prospective-logistic',
      model_version:typeof inc.model_version==='string'&&/^[a-z][a-z0-9-]{1,79}$/.test(inc.model_version)?inc.model_version:null,
      serving_status:['serving','unverified'].includes(inc.serving_status)?inc.serving_status:'unverified',
      lineage_status:['weight_updates_observed','no_recent_weight_change_proven','insufficient_comparable_snapshots','no_lineage'].includes(inc.lineage_status)?inc.lineage_status:'unknown',
      weights_sha256_prefix:sha(inc.weights_sha256_prefix),
      seed_sha256_prefix:sha(inc.seed_sha256_prefix),
      online_training_updates:numberOrNull(inc.online_training_updates),
      sampled_forecasts:numberOrNull(inc.sampled_forecasts),
      sampled_streams:numberOrNull(inc.sampled_streams),
      verified_weight_updates:numberOrNull(inc.verified_weight_updates),
      lab:{status:['not_initialized','recorded','no_saved_learners'].includes(lab.status)?lab.status:'unknown',
        sampled_streams:numberOrNull(lab.sampled_streams),
        streams_with_training_updates:numberOrNull(lab.streams_with_training_updates),
        latest_checkpoint_at:isoOrNull(lab.latest_checkpoint_at)},
      proves_improvement:false};
  }
  if(Array.isArray(v.xmarket))for(const r of v.xmarket.slice(0,12)){
    if(!r||!['BTC','ETH','SOL','XRP'].includes(r.asset)||![5,15,60].includes(r.horizon))continue;
    out.xmarket.push({asset:r.asset,horizon:r.horizon,
      artifact_sha256_prefix:sha(r.artifact_sha256_prefix),
      trained_through:isoOrNull(r.trained_through),
      latest_imported_at:isoOrNull(r.latest_imported_at),
      stored_artifacts:numberOrNull(r.stored_artifacts,100000)});
  }
  out.xmarket.sort((a,b)=>a.asset.localeCompare(b.asset)||a.horizon-b.horizon);
  return out;
}
function cleanResearchLoop(v){
  const out={status:'unavailable',enabled:null,drive_folder_ids_present:null,recent_by_status:{},recent_submitted_to_drive:null};
  if(!v||typeof v!=='object')return out;
  if(['recorded','unavailable'].includes(v.status))out.status=v.status;
  if(typeof v.enabled==='boolean')out.enabled=v.enabled;
  if(typeof v.drive_folder_ids_present==='boolean')out.drive_folder_ids_present=v.drive_folder_ids_present;
  if(v.recent_by_status&&typeof v.recent_by_status==='object')
    for(const [k,count] of Object.entries(v.recent_by_status).slice(0,16))
      if(/^[a-z][a-z0-9_-]{2,40}$/.test(k)&&Number.isSafeInteger(count)&&count>=0&&count<=16)out.recent_by_status[k]=count;
  out.recent_submitted_to_drive=numberOrNull(v.recent_submitted_to_drive);
  return out;
}
function cleanXmarketQuotes(v){
  const out={status:'unavailable',window_start:null,captured_at:null,scopes:[],
    counts_are_independent_targets:false,does_not_prove_prediction_edge:true};
  if(!v||typeof v!=='object')return out;
  if(['recorded','no_fitted_scopes','read_unavailable'].includes(v.status))out.status=v.status;
  out.window_start=isoOrNull(v.window_start);
  out.captured_at=isoOrNull(v.captured_at);
  const grouped={};
  for(const r of Array.isArray(v.scopes)?v.scopes:[]){
    if(!r||!['BTC','ETH','SOL','XRP'].includes(r.asset)||![5,15,60].includes(r.horizon_minutes)
       ||typeof r.venue!=='string'||!/^[a-z0-9_-]{2,28}$/.test(r.venue)
       ||typeof r.quote!=='string'||!/^[A-Z]{2,8}$/.test(r.quote))continue;
    const key=r.asset+':'+r.horizon_minutes;
    const agg=grouped[key]||(grouped[key]={asset:r.asset,horizon:r.horizon_minutes,
      forecast_rows:0,decisive_forecast_rows:0,entry_rows:0,closed_exit_rows:0,missed_exit_rows:0,
      elapsed_unmatched_decisive_rows:0,latest_entry_at:null,latest_exit_at:null,venues:[]});
    for(const f of ['forecast_rows','decisive_forecast_rows','entry_rows','closed_exit_rows','missed_exit_rows','elapsed_unmatched_decisive_rows']){
      const n=numberOrNull(r[f]);if(n!==null)agg[f]+=n;
    }
    for(const [src,tgt] of [['latest_entry_at','latest_entry_at'],['latest_exit_at','latest_exit_at']]){
      const stamp=isoOrNull(r[src]);
      if(stamp!==null&&(agg[tgt]===null||stamp>agg[tgt]))agg[tgt]=stamp;
    }
    agg.venues.push({venue:r.venue,quote:r.quote,entry_rows:numberOrNull(r.entry_rows),
      closed_exit_rows:numberOrNull(r.closed_exit_rows),missed_exit_rows:numberOrNull(r.missed_exit_rows)});
  }
  out.scopes=Object.values(grouped).map(a=>({...a,venues:a.venues.sort((x,y)=>x.venue.localeCompare(y.venue))}))
    .sort((a,b)=>a.asset.localeCompare(b.asset)||a.horizon-b.horizon).slice(0,5);
  return out;
}
function cleanXmarketWatchdog(v){
  const out={status:'unavailable',operational_status:'unverified',census_status:null,scope_checked_at:null,
    scope_age_seconds:null,watchdog_checked_at:null,boot_started_at:null,fitted_scopes:null,
    issuing_scopes:null,silent_scopes:null,open_silence_incidents:null,resolved_silence_incidents:null,
    scopes:[],does_not_prove_prediction_edge:true};
  if(!v||typeof v!=='object')return out;
  if(['fresh','stale_or_boot_unverified','not_recorded','read_unavailable'].includes(v.status))out.status=v.status;
  if(['healthy','degraded','pending','unverified'].includes(v.operational_status))out.operational_status=v.operational_status;
  if(['recorded','not_initialized'].includes(v.census_status))out.census_status=v.census_status;
  out.scope_checked_at=isoOrNull(v.scope_checked_at);
  out.scope_age_seconds=numberOrNull(v.scope_age_seconds);
  out.watchdog_checked_at=isoOrNull(v.watchdog_checked_at);
  out.boot_started_at=isoOrNull(v.boot_started_at);
  for(const f of ['fitted_scopes','issuing_scopes','silent_scopes','open_silence_incidents','resolved_silence_incidents'])out[f]=numberOrNull(v[f]);
  for(const r of Array.isArray(v.scopes)?v.scopes.slice(0,12):[]){
    if(!r||!['BTC','ETH','SOL','XRP'].includes(r.asset)||![5,15,60].includes(r.horizon_minutes)
       ||!['invalid_import_clock','import_grace','insufficient_incumbents','issuing','silent'].includes(r.status))continue;
    out.scopes.push({asset:r.asset,horizon:r.horizon,status:r.status,
      shadow_rows:numberOrNull(r.shadow_rows)});
  }
  out.scopes.sort((a,b)=>a.asset.localeCompare(b.asset)||a.horizon-b.horizon);
  return out;
}

const SOURCE_NAMES=['discloud_forecast','discloud_learning','postgres','drive',
 'github','intelligence','intelligence_host','performance'];
const SOURCE_STATUSES=['ok','unreachable','auth_failed','failed_read','no_records',
 'measured_zero','not_configured','stale'];
const COLLECTION_EVENTS=['schedule','workflow_dispatch','push','workflow_run','unknown'];
const ATTEMPT_OUTCOMES=['accepted','failed','coalesced'];

function cleanCollection(value){
  if(!value||typeof value!=='object')return null;
  if(!COLLECTION_EVENTS.includes(value.event))return null;
  const runId=typeof value.run_id==='string'&&/^\d{5,18}$/.test(value.run_id)?value.run_id:null;
  const attempt=Number.isSafeInteger(value.run_attempt)&&value.run_attempt>=1&&value.run_attempt<=100?value.run_attempt:null;
  const at=isoOrNull(value.attempted_at);
  if(!at)return null;
  return {event:value.event,run_id:runId,run_attempt:attempt,
    attempted_at:at,coalesced:value.coalesced===true};
}

function cleanSources(value){
  if(!value||typeof value!=='object')return null;
  const out={};
  for(const name of SOURCE_NAMES){
    const entry=value[name];
    if(!entry||typeof entry!=='object')continue;
    if(!SOURCE_STATUSES.includes(entry.status))continue;
    const reason=entry.reason===null||entry.reason===undefined?null:
      (typeof entry.reason==='string'&&/^[a-z0-9_]{1,80}$/.test(entry.reason)?entry.reason:null);
    if(entry.reason!==null&&entry.reason!==undefined&&reason===null)continue;
    const checked=isoOrNull(entry.checked_at);
    const fresh=Number.isSafeInteger(entry.freshness_seconds)&&entry.freshness_seconds>=60&&entry.freshness_seconds<=604800?entry.freshness_seconds:null;
    out[name]={status:entry.status,reason,checked_at:checked,freshness_seconds:fresh};
  }
  return Object.keys(out).length?out:null;
}

function cleanAttempt(value){
  if(!value||typeof value!=='object'||value.kind!=='collection-attempt')return null;
  if(!COLLECTION_EVENTS.includes(value.event))return null;
  if(!ATTEMPT_OUTCOMES.includes(value.outcome))return null;
  const at=isoOrNull(value.attempted_at);
  if(!at)return null;
  const runId=typeof value.run_id==='string'&&/^\d{5,18}$/.test(value.run_id)?value.run_id:null;
  const attempt=Number.isSafeInteger(value.run_attempt)&&value.run_attempt>=1&&value.run_attempt<=100?value.run_attempt:null;
  const reason=value.reason===null||value.reason===undefined?null:
    (typeof value.reason==='string'&&/^[a-z0-9_]{1,80}$/.test(value.reason)?value.reason:null);
  if(value.reason!==null&&value.reason!==undefined&&reason===null)return null;
  return {event:value.event,run_id:runId,run_attempt:attempt,
    attempted_at:at,outcome:value.outcome,reason};
}

function rollupSourceHealth(previous,cleanedSources,receivedAt){
  const health=(previous&&typeof previous==='object')?previous:{};
  if(!cleanedSources)return health;
  for(const name of SOURCE_NAMES){
    const entry=cleanedSources[name];
    if(!entry)continue;
    const prev=(health[name]&&typeof health[name]==='object')?health[name]:{};
    if(entry.status==='ok'){
      health[name]={status:'ok',reason:null,
        last_success_at:entry.checked_at||receivedAt,
        last_error_at:prev.last_error_at||null,
        last_reason:prev.last_reason||null,
        consecutive_failures:0,
        freshness_seconds:entry.freshness_seconds||null};
    }else{
      health[name]={status:entry.status,reason:entry.reason||null,
        last_success_at:prev.last_success_at||null,
        last_error_at:entry.checked_at||receivedAt,
        last_reason:entry.reason||null,
        consecutive_failures:(Number.isSafeInteger(prev.consecutive_failures)?prev.consecutive_failures:0)+1,
        freshness_seconds:entry.freshness_seconds||null};
    }
  }
  return health;
}

function appendAttempt(history,entry){
  const entries=Array.isArray(history)?history.filter(e=>e&&typeof e==='object'):[];
  entries.push(entry);
  entries.sort((a,b)=>Date.parse(a.attempted_at||0)-Date.parse(b.attempted_at||0));
  return entries.slice(-72);
}

function build(payload,identity){
  if(!payload||payload.schema_version!==1||typeof payload!=='object')throw Error('invalid_schema');
  const received=new Date().toISOString(),at=isoOrNull(payload.captured_at);
  if(!at||Math.abs(Date.now()-Date.parse(at))>86400000)throw Error('invalid_capture_timestamp');
  const h=payload.host||{},db=payload.database||{},d=payload.drive||{};
  const reports=Array.isArray(d.reports)?d.reports.slice(0,12):[];
  const scopeProgress=cleanXmarketScopeProgress(h.xmarket_scope_progress);
  const cleanReports=reports.map(r=>{
    if(!['BTC','ETH','SOL','XRP'].includes(r.asset)||![5,15,60].includes(r.horizon))return null;
    const s=['blocked','fitted','rejected','accepted','unverified','not_recorded','unknown'];
    return {
      asset:r.asset,horizon:r.horizon,
      status:oneOf(r.status,s),
      family:oneOf(r.family,['xmarket_v1']),
      model_version:r.family==='xmarket_v1' && ['fitted','accepted','rejected'].includes(r.status) &&
        typeof r.model_version==='string' && /^xmarket-[a-z0-9-]{1,79}$/.test(r.model_version)
        ?r.model_version:null,
      artifact_sha256_prefix:typeof r.artifact_sha256_prefix==='string'&&/^[0-9a-f]{12}$/.test(r.artifact_sha256_prefix)?r.artifact_sha256_prefix:null,
      gate_failures:Array.isArray(r.gate_failures)?r.gate_failures.slice(0,4)
        .filter(x=>typeof x==='string'&&x.length>0).map(x=>x.slice(0,120)):[],
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
    collection:cleanCollection(payload.collection),
    sources:cleanSources(payload.sources),
    host:{status:oneOf(h.status,['verified','unverified','unavailable']),
      forecast_status:oneOf(h.forecast_status,['fresh','stale_or_clock_invalid','not_recorded','read_unavailable']),
      learning_status:oneOf(h.learning_status,['fresh','stale_or_clock_invalid','not_recorded','read_unavailable']),
      forecast_issued:numberOrNull(h.forecast_issued),
      learning_settled:numberOrNull(h.learning_settled),
      xmarket_fresh_inputs:numberOrNull(h.xmarket_fresh_inputs,12),
      xmarket_expected_inputs:numberOrNull(h.xmarket_expected_inputs,12),
      xmarket_shadow_rows:numberOrNull(h.xmarket_shadow_rows),
      xmarket_imported_scopes:numberOrNull(h.xmarket_imported_scopes,12),
      xmarket_stage:oneOf(h.xmarket_stage,['not_initialized','waiting_for_inputs',
        'waiting_for_completed_day_export','waiting_for_fit','imported_no_shadow_rows',
        'shadow_issuing','read_unavailable']),
      live_call_delivery:{
        status:oneOf(h.live_call_delivery?.status,['recorded','unavailable']),
        active_channels:numberOrNull(h.live_call_delivery?.active_channels,100000),
        recent_accepted_delivery_rows:numberOrNull(h.live_call_delivery?.recent_accepted_delivery_rows,100),
        advance_model:typeof h.live_call_delivery?.advance_model==='string' &&
          /^[a-z][a-z0-9-]{1,79}$/.test(h.live_call_delivery.advance_model)
          ?h.live_call_delivery.advance_model:null,
        delivery_is_discord_api_acceptance_only:true
      },
      kucoin_gap_recovery:{
        status:oneOf(h.kucoin_gap_recovery?.status,['recorded','unavailable']),
        assets_observed:numberOrNull(h.kucoin_gap_recovery?.assets_observed,4),
        queued_exact_gaps_14d:numberOrNull(h.kucoin_gap_recovery?.queued_exact_gaps_14d,100000),
        due_exact_gaps_14d:numberOrNull(h.kucoin_gap_recovery?.due_exact_gaps_14d,100000),
        attempted_exact_gaps_14d:numberOrNull(h.kucoin_gap_recovery?.attempted_exact_gaps_14d,100000),
        bulk_upstream_empty_assets:numberOrNull(h.kucoin_gap_recovery?.bulk_upstream_empty_assets,4),
        does_not_prove_source_recovery:true
      },
      pending_backlog:{
        status:oneOf(h.pending_backlog?.status,['recorded','unavailable']),
        pending_paths_14d:numberOrNull(h.pending_backlog?.pending_paths_14d,10000000),
        due_pending_paths_14d:numberOrNull(h.pending_backlog?.due_pending_paths_14d,10000000),
        unique_due_targets_14d:numberOrNull(h.pending_backlog?.unique_due_targets_14d,10000000),
      },
      xmarket_exports:cleanXmarketExports(h.xmarket_exports),
      xmarket_scope_progress:scopeProgress,
      recent_forward_calls:cleanForwardCalls(h.recent_forward_calls),
      model_artifacts:cleanModelArtifacts(h.model_artifacts),
      research_loop:cleanResearchLoop(h.research_loop),
      paper_proof:cleanPaperProof(h.paper_proof),
      xmarket_quotes:cleanXmarketQuotes(h.xmarket_quotes),
      xmarket_watchdog:cleanXmarketWatchdog(h.xmarket_watchdog)
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
    intelligence_metrics:cleanIntelligenceMetrics(payload.intelligence_metrics),
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
    // History keeps only bounded trend inputs, not full receipts: 72 points
    // (~1 day at the 10-minute cadence). Drive reports are NOT retained
    // per-point beyond these counts, and per-run full receipts live 90 days
    // under run:<id>. Trend comparisons must gate on schema_version and on
    // the performance sampling_policy carried per point.
    const driveReports=Array.isArray(clean.drive.reports)?clean.drive.reports:[];
    const llm=clean.intelligence_metrics||{};
    const llmMemory=llm.memory||{};
    entries.push({schema_version:1,run_id:identity.runId,captured_at:clean.captured_at,
      host:clean.host,database:clean.database,performance:clean.performance,
      drive:{status:clean.drive.status,reports_considered:clean.drive.reports_considered,
        accepted:driveReports.filter(r=>r.status==='accepted').length,
        rejected:driveReports.filter(r=>r.status==='rejected').length,
        blocked:driveReports.filter(r=>r.status==='blocked').length,
        fitted:driveReports.filter(r=>r.status==='fitted').length},
      intelligence:{status:clean.intelligence.status,host_status:clean.intelligence.host_status,head_sha:clean.intelligence.head_sha},
      llm:{status:llm.status||'unavailable',inference_verified:llm.inference_verified===true,
        ready:llm.ready===true?true:llm.ready===false?false:null,
        memory_pressure_pct:typeof llm.memory_pressure_pct==='number'?llm.memory_pressure_pct:null,
        oom_kills:Number.isSafeInteger(llmMemory.oom_kills)?llmMemory.oom_kills:null,
        repair_count:Number.isSafeInteger(llm.repair_count)?llm.repair_count:null,
        status_latency_ms:typeof llm.status_latency_ms==='number'?llm.status_latency_ms:null}});
    entries=entries.slice(-72);
    await env.EVIDENCE_KV.put('metrics-series',JSON.stringify(entries));

    if(clean.github){await env.EVIDENCE_KV.put('github-snapshot',JSON.stringify(clean.github));}
    await env.EVIDENCE_KV.put('run:'+identity.runId,JSON.stringify(clean),{expirationTtl:60*60*24*90});
    // Reliability bookkeeping (Delivery 1): the last successful signed
    // publication, per-source last-success / last-error, and the attempt
    // history are kept apart from the snapshot so a failed run can still
    // be explained after the fact. An attempt never overwrites 'latest'.
    const attemptEntry={run_id:identity.runId,
      event:clean.collection?clean.collection.event:'unknown',
      attempted_at:clean.collection?clean.collection.attempted_at:clean.captured_at,
      received_at:clean.received_at,outcome:'accepted',reason:null};
    await env.EVIDENCE_KV.put('last-publication',JSON.stringify({
      run_id:identity.runId,captured_at:clean.captured_at,
      received_at:clean.received_at,event:attemptEntry.event}));
    await env.EVIDENCE_KV.put('last-attempt',JSON.stringify(attemptEntry));
    if(attemptEntry.event==='schedule'){
      await env.EVIDENCE_KV.put('last-schedule-attempt',JSON.stringify(attemptEntry));
    }
    const previousHealth=await env.EVIDENCE_KV.get('source-health','json');
    await env.EVIDENCE_KV.put('source-health',JSON.stringify(
      rollupSourceHealth(previousHealth,clean.sources,clean.received_at)));
    const previousAttempts=await env.EVIDENCE_KV.get('attempt-history','json');
    await env.EVIDENCE_KV.put('attempt-history',JSON.stringify(
      appendAttempt(previousAttempts,attemptEntry)));
    return reply({status:'accepted',run_id:identity.runId,captured_at:clean.captured_at},202);
  }
  if(req.method==='POST'&&u.pathname==='/ingest-attempt'){
    const auth=req.headers.get('Authorization')||'';
    if(!auth.startsWith('Bearer '))return reply({error:'authorization_required'},401);
    let identity;
    try{identity=await verifyGithubOIDC(auth.slice(7))}catch(e){return reply({error:'unverified_github_identity',reason:e.message},403)}
    const length=Number(req.headers.get('content-length')||0);
    if(length>4000)return reply({error:'payload_too_large'},413);
    const raw=await req.text();
    if(raw.length>4000)return reply({error:'payload_too_large'},413);
    let attempt;
    try{attempt=cleanAttempt(JSON.parse(raw))}catch(e){return reply({error:'invalid_receipt',reason:'malformed_token'},400)}
    if(!attempt)return reply({error:'invalid_receipt',reason:'invalid_schema'},400);
    const entry={run_id:identity.runId,event:attempt.event,
      attempted_at:attempt.attempted_at,received_at:new Date().toISOString(),
      outcome:attempt.outcome,reason:attempt.reason};
    await env.EVIDENCE_KV.put('last-attempt',JSON.stringify(entry));
    if(entry.event==='schedule'){
      await env.EVIDENCE_KV.put('last-schedule-attempt',JSON.stringify(entry));
    }
    const previousAttempts=await env.EVIDENCE_KV.get('attempt-history','json');
    await env.EVIDENCE_KV.put('attempt-history',JSON.stringify(
      appendAttempt(previousAttempts,entry)));
    return reply({status:'accepted',run_id:identity.runId,outcome:entry.outcome},202);
  }
  if(req.method==='GET'&&u.pathname==='/diagnostics'){
    const [lastAttempt,lastPublication,lastSchedule,health,history]=await Promise.all([
      env.EVIDENCE_KV.get('last-attempt','json'),
      env.EVIDENCE_KV.get('last-publication','json'),
      env.EVIDENCE_KV.get('last-schedule-attempt','json'),
      env.EVIDENCE_KV.get('source-health','json'),
      env.EVIDENCE_KV.get('attempt-history','json')]);
    const attempts=Array.isArray(history)?history.slice(-20):[];
    const sources=(health&&typeof health==='object')?health:{};
    const counters={total_attempts:attempts.length,
      failed_attempts:attempts.filter(a=>a&&a.outcome==='failed').length,
      coalesced_attempts:attempts.filter(a=>a&&a.outcome==='coalesced').length,
      accepted_attempts:attempts.filter(a=>a&&a.outcome==='accepted').length,
      // A genuinely measured zero is a successful read, not a failure.
      sources_failing:Object.values(sources).filter(s=>s&&['unreachable',
        'auth_failed','failed_read','no_records','not_configured','stale'
        ].includes(s.status)).length,
      sources_tracked:Object.keys(sources).length};
    return reply({source:'signed-github-actions',
      collection:{last_attempt:lastAttempt||null,last_publication:lastPublication||null,
        last_schedule_attempt:lastSchedule||null},
      sources,attempts,counters},200,'public,max-age=30');
  }
  return reply({error:'not_found_or_method_denied'},404);
 }
};

