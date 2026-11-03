# Historical source throughput and integrity

This is a **per-provider budgeting ledger**. It does not imply any provider
has data as far back as January 2021, and it does not authorize live trades.

| Source | Public historical request guidance | Implementation rule |
| --- | --- | --- |
| OKX | `/api/v5/market/history-candles`: 20 requests / 2 seconds / public IP; max 100 candles/page | `okx_bulk_bootstrap` owns one shared paced IP budget across BTC/ETH/SOL/XRP. Default 0.16 s between all public requests (12.5 requests/2 seconds, 37.5% headroom), 429/50011 cooldown and bounded retries. |
| Binance USDⓈ-M | `/fapi/v1/klines` weight varies by LIMIT (e.g., 2 for 100–499, 5 for 500–1000); shared per-IP quota; response `X-MBX-USED-WEIGHT-*` | Prefer official monthly/daily archives (data.binance.vision) for whole-history backfill. Track live weight headers independently. No synthetic candles. |
| Bitget futures | `/api/v2/mix/market/history-candles` 20/s/IP; max 200 rows; updated v3 also 20/s/IP | One provider-specific token budget. Do not use OKX requests/second for Bitget. |
| Gate futures | Public endpoints 200 requests / 10 s / endpoint / IP in current v4 docs | Per-endpoint budget using the applicable current regional limit. |
| Hyperliquid | Weighted REST budget 1200/min/IP, most `info` calls weight 20; candleSnapshot has additional per-60-returned-items cost | Weighted budget and quota-aware page accounting; websocket for forward live market data. |
| KuCoin futures | Futures kline query consumes 3 public-pool units; up to 500 rows for one legacy endpoint, newer UTA futures paths differ | Use authenticated response-limit/pool metadata and endpoint-specific page size; verify historical retention first. |
| Bybit (alternative) | Default IP budget 600/5s plus per-endpoint/UID limits and `X-Bapi-Limit-Status`; up to 1000 candles/page | Enforce minimum of all applicable limits and observe headers. |
| Phemex | 5000 requests / 5 minutes / IP plus API-group-weight limits | Enforce both; use `x-ratelimit-retry-after-*` when blocked. |
| Deribit | Credits are dynamic by tier, method, and authentication; public unauthenticated traffic is IP-limited | Never assume a static generous limit. Prefer confirmed available credits. |
| dYdX, Lighter | No trustworthy universal historical endpoint allowance established | Keep conservative current request limits; no auto-raise without actual documented budget and measurement. |

Official documentation:
- OKX: https://www.okx.com/docs-v5/en/
- Binance futures: https://developers.binance.info/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data
- Binance bulk history: https://github.com/binance/binance-public-data
- Bitget: https://www.bitget.com/docs/catalog/classic-contract-market/classic-contract-market
- Gate: https://www.gate.com/docs/developers/apiv4/en/
- Hyperliquid: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits
- KuCoin: https://www.kucoin.com/docs-new/rest/futures-trading/market-data/get-klines
- Bybit: https://bybit-exchange.github.io/docs/v5/rate-limit
- Phemex: https://github.com/phemex/phemex-api-docs/blob/master/Generic-API-Info.en.md
- Deribit: https://deribit1692970334.zendesk.com/hc/en-us/articles/33675331213853-Rate-Limits

## Autonomous OKX historical acceptance

The automated job advances four assets independently on a **single runner** with
one shared IP pace, source-native UTC 5-minute coverage checking, immutable
raw/manifest checksums, and private Drive remote readback before declaring a
month verified. A failure is reported explicitly and does not authorize
partially missing months, replacement data, model promotion, or simulated fills.

The older daily single-asset runner is manual-only to prevent competing for
the same OKX public IP budget. Runs are bounded to 3 passes x 6 months/asset
every six hours, with GitHub Actions serialization and a 180-minute job timeout.
Automatic source collection is still subject to GitHub Actions availability,
provider availability, archive storage quota, and `RIVAL_NATIVE_PUBLIC_BOOTSTRAP_ENABLED`
configuration.

**Important:** No historical 2021–present availability is invented. Exchange
instrument listing date, public archive start date, retention, complete-month
check, verified historical book sequence, and downstream retrospective
feature benefit are *separate* acceptance requirements.

## Accelerated, source-safe archive collection (2026-11)

The Gate public archive collection now schedules **six serialized runs per
day**, staggered from Binance. Each Gate run is capped at **32 newly
readback-verified archives** and **48 source attempts**; the existing
recertification/5m-gap/advanced-family budget is preserved, as are the
provider-throttle stop, same-Gate API fallback, and original-byte SHA checks.
At a *perfect* 32 new archives per run, 764 pending source-family jobs take
at least `ceil(764 / (6 * 32)) = 4` calendar days of full batches.
This is a theoretical throughput floor, **not a delivery promise**: 404s,
historical contract metadata gaps, unavailable source files, Google Drive
limitations and GitHub scheduling affect realized progress.

Binance's separate official-archive worker also schedules **six serialized
runs per day** capped at **32 new source-month archives per run**, without
raising REST request weights. Both jobs remain single-run per provider,
preserve the existing off switches and avoid replacing source-specific history
with another venue's prices.

This does **not** finish or certify every venue/data family. OKX's confirmed
2023–present five-minute archive scope is separate from its trades/funding/L2
archive work; other venues need source-family month-by-month evidence.
Training compatibility and future-only performance remain independent gates.
Track actual new archives, rejected attempts, valid complete months, exact
instrument start dates and model admissions before calling any family done.

## Time-budgeted, self-continuing Binance archive runs (2026-11-03)

Binance's official monthly archives at data.binance.vision are static files,
not the weighted REST API, so the two Binance archive collectors are bounded
by **time and storage** rather than a small file count:

- Each run works for up to **45 minutes**, sequentially, with at least
  0.25 s between archive requests, and keeps every existing checksum, UTC-grid
  and Drive readback check.
- A run that stops on its time budget with work left and progress made
  **dispatches the next run itself** (at most 40 links per chain). Scheduled
  runs every 6 hours only restart a chain GitHub dropped; the coordinator also
  lists the two Binance core collectors first while months are missing.
- Collection stops before the Drive account passes **90% full**. An
  unreadable quota does not block collection.
- The month floor is **2019-09**, the first Binance USD-M perpetual month.
  Later-listed contracts return 404 for earlier months; those are reported as
  unavailable and never filled.
- Premium index klines are fetched from Binance's real dataset name,
  `premiumIndexKlines`. The previous `premiumPriceKlines` path returned 404,
  so no premium month had been collected before this change.

GitHub dropped most cron runs in practice (5 of 24 expected Binance candle
runs and 1 of 12 auxiliary runs in the 48 hours to 2026-11-03), which is why
continuation no longer depends on the schedule.
