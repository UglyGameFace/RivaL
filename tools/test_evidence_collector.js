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
 const payload={schema_version:1,captured_at:now,host:{xmarket_stage:'waiting_for_completed_day_export',
  xmarket_exports:{recent_completed_days_pending:4,eligible_before:now,
   published:[{asset:'BTC',exported_days:1,latest_exported_day:'2026-11-12',drive_file_id:'PRIVATE'}],
   pending_by_asset:[{asset:'ETH',days:1}],password:'PRIVATE'}},
  drive:{reports:[{asset:'BTC',horizon:5,reason:'insufficient_exact_pairs'}]},
  performance:{status:'observed',captured_at:now,sampling_policy:'per-asset-horizon-v2',
   scopes:[{asset:'BTC',horizon:5,model_version:'prospective-logistic-persistent-v3',
    paired_outcomes:100,model_brier:.2,baseline_brier:.3,unique_target_windows:20,venue_count:5}]}};
 const records=new Map();
 const env={EVIDENCE_KV:{get:async key=>records.has(key)?JSON.parse(records.get(key)):null,
  put:async(key,value)=>{records.set(key,value);}}};
 const make=(token,body=payload)=>new Request(claims.aud+'/ingest',{method:'POST',
  headers:token?{Authorization:'Bearer '+token}:{},body:JSON.stringify(body)});
 assert.equal((await cx.worker.fetch(make(null),env)).status,401);
 assert.equal((await cx.worker.fetch(make(await sign({...claims,ref:'refs/heads/other'})),env)).status,403);
 const valid=await sign(claims);
 const parts=valid.split('.');
 const corrupted=Buffer.from(parts[2],'base64url');corrupted[0]^=1;parts[2]=corrupted.toString('base64url');
 assert.equal((await cx.worker.fetch(make(parts.join('.')),env)).status,403);
 assert.equal(records.size,0,'Unverified callers must never change receipts');
 assert.equal((await cx.worker.fetch(make(valid),env)).status,202);
 const receipt=JSON.parse(records.get('latest'));
 assert.equal(receipt.provenance.verified,true);
 assert.equal(receipt.host.xmarket_exports.recent_completed_days_pending,4);
 assert.equal(receipt.host.xmarket_stage,'waiting_for_completed_day_export');
 assert.equal(receipt.host.xmarket_exports.published[0].latest_exported_day,'2026-11-12');
 assert.equal(receipt.drive.reports[0].reason,'insufficient_exact_pairs');
 assert.equal(receipt.performance.scopes[0].horizon,5);
 assert.equal(receipt.performance.scopes[0].unique_target_windows,20);
 assert.equal(receipt.performance.sampling_policy,'per-asset-horizon-v2');
 assert.equal(receipt.performance.after_cost_qualified,false);
 assert.ok(!records.get('latest').includes('PRIVATE'));
 assert.equal(records.get('run:'+claims.run_id),records.get('latest'));
 const unavailable=cx.build({schema_version:1,captured_at:now},{runId:'123456789'});
 assert.equal(unavailable.host.xmarket_exports,null);
 assert.equal(unavailable.host.xmarket_imported_scopes,null);
 assert.equal(unavailable.performance.status,'unavailable');
 console.log('Evidence collector signed round-trip, redaction and rejection regressions passed');
}
main().catch(error=>{console.error(error);process.exitCode=1});
