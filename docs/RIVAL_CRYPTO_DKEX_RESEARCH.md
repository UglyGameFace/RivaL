# Crypto research and DKeX data-evidence milestone

## Why this change

The previous monthly six-month run requested every BTC/ETH/SOL time interval AND ten old football league-season archives. It failed when football downloads were unavailable, even though the crypto partitions completed. Crypto model training must not depend on optional sports history. This changes the monthly research job to BTC, ETH, SOL and XRP **5-minute** histories, the only time interval the current live-compatible ten-feature trainer can ingest. It retains 6 full calendar months and independent checksums, and reports historical candidates tested and approved, not just the 42 legacy evaluations. Sports backfill remains explicitly available with `--seasons`.

`all_requested_data_complete` remains strict when the owner explicitly requests both crypto and sports. `crypto_sources_complete` records the independent crypto status. Historical training still never promotes a model without passing all held-out and prospective gates. If a provider is inaccessible the run should fail accurately with exact incomplete coverage. Private Google Drive publication still requires its bot-owned configured credentials.

## DraftKings Predictions is not the same as DKeX

DraftKings announced DKeX on June 26, 2026. It also routes some Predictions contracts through other exchanges, so **DKeX daily reports are NOT proof of every DraftKings Predictions crypto contract** or of the executable app prices a customer would see. There is no implemented, verified official live DraftKings Predictions crypto feed in RivaL.

Official DKeX research pages: `https://railbirdexchange.com/daily-market-reports` and `https://railbirdexchange.com/daily-settlement-reports`. The public reports cover DKeX's markets and settlements, typically the prior trading day. They do not provide the live bid/ask and fee data needed to score trading edge.

`python -m parlay_bot.research.dkex_reports` is an **opt-in, local, read-only research data importer**. An operator must save a source CSV from the official report page, verify the exact bytes and SHA-256 and supply the actual published header names. It checks a bounded CSV, refuses duplicate or conflicting revisions, records provenance in isolated tables and returns only research status. The importer intentionally does not fetch hidden or authenticated endpoints, create a market identifier, infer crypto asset/timeframe from a symbol, or make trading calls.

Example: `python -m parlay_bot.research.dkex_reports --file public-report.csv --date 2026-10-28 --kind settlement --symbol-column 'Market' --settlement-column 'Settlement Price' --sha256 <original-CSV-SHA256>`

The example column names are illustrative **not verified DKeX CSV schema**. Do not publish an uninspected CSV or mark the source as independently authenticated on user-provided bytes alone. The importer reports `official_source_independently_verified=False` until an authorized original-file acquisition/check mechanism exists. Amount 0.5 is tracked as a half-value settlement only; it does not assert a void without official outcome rules.

## Next acceptance gates

1. Green Ruff, full test suite, PostgreSQL tests and exact-commit Discloud release, plus a separate *real* full six-month crypto research run.
2. Verify report covers each requested crypto asset, exactly 6 full months of 5-minute bars, no missing candles and no look-ahead. Observe model approval results without weakening acceptance requirements.
3. Obtain permitted DKeX official CSV data and verify the true schema and exact-bytes source chain; ingest genuine reports after explicit confirmation. Do not use synthetic test rows to make accuracy claims.
4. Determine which exchange routes the *specific* DraftKings Predictions crypto contracts available to the owner. Find authorized live contract quotes, settlement rules, fees and terminal outcomes; only then build an exact market-matched forward ledger and model.
5. Compare prospective Brier/calibration and realistic after-cost entry prices against independent market-implied baselines for Polymarket and DraftKings separately. Preserve WAIT and do not auto-place bets.

**Scope**: no user-facing new menu, no real orders, no authentication bypass, no claim that ten venues or 6 months automatically beat the market.