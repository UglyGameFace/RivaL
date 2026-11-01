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
twice daily, with GitHub Actions serialization and a 180-minute job timeout.
Automatic source collection is still subject to GitHub Actions availability,
provider availability, archive storage quota, and `RIVAL_NATIVE_PUBLIC_BOOTSTRAP_ENABLED`
configuration.

**Important:** No historical 2021–present availability is invented. Exchange
instrument listing date, public archive start date, retention, complete-month
check, verified historical book sequence, and downstream retrospective
feature benefit are *separate* acceptance requirements.
