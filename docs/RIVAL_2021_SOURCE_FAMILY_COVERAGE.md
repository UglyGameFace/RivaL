# 2021-to-present historical coverage: evidence contract

**Target**: all supported BTC/ETH/SOL/XRP source/contract/quote pairs and relevant
external market-context data from January 2021 through the most recent completed
UTC month, plus separately timestamped *live* observations in the current month.
This is an **acquisition and verification goal**, not a claim that every source,
derivative, contract, exchange, or API existed in 2021.

## Already operating; do not duplicate

- Keep the existing official Binance 2021+ file-first 5m ZIP pipeline and Drive
  SHA readback, six-resolution research intake, daily historical positioning,
  OKX official one-minute archive + paced 5m fallback and auxiliary trade/funding/L2
  import, Gate gzip/API gap repair and contract-multiplier checks, and the
  per-venue Discloud candle repair workers.
- Existing source-native training, prospective forecasts, venue books,
  outcome settlement, calibration, risk gating, and immutable records remain
  authoritative. Never reclassify an old downloaded candle as a live forecast.
- The historical source-family coverage ledger adds **only a read-only reporting
  layer**. Run with:
  \`python -m parlay_bot.research.source_family_coverage --root data/research-history --root data/binance-deep --out-dir data/native-reports\`
- \`source-family-coverage.jsonl.gz\` provides every month x source x asset x
  research family. \`source-family-summary.json\` gives counts, conflicts and
  invalid evidence. If archives were not mounted/restored into the runner, the
  status is \`not_verified_in_this_run\`, **not** "provider has no data".
- Local normalized file SHA and full UTC grid checks are distinct from original
  provider publication authenticity, current off-run private Drive SHA readback,
  historical listing/contract-multiplier verification, actual point-in-time
  first observation, and prospective improvement. The ledger never promotes
  a model or declares full 2021-to-present coverage merely from files.

## What "complete" requires for each data family

1. Identify provider-authenticated official source, actual contract/quote,
   asset listing period, licensing/region/rate and transfer costs. Classify
   months before genuine listing as \`provider_proven_not_listed\`, not filled.
2. Use official free archives before bounded REST, with immutable original
   bytes, source SHA / publisher checksum where available, schema, units,
   precise event time and source-available-at assumptions.
3. Preserve exact record continuity **appropriate to the data family**:
   candles require closed-month UTC grid; event-driven funding, trades,
   liquidations, L2 deltas, options snapshots and settlements have their
   own expected publication/capture coverage. An archive containing one
   funding row cannot certify a complete month.
4. Verify actual byte readback from private Drive independently of the
   uploading run, reconcile duplicates without deleting distinct revisions,
   and prove restored source-native model artifacts and feature/label contract.
5. Confirm live and historic feature parity without leakage; compare each
   added contextual feature with the existing ten-feature candle baseline
   using purged historical holdouts and **independent future** paired forecasts.
   No candidate promotion without existing sample, day, quote, calibration
   and after-cost gates.
6. Record true unavailable, paid-only, retention-expired and restricted
   sources explicitly and leave those requirements **incomplete**, rather
   than silently using another venue's prices or proxy odds.

## Current external limits to track

Hyperliquid's public candle endpoint retains only the most recent roughly
5,000 candles; its older official trade archives may incur requester-paid S3
fees. Historical exact exchange-specific candles require validated free
archives or reconstruction. Gate contracts need dated historical multiplier
proof. OKX Drive duplicates and official archive host/filename acceptance
remain distinct issues. DKeX/DraftKings Predictions needs authorized
live contract/orderbook/rules/settlement data; Polymarket and Kalshi require
exact venue-native contract/oracle/fee evidence. Macroeconomic consensus,
economic vintages, options volatility, institutional flows, on-chain data
and liquidation history must not be claimed by adding names to an inventory.

**This ledger is not completion of Issue #188 or #151.** Its job is to make
missing evidence visible without blocking existing recovery, or changing
the bot's predictions and free-only quota limits.
