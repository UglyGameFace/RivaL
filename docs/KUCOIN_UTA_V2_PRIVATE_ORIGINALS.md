# KuCoin UTA V2: additional public historical sources

This workflow adds an original-source route for eight previously unrouted
KuCoin price-data families, across BTC, ETH, SOL and XRP:

- Trade candles at 1m, 15m, 1h, 4h and 1d. Existing 5m recovery is untouched.
- Mark-price, index-price, premium-index at 1h from KuCoin UTA V2 `kline`.
- A separate ninth route, **open interest**, only in recent retained windows
  (1h: at most 7 days; 1day: at most 70 days). It is impossible to use this
  public endpoint to recover 2021 open interest.

Each of the eight new historical price-family routes is counted separately for
each asset (32 newly routed scopes). Recent OI adds four more routed scopes.
The overall 640-scope coordinator advances from 188 to 224 *routed*, with
416 still lacking historical collection routes. None of these are equivalent
to 224 fully downloaded or qualified data scopes.

## Source and retention boundaries

Current official KuCoin documentation states that futures intervals under 8
hours are publicly queryable only beginning 2025-01-01. The daily candle route
attempts older months independently; this does **not** prove an actual contract
listing date, a month of trading or that specific older source data exists.
No candle or index is manufactured for no-tick intervals. Query scopes are
bounded to a maximum of 180 candles within the provider's 200-record cap.
Missing/empty/truncated/invalid pages remain incomplete.

KuCoin UTA V2 provides public `klineType=TRADE|INDEX_PRICE|MARK_PRICE|PREMIUM_INDEX`;
a separate OI endpoint requires explicit `interval` and exact symbol. The
collector enforces source-specific UTC timestamps, contract identity, sane
numbers, and readbacks of both original raw API bytes and immutable manifest.

Official references:
- https://www.kucoin.com/docs-new/v2/rest/ua/get-klines
- https://www.kucoin.com/docs-new/v2/rest/ua/get-futures-open-interest
- https://www.kucoin.com/docs-new/change-log

## Private storage, no public archive download

Every accepted original is stored **only in the existing private RivaL
Google Drive warehouse** with a verified post-upload byte readback. Before
uploading, the collector verifies root/history/manifest/staging folder
permissions are not anyone/domain shared. After upload it verifies each
original and manifest is not link/domain shared. If permission inspection
fails, the collector fails closed. It does not create public Drive links,
publish original files or attach raw bytes to public GitHub Actions runs.

GitHub Actions publishes **only a metadata progress receipt**, which can be
read by repository observers. It never uploads raw originals, provider JSON,
source manifests, credential files or original download URLs as artifacts.

These safeguards prevent the *user-owned stored copies* from being made
publicly downloadable by this workflow. They cannot make **public exchange
market data** unavailable for other people to download directly from KuCoin,
and anyone with authorized Drive permissions can still access their copies.

## Operations

The four-times-daily GitHub workflow `kucoin-uta-v2-originals.yml` selects
one historical family per run, rotates source months and original page windows,
and retains recent OI separately via manual `family=open_interest`. Manual
runs can specify a completed UTC month for candles. This is independently
rate-bounded and never stops the main bot, its live 5-minute collector or
other exchanges' source collectors.

Only source receipts, not model weights, are changed. Real model admission,
futures qualification, after-cost superiority, full historical coverage and
live order execution remain unchanged. Missing trades, liquidations, and
historical L2 depth still require their own authentic source routes.
