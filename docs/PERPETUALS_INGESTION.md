# Hyperliquid perpetuals candle ingestion (first venue)

**Not yet a live production collector.** This slice provides a read-only
HTTP adapter and PostgreSQL-compatible persistence. The sports bot's runtime
remains unaffected; nothing here registers new commands or schedules polling.

- Source: Hyperliquid `POST https://api.hyperliquid.xyz/info` with
  `{"type":"candleSnapshot","req":{...}}`, using no API key.
- Official docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
- Rate policy: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits
- Allowed markets initially: BTC, ETH, SOL **linear perpetuals** in USDC.
- 1m, 5m, 15m, 1h, 4h intervals. Up to 180 requested bars per call, with
  a ten-second HTTP timeout, **no retry loop**, and no exchange credentials.
  Hyperliquid's public endpoint retains only the latest 5,000 candles per
  market/interval. CandleSnapshot consumes base request weight plus extra
  weight proportional to rows returned; do not parallel-poll recklessly.
- Reject mismatched symbols/intervals, broken OHLC ranges, nonfinite
  values, duplicate timestamps and partial candles. Propagate 403 and 429;
  never hide regional or rate-limit failures by inventing data.
- Candles are keyed by independent venue, asset, quote, contract kind,
  interval and candle-opening timestamp. Identical duplicates are skipped;
  changed historical values trigger an error rather than silently rewriting
  training history. Each candle retains `first_ingested_at`, and historical
  queries only return data present at the requested observation time.
- The integration function `collect_hyperliquid(store=..., coin="BTC",
  interval="5m", count=48)` fetches a batch into existing relational storage
  and returns `(closed_count, inserted_count)`. Do **not** confuse GitHub
  mocks or a public documentation example with verified Discloud access.

## Required next work

Verify HTTP access **from Discloud itself**, implement startup-safe
scheduling with per-market cooldown and rate budgeting, add 9 distinct
permitted venue adapters, normalize funding/OI and derivative contract
multipliers, backfill enough point-in-time data, build forecasting features,
training/validation, calibrate and promote models only on independent
walk-forward evidence, then connect scheduled settlement/retraining and the
Discord prediction UI. Until that is done, no live forecasts are claimed.
