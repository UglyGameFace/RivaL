/* Read-only RivaL market bridge API tests. No real API/network needed. */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'..','web','evidence-board','worker.js'),'utf8');
const start=source.indexOf('const REPOS = ');
const end=source.indexOf("addEventListener('fetch',",start);
assert.ok(start>=0&&end>start,'Expected Service Worker backend source');
const script=new vm.Script(source.slice(start,end),{filename:'rival-dashboard-backend.js'});
class FakeRequest {
  constructor(url,options){this.url=url;this.method=options?.method||'GET'}
}
class FakeResponse {
  constructor(body,options){this.text=String(body);this.status=options.status;this.headers=options.headers}
  async json(){return JSON.parse(this.text)}
}
const now=Date.now();
const paths=[];
const market={
  generated_at:now,
  venues:[{id:'okx',name:'OKX',status:'ok',latency_ms:25},{id:'binance',name:'Binance',status:'blocked',http_status:403}],
  assets:{BTC:{median:64000,chg24_median_pct:-2,venues_used:8,dispersion_bps:3.4,
      spread_median_bps:0.15,vol24_usd_total:10000000000,oi_usd_total:450000000,
      api_token:'secret_market_field',
      rows:[{venue:'okx',status:'ok',bid:63999,ask:64001,mid:64000,funding:0.0001,
        spread_bps:0.15,oi_usd:450000000,latency_ms:1518,
        private_data:'secret_market_field'}]}}
};
const poly={
  generated_at:now,
  contracts:[{asset:'BTC',horizon:5,title:'BTC 5m',url:'https://polymarket.com/event/btc-5m',
    closes:now+300000,status:'ok',accepting_orders:true,
    up:{best_ask:.52,best_bid:.49,ask_depth:1200},down:{best_ask:null,best_bid:.51,ask_depth:0},
    up_token_id:'private_unwanted_field',book_age_ms:34},
    {asset:'ETH',horizon:5,title:'not the requested market',url:'https://evil.example/contract'}]
};
const kalshi={
  generated_at:now,quote_source:'kalshi_market_summary',
  assets:{BTC:{open_count:1,series:['KXBTC'],markets:[{
    ticker:'KXBTC',title:'BTC above threshold',close_time:now+7200000,
    yes_bid:.46,yes_ask:.54,no_ask:.55,volume_24h:100,oi:25,
    account_metadata:'secret_market_field'
  }]}}
};
function setup(fn){
 const calls=[];
 const context={
  Request:FakeRequest,Response:FakeResponse,URL,console,Date,Math,Number,String,
  RIVAL_MARKET_FEED:{
   fetch:async(req)=>{
    assert.equal(req.method,'GET','The bridge may only issue GET requests');
    const u=new URL(req.url);calls.push(u);
    assert.equal(u.origin,'https://rival.the420lobby.com');
    if(u.pathname==='/api/perps')return {ok:true,status:200,json:async()=>market};
    if(u.pathname==='/api/polymarket')return fn==='poly-fail'?{ok:false,status:502}: {ok:true,status:200,json:async()=>poly};
    if(u.pathname==='/api/kalshi')return {ok:true,status:200,json:async()=>kalshi};
    throw Error('Unallowlisted upstream route: '+req.url);
   }
  }
 };
 vm.createContext(context);
 script.runInContext(context,{timeout:1000});
 return {context,calls};
}
async function main(){
 {
  const {context,calls}=setup();
  const r=await vm.runInContext('marketPerps()',context);
  assert.equal(r.status,200);
  const d=await r.json();
  assert.equal(d.source,'rival-public-site');
  assert.equal(d.assets.length,4);
  assert.equal(d.assets[0].asset,'BTC');
  assert.equal(d.assets[0].price,64000);
  assert.equal(d.venues[1].status,'blocked');
  assert.equal(d.assets[1].price,null,'Missing quotes are null, not zero');
  // D3: per-venue quote age is the source age (upstream calls it latency_ms),
  // never a fabricated zero; receipt-backed quotes are labelled as such.
  assert.equal(d.assets[0].venues[0].quote_age_ms,1518);
  assert.equal(d.assets[0].venues[0].quote_kind,'indicative');
  assert.equal(d.assets[0].venues[0].open_interest_usd,450000000);
  assert.equal(calls.length,1);
  assert.equal(calls[0].pathname,'/api/perps');
  assert.ok(!JSON.stringify(d).includes('secret_market_field'),'Only allowlisted public fields may be exposed');
 }
 {
  const {context,calls}=setup();
  const r=await vm.runInContext("marketPredictions('BTC')",context);
  assert.equal(r.status,200);
  const d=await r.json();
  assert.equal(d.asset,'BTC');
  assert.equal(d.polymarket.contracts.length,1,'No other asset may leak');
  assert.equal(d.polymarket.contracts[0].up_ask,.52);
  assert.equal(d.polymarket.contracts[0].down_ask,null,'No ask stays unknown');
  // D3: market-implied probability is the carried Up-book mid only; a
  // missing side makes the contract unusable as a baseline, never zero.
  assert.equal(d.polymarket.contracts[0].market_up_prob,.505);
  assert.equal(d.polymarket.contracts[0].quote_state,'missing_side');
  assert.equal(d.polymarket.contracts[0].usable,false);
  assert.equal(d.polymarket.contracts[0].resolution_rule,null);
  assert.equal(d.polymarket.contracts[0].resolution_rule_source,'not_carried_by_feed');
  assert.equal(d.polymarket.contracts[0].model_linkage,'proximity_only_no_contract_id');
  assert.equal(d.kalshi.markets[0].yes_ask,.54);
  // D3: Kalshi summary fields are never executable book quotes.
  assert.equal(d.kalshi.markets[0].yes_implied_prob,.5);
  assert.equal(d.kalshi.markets[0].quote_kind,'market_summary_not_executable');
  assert.equal(d.kalshi.markets[0].executable,false);
  assert.equal(d.kalshi.markets[0].resolution_rule,null);
  // D3: DraftKings stays explicitly disconnected without a verified source.
  assert.equal(d.draftkings.status,'disconnected');
  assert.equal(d.draftkings.reason,'no_authorized_verified_source');
  assert.deepEqual(d.draftkings.contracts,[]);
  assert.equal(d.kalshi.quote_source,'kalshi_market_summary','Do not pretend summary quote is executable book');
  assert.ok(!JSON.stringify(d).includes('secret_market_field'));
  assert.ok(!JSON.stringify(d).includes('private_unwanted_field'));
  assert.deepEqual(Array.from(calls.map(u=>u.pathname)).sort(),['/api/kalshi','/api/polymarket']);
  assert.equal(calls[0].searchParams.get('asset'),'BTC');
 }
 {
  const {context,calls}=setup();
  const r=await vm.runInContext("marketPredictions('DOGE')",context);
  assert.equal(r.status,400);
  assert.equal(calls.length,0,'Unsupported assets must not call upstream');
 }
 {
  const {context}=setup('poly-fail');
  const r=await vm.runInContext("marketPredictions('BTC')",context);
  assert.equal(r.status,200,'One failed public venue must not conceal the other');
  const d=await r.json();
  assert.equal(d.polymarket.status,'unavailable');
  assert.equal(d.kalshi.status,'ok');
 }
 {
  const {context}=setup();
  context.RIVAL_MARKET_FEED=undefined;
  const r=await vm.runInContext('marketPerps()',context);
  assert.equal(r.status,503,'Missing binding must fail closed, not fabricate quotes');
 }
 assert.match(source,/req\.method!=='GET'&&req\.method!=='HEAD'/);
 assert.match(source,/RIVAL_MARKET_FEED\.fetch/);
 assert.doesNotMatch(source,/\/api\/market\/private/);
 console.log('RivaL operations API read-only and allowlist regressions passed');
}
main().catch(e=>{console.error(e);process.exitCode=1});
