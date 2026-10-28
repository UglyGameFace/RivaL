# Hyperliquid public-market ingestion (first live provider)

## Scope

This is RivaL's first read-only, bounded, manually invoked collector, not the
ten-provider model or the continuously learning prediction bot. Kraken is
excluded. The current Discord sports dashboard, PostgreSQL service and
prediction ledger are intentionally unchanged.

Official docs:
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits

## Live Discloud access probe

In the authorized RivaL Discloud container/shell, run:

    python -m parlay_bot.perpetuals.probe --assets BTC ETH SOL --interval 5 --bars 48

The default is **no database writes**. It calls only public, no-key Info
endpoints (metaAndAssetCtxs and candleSnapshot), verifies market contracts
and OHLCV, funding, mark/oracle, and open interest, and prints a
non-sensitive summary. Access restrictions, HTTP 429, invalid responses and
unavailable assets fail closed, without bypasses or synthetic values.

After a successful probe, explicitly write bounded history with:

    python -m parlay_bot.perpetuals.probe --assets BTC ETH SOL --interval 5 --bars 48 --persist

This requires the production RIVAL_STORAGE_BACKEND=postgres and private
RIVAL_DATABASE_URL. Two new isolated tables (perp_market_candles and
perp_market_contexts) are created. The first insert is immutable; identical
retry is idempotent; a closed candle that later changes raises an integrity
error. Existing sports and archive tables are untouched.

## Data and limits

Each record retains venue, contract identity, quote currency (USDC), UTC
interval and observation time. Candle volume and open interest are base-asset
units; funding is an exchange-reported snapshot, not a backfilled history.
An incomplete/current candle is rejected; missing time buckets are not filled.
The snapshot endpoint retains at most its latest 5,000 candles, and this
collector limits each request to 200 closed candles and 10 markets. Hyperliquid
documents a shared IP budget of 1,200 REST request weight per minute; most
Info calls cost at least 20 plus additional candle-response weight. No polling
or automatic retry runs in this slice.

Mock HTTP and PostgreSQL 16 integration tests do NOT prove Discloud regional
access. That requires the one-shot probe on the real host. Do not enable
trading permissions, bypass geographic blocks or claim predictive edge.

## Continued single active task

After source availability is verified on Discloud, introduce additional
permitted venue adapters, normalized history, controlled low-rate scheduling,
point-in-time feature training, an audited champion/challenger model process,
matured-outcome settlement to the existing prediction ledger, calibration and
walk-forward comparisons, and the new Discord command. A model must not be
promoted merely because it trained successfully; benchmark and held-out
results must justify promotion. Continuous learning is not a guarantee of
increasing accuracy.
