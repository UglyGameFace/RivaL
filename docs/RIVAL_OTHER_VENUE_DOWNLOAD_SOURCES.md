# RivaL remaining-venue historical download priority (2026-11)

## Source selection, only after genuine host / schema verification

| Venue | First candidate | Original provenance | Historical families | Next action |
|---|---|---|---|---|
| Bitget | https://www.bitget.com/data-download | Official interactive site, **not file URL** | Futures candles, public trades and depth | Confirm actual bulk file names, symbols, earliest release, downloadable legal terms, then keep original archive |
| KuCoin | https://www.kucoin.com/markets/historydata | Official interactive site; page seems spot-focused | Published candles, trades and depth; **futures bulk not yet proven** | Check futures availability; newer UTA API only exposes sub-8h futures candles since 2025-01 |
| Deribit | https://history.deribit.com/api/v2/public/get_last_trades_by_instrument_and_time | Official historical API, not ZIP | Trades and expired instrument metadata | Verify oldest BTC/ETH/etc trades, immutable per-page hashes and chronological cursor before backfill |
| dYdX | https://indexer.dydx.trade/v4 | Official v4 indexer, not bulk | v4 candles and supported indexer events | Probe range fidelity; keep older v3 exchange distinct from v4 chain identity |
| Lighter | https://mainnet.zklighter.elliot.ai/api/v1/candles | Official REST, not ZIP | Candles; funding endpoint separately | Historical paged fetch, verify market ID and UTC units before model import |
| Phemex | https://api.phemex.com/exchange/public/md/v2/kline/list | Official API, not bulk | Perpetual candles | Check historical endpoint range / retention; trade history may use other endpoint |
| Hyperliquid | https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data | Official L2/book/fills S3, **requester pays** | Book snapshots, contexts, fills / events | Never auto-download with owner's free-only constraints; investigate independently verified cost-free mirror before ingest |
| Lighter secondary | https://github.com/Ankvik-Tech-Labs/lighter-historical-data/releases | **Third-party** release: latest checked 2026-05-29 (stale in Nov 2026), no explicit repository license confirmed | Multiple candle intervals and funding | Not preferred: check license, stale coverage, manifest, official API parity and file bytes before any optional use |

## Operational constraints

- Some records did not exist in 2021 (listing and launch date matter). Never represent an unavailable early month as a source failure or fabricated data.
- No invented static file URLs from web portal buttons. The download may be dynamically generated or require interactive access. Query catalogs before bulk transfers.
- Respect source terms, actual cost, IP/endpoint budgets, Drive quota and file integrity; keep each raw source's bytes, SHA, timestamp, contract/quote, observed time and verified coverage separately.
- Never use a third-party mirror to overwrite or certify original exchange files. Compare samples against the official source and preserve distinct provenance.
- A successful portal/API HTTP GET does not prove bulk history available, and an archived dataset does not imply live profitable model learning. No change to real-time operation or model promotion.
- Reuse the existing per-venue live 5m recovery components. Add archive-first offline research collectors without bypassing their provenance guards or source-specific API fallback.

## Verification

Run `python tools/other_venue_download_discovery.py --probe` for tiny bounded source reachability checks; production GitHub Actions publishes the JSON receipt. No raw archive, market order, or LLM is accessed by the probe.

Official references (source discovery):
- Bitget API data download center: https://www.bitget.com/legacy-docs/uta/data-download-center
- KuCoin download page: https://www.kucoin.com/markets/historydata
- KuCoin UTA historical limits: https://www.kucoin.com/docs-new/v2/rest/ua/get-klines
- Deribit institutional guide: https://statics.deribit.com/files/DeribitInstitutionalSetupGuide.pdf
- Lighter historical documentation: https://apidocs.lighter.xyz/docs/historical-data
- Hyperliquid cost policy: https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data
