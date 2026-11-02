# RivaL ten-venue liquidity and historical-source gap audit (2026-11-02)

**Status: open evidence gaps, not a completion claim.** The authoritative source-family contract is `source_family_coverage.py`: 10 venues × BTC/ETH/SOL/XRP × 16 research families by calendar month. This audit describes active real collectors and remaining genuine original-data gaps. It is not a substitute for a full original-bytes Drive readback or current Discloud PostgreSQL audit.

## The indispensable feature families

- **Trades:** source-labeled price, size, aggressor side where truly published, venue contract size and transaction source timestamps. Only actual events, not interpolated volume or candles borrowed from other markets.
- **Liquidity / L2:** actual best bid/ask, multiple depth levels, spread, top-of-book imbalance, linked order-book snapshots + deltas, quote age and the fraction of each time window actually observed. A current snapshot is **not** a historical order book.
- **Perpetual financing / crowding:** provider-native funding settlement, mark/index/premium, open interest, exchange-identity-verified liquidation history, positioning/long-short metrics. Funding marks, liquidation alerts and open-interest snapshots are distinct units and timelines.
- **Source safety:** trading pair and quote identity, actual listing and contract multiplier history, timezone/UTC boundaries, fee and historical contract-settings vintages, immutable source bytes and source/Drive SHA readbacks, publication/first-observation time, provider quotas/costs, exact missing-period masks, no retroactive live features.
- **Evaluation:** source-specific history is eligible for research only after actual completeness and provenance. No model promotion or execution without future-only matched quotes, uncertainty and the existing 21-day/150-target/80%-quote after-cost controls.

## Original acquisition and unresolved limitations by venue

| Venue | Verified implementation paths available in repository | Important unverified or missing source families |
|---|---|---|
| **Binance** | Official 2021+ monthly ZIP candles; supplemental funding, mark/index/premium, daily positioning with provider checksums and Drive receipts | Confirm all reachable monthly source families through October 2026; historical L2 and exchange liquidation tape need independently authentic and license-compatible original sources |
| **Bitget** | V3 historical market/mark/index/premium, recent native trades, live 100-level/RPI depth, open interest + history, funding, liquidation samples, original SHA stored in Drive | 2021 deep-history coverage not proven; live depth snapshots do **not** imply old L2 continuity; keep slower deep-history independent of all other venues |
| **Deribit** | Public history trading API, bounded source-native trade archives; hourly native historical work, separate live candle recovery | Multi-year complete BTC/ETH/SOL/XRP contract coverage, old option surface/IV, historical books and liquidations unproven; instrument listing and expiry metadata mandatory |
| **dYdX** | V4 indexer historical candles plus historical funding/oracle event pages stored source-exact | V4 history begins no earlier than the actual v4 launch (2023, not 2021). Earlier v3 is a distinct product, not a relabeling opportunity; historical books, OI and liquidation coverage require native reconstruction evidence |
| **Gate** | Official 1m/5m/1h futures files, trades, funding updates/applies, mark prices; authenticated API fallback; exact 1m-to-5m gap evidence and new bounded missing 5m API witness | Real Mar 2023 SOL 5m raw has two missing rows also absent in original 1m; live 5m historical API returned HTTP 400 in PR smoke. Keep missing. Historical contract multiplier/settings and source-native old L2 book archive remain unqualified |
| **Hyperliquid** | Live context including OI/funding and recent 5m recovery; separate published historical S3 documentation | API only retains roughly the latest 5000 candles. Old official data may charge requester-pays transfers; no paid S3 acquisition or claimed old L2 without a genuinely free original-source path |
| **KuCoin** | Venue-specific live/history recovery, source-exact identity and gap audits | Official bulk **futures** old trade/L2/depth history not verified; do not treat the official spot history portal as perpetual evidence |
| **Lighter** | Perpetual historical 5m recovery plus original funding and hourly mark-price public API samples; monthly rotation and Drive readback | Add January 2025 as attempt floor (not asset launch proof). Recover months in 2025/26 with explicit partial hourly masks; trade tape, old full-depth L2 and liquidations not qualified from latest 12 successful October readbacks |
| **OKX** | Official one-minute file first, 5m API fallback and SHA-readback; auxiliary archive trade, funding, order-book snapshot/delta replayer with sequence checks | Per official archive page, trades from **Sep 2021**, funding **Mar 2022**, high-resolution L2 **Mar 2023**, 5m candle target **Jul 2023**, subject to actual contract listing. 400-level monthly L2 catalogs may return provider 50016, and daily L2 may be too large for free-byte budget. Count archive receipts only when exact files have been truly downloaded and rehashed |
| **Phemex** | Source-native candle recovery through the documented public market endpoint | Old native executions, liquidation/open-interest and L2 source archive retention is not proven; request only provider-supported public history |

## Implementation and operational boundaries

1. Do not create a second live venue collector for an endpoint already used by the hosted bot. Reuse current source-native acquisition, future-only feature and Drive manifest pathways.
2. **PR #247** addresses OKX **advanced** source-era floors + name-only resume hints, Gate residual authentic 5m API check, and Lighter January 2025 attempt + missing-hour reporting. These are *collector repairs*, not proof the missing data is now in Drive.
3. Keep **Issue #188** open until real month x venue x contract x family source receipts and independent Drive byte readback demonstrate the true obtainable coverage. An all-10 combined *feature* can use available source-labeled signals but cannot certify ten full identical 2021-to-2026 datasets.
4. Current November 2026 month is unfinished; live timestamped capture should continue separately while archived complete calendar months end at October 2026.
5. Keep free-only safeguards. Order books and liquidations that were never archived publicly remain **not recoverable from public historical API**, rather than defaulting to fabricated prices. Prioritize accessible official trade/funding/OI/L2 archive families and visibly report restricted/unavailable slots.

## What to verify after merge

- One exact-main passing CI and source-family workflows, plus source-specific post-merge Drive receipts and independent SHA checks.
- OKX: earliest *actual* cataloged source-month per asset/module, whether source file is publicly downloadable under byte budget; audit obsolete duplicate archive jobs.
- Gate: genuine March 2023 SOL missing 5m slots and the provider's HTTP 400 reason, stable exact source hashes; report blocked without training admission.
- Lighter: monthly hour counts for each asset × funding/mark family, launch-date proof, and any API time-window truncation that needs bounded pagination.
- All other venues: per source-month ledger for original L2/depth snapshots/deltas, trade tape, OI, funding, liquidations and contract metadata; independent full-history proof remains open.
