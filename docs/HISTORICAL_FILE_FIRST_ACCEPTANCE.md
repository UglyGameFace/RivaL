# Official historical file-first acquisition: evidence and boundaries

## Actual source support

- **Binance**: The existing `parlay_bot.research.free_history.ingest_binance` already downloads monthly official USD-M futures `5m` ZIP files, checks the accompanying `.CHECKSUM`, validates a bounded CSV, and stores the source-derived research file.
- **Binance 2021+ automation**: `parlay_bot.research.binance_deep_backfill` now advances January 2021 through the last complete UTC month for BTC/ETH/SOL/XRP using the existing provider-published-checksum ZIP importer. Every *new* zero-gap source-month is published into the private Google Drive warehouse and both the data and manifest are read back by SHA-256. It skips exact existing archive manifest names for resume but explicitly labels those as **not reverified during this run**, never as a fully certified 2021-to-present inventory. Unavailable pre-listing months are recorded as missing, not filled.
- **OKX**: `parlay_bot.research.okx_file_first.download_file_month` obtains official one-minute monthly ZIPs from the exchange's live `/api/v5/public/market-data-history` catalog, verifies **both** the named UTC+8 archive months needed to cover the exact UTC month, rejects missing/duplicated minute records, derives 5m OHLCV without interpolating, then publishes an immutable source-native file through the existing SHA-verified Drive research workflow.
- **OKX fallback**: If either ZIP is missing, too large, malformed or cannot reproduce a fully contiguous month, use the existing paced official 5m REST month downloader instead. Explicitly list `archive_fallbacks` for the attempt. Never silently combine conflicting data sources. A failed API fallback still blocks the month rather than marking it verified.
- **Gate**: The existing official monthly gzip pipeline keeps raw source files immutable and qualifies full 5m UTC months only with same-venue API gap evidence and dated contract-multiplier proof. When a Gate 5m bulk download reports HTTP 404, a **single bounded same-Gate public REST attempt per scheduled run** can recover a full UTC month as independent, checksum-readback API response files and a research-only receipt. It uses up to ten requests of 900 5m points, rejects incomplete/duplicate/out-of-window records, and never labels API records as official gzip or authorizes training. Existing Gate raw archive and model-import acceptance paths remain authoritative. Old API history may have expired: a failed fallback is an explicit availability blocker, not grounds to synthesize Gate candles from Binance or OKX.
- **Bitget**: The official `https://www.bitget.com/data-download` center offers historical candles, trades and depth, but the export/download link path and stable schema have not been verified for unattended public access. Do not invent URLs.
- **Hyperliquid**: Public historical S3 exports exist with a different object and data schema. Storage egress and exact checksum eligibility need separate acceptance. No silent cross-venue substitutions.
- **Deribit, dYdX, KuCoin, Lighter, Phemex, Gate and Bitget**: Keep existing venue-native collectors. Do not claim the above OKX archive branch enables their historical model import.

## Trust boundaries

1. Never falsify 2021 history for contracts that did not yet trade, or fill a missing month with another venue's prices under the missing venue's label.
2. Each source/month/contract/quote must have its own verified calendar coverage, origin URL, raw archive SHA-256 and after-ingest immutable Drive byte readback. Local SHA-256 is **not** a provider-signed checksum.
3. OKX ZIPs are requested only from exact exchange-generated HTTPS `static.okx.com/cdn/okex/` catalog URLs. Redirects, excessive archives, additional ZIP members, path traversal, ambiguous timestamp units, duplicate samples, price-envelope failures and incomplete minute calendars fail closed.
4. Archive months use UTC+8 boundaries: a complete UTC May needs the May **and** June archive. This is not a clock correction or synthetic history.
5. Retrospective native history is not forward validation, live order-fill evidence, financial profitability, or an approved model. The existing 21-day forward, 150-outcome, 80%-quote and after-cost acceptance gates remain in place.
6. Continue collecting *new* candles with current API/WebSocket feeds. Archives are generally delayed and are not a real-time prediction feed.

## Production acceptance required

The first scheduled action on `main` must prove at least one real monthly official ZIP successfully produces a full exact 5m UTC grid and is published/read back from connected Drive. If it falls back to REST, inspect the `archive_fallbacks` receipt and repair the specific real-world archive schema, URL, or availability problem. Do not call this feature complete based only on synthetic test ZIPs.

Further source-native backfill, feature-model parity, historical gap inventory and prospective evaluation remain separate milestones.

## Public source links

- https://www.okx.com/en-us/historical-data
- https://www.okx.com/docs-v5/en/
- https://data.binance.vision/
- https://github.com/binance/binance-public-data
- https://www.gate.com/developer/historical_quotes
- https://www.bitget.com/data-download
