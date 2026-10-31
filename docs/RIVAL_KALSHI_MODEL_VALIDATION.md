# Kalshi forward model and DraftKings integration status

## Why there was no Kalshi model

Before this change, RivaL's Kalshi integration contained a public REST market browser only. It did not persist an original Kalshi contract quote, independently reconcile that specific contract's eventual settlement, train on earlier labels, or score its own frozen candidate forecasts. Polymarket and exchange-candle models cannot be presumed valid for Kalshi's YES/NO event contracts.

Kalshi's official public REST endpoints provide series, open markets and market details (including status/result/settlement_ts); no Kalshi account or API key is needed for these **read-only market data** requests: https://docs.kalshi.com/getting_started/quick_start_market_data .

## New data and experimental learning

- On an owner-initiated **Browse Kalshi** action, the existing browser now asynchronously records the first observed YES and NO ask quote and exact Kalshi contract ticker, matching official series, published rules, close timestamp and a source-evidence SHA. It reads just four qualifying open crypto contracts at most.
- In the same bounded asynchronous background research task, it checks up to two previously observed, due-to-close contracts using the documented public GET market endpoint. Only an official `status=settled` and unambiguous `result=yes/no` with matching ticker and valid settlement timestamp produce a learning label. Unknown/missing/void outcomes are excluded; first quote and first candidate never rewritten.
- A separate permanent Kalshi logistic challenger runs **per asset AND exact series**, not per Polymarket token or unrelated crypto trading venue. A new candidate may issue a frozen paper probability only after **100 previously verified contract outcomes** within that series. Future candidate Brier score is compared with the normalized actual Kalshi quote baseline.
- Current screen shows observed, settled and independently scored contract counts. It remains **browse-only/no approved call**, and correctly reports no validated Kalshi-specific RivaL model until 150 prospective candidate outcomes, at least 21 independent days, real market-matched improvement, realistic fees/spread and other promotion controls are verified.
- No app credentials, no real betting, no automatic user DMs and no new server-wide background polling. The observer is limited to browsing sessions and will accumulate evidence at that cadence; it is not a substitute for a verified full historical dataset/backfill.

## DraftKings Predictions remains separate

RivaL's DKeX research importer records official daily market and settlement **reports** only. The DraftKings Predictions app may route events across DKeX or other exchanges; public daily reports are not real-time executable bid/ask quotes or a complete DraftKings Predictions crypto contract catalog.

Thus DraftKings remains not connected for contract-specific forecasts. Before adding real DraftKings calls we must identify its authorized live contract/quote access, exact routing, settlement rules and final outcome receipts, correct timestamps, fees and jurisdiction, then preserve independent historical and forward prediction proof. RivaL must never substitute Kalshi or Polymarket market quotes.

## Production acceptance

Verify Ruff and the full PostgreSQL-backed test suite, exact main commit and deployment action. Browse a currently open **real** crypto series in Discord; verify the first Kalshi observation is stored with original Kalshi ticker and rules. After it settles, verify official GET result/timestamp and a single immutable outcome. Validate series-specific training/forward Brier only on strictly future windows. Do not call a model approved or profitable based solely on having a training pipeline.