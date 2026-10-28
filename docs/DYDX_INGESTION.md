# dYdX v4 public perpetual-candle ingestion

Status: second **independent** public data-source adapter within the RivaL
perpetuals-conversion task. It is a manually triggered, read-only source probe,
not scheduled ingestion or a trained prediction system. Kraken excluded.

## Official documentation

- [dYdX v4 Indexer market/candle API](https://docs.dydx.exchange/api_integration-clients/indexer_client)
- [dYdX Indexer API reference](https://indexer.dydx.trade/docs/)
- [Indexer architecture](https://docs.dydx.exchange/concepts-architecture/indexer)

The adapter uses the read-only v4 Indexer:
`GET https://indexer.dydx.trade/v4/perpetualMarkets` and
`GET https://indexer.dydx.trade/v4/candles/perpetualMarkets/{ticker}`
with `resolution` and `limit`.

Unlike Hyperliquid, its market symbols here are BTC-USD, ETH-USD, and SOL-USD
and its candle data is **USD-quoted**, not USDC. Even if both exchanges report
nearly the same BTC price, observations are never merged as if they represent
the same contract. Historical candle volumes are base-asset volume, not USD.

## Run safe read-only probe from the real Discloud host

    python -m parlay_bot.perpetuals.probe --provider dydx --assets BTC ETH SOL --bars 48

The command makes public GET calls only. It prints a minimal summary, and
does not write data. If this is reachable, authorized, and its responses
pass validation, an explicit bounded ingest can be invoked:

    python -m parlay_bot.perpetuals.probe --provider dydx --assets BTC ETH SOL --bars 48 --persist

The opt-in `--persist` command uses RivaL's **existing PostgreSQL**
configuration, writing immutable, idempotent records into
`perp_market_candles`. It does not modify sports tables, and does not
create market-context rows for dYdX because this adapter does not supply
matching fields/semantics. No fabricated funding rate, mark or open interest.

Bounds: max 10 uppercase market symbols, 99 completed candles per request,
5/15/60/240-minute intervals, one market-list request and one candle request
per asset, no retry loop. Closed, aligned, finite-price OHLC candles only.
Bad/unavailable/blocked/429 results fail closed; do not route around any
regional restriction or rate limit. Data freshness and gaps must be monitored
before using the source for live forecasting.

## Remaining work

The **two adapters** are not proof of two production-available sources:
Discloud regional access is unverified, and live schema may differ from
offline mocks. Before enabling scheduled ingestion, verify both feeds from
the deployment host. Add the remaining independent venues and model-health
monitoring, time-safe features and holdout evaluations, calibrated forecasts,
a verified settlement loop, rollback/challenger promotion, the Discord
perpetuals UI, and operational canary validation.
