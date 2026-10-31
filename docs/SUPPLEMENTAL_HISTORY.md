# Supplemental six-month research sources (additive)

This extends RivaL's *existing* historical research intake, quality manifests and
private Google Drive staging/verified readback. It does **not** replace the base
Binance OHLCV backfill, source repair, Discord collector, model checkpoints,
sports workers, Polymarket tracker or Kalshi learner.

## Implemented source readers

| Category | Research source / availability | Actual meaning |
| --- | --- | --- |
| Historical funding | Official Binance USD-M monthly fundingRate ZIP and matching CHECKSUM | Past funding rates, not verified actual account payments or execution proof |
| Open interest and positioning | Official Binance USD-M daily metrics ZIPs: open interest, top-trader ratios, long-short ratios and taker-volume ratios | 5m historical measurements with explicit pre/post June 25, 2026 timestamp labeling; source capture/publication time not independently authenticated |
| Exchange reference prices | Official Binance USD-M markPriceKlines and indexPriceKlines, 5m | Venue-specific mark/index histories, **not** the precise official Polymarket/Kalshi oracle |
| Futures premium | Official Binance USD-M premiumIndexKlines, 5m, including negative premiums | Exchange premium history, no inferred tradable market depth |
| Higher-frequency BTC/ETH/SOL | The **existing** `free_history binance --interval 1m` and normal base 5m imports | Historical OHLCV granularity; within-candle TP-vs-stop ordering can remain unknown |
| Polymarket contract token prices | Public CLOB `/prices-history` by validated exact outcome token ID and time range | Historical token price samples; no historic bid/ask book, fill proof or settlement receipt |
| Kalshi exact-contract history | Public archived `/historical/markets/{ticker}/candlesticks` | Historical OHLC YES bid/ask/price when provider has it, with volume/open interest when present; no authenticated settlement label or fill proof |
| Macro calendars, options volatility, on-chain events, six-month book depth, historical cross-venue OI | **Not imported yet** | No unlicensed data guesses or misleading six-month completeness claims |

Every imported partition is recorded as `offline_historical_research_only`
with its actual source URL, source SHA, normalized content SHA, event-time
semantics, contract identity, gaps and a required `prospective_forecast=false`.
Binance ZIP bytes must match the official SHA256 checksum, and malformed
timing or prices cause rejection. Provider history without a cryptographic
checksum is hashed on intake but is **not** claimed provider-signed. The
existing `prepare`/`publish` path checks normalized content bytes and
restores the published private Drive copy before confirming publication.

The old six-month candidate-training feature contract and the live model remain
unchanged. Supplementary data is stored as separately versioned **research
features not yet admitted to live modeling**. A later source-matched, strictly
point-in-time, holdout-gated experiment is required before any new input is used
for forecasts. This intentionally avoids accidental retroactive confidence
inflation or changing approved model weights.

## Running the verified intake

These commands can be run from the checked-out RivaL repository:

```bash
python -m parlay_bot.research.supplemental_history --months 6 \
  --assets BTC ETH SOL --max-new 100 --plan

python -m parlay_bot.research.supplemental_history --months 6 \
  --assets BTC ETH SOL --max-new 100 \
  --root data/research-history --report-dir data/research-reports
```

The separate `.github/workflows/verified-daily-metrics.yml` runs on main
updates and daily. It backfills up to 215 past days of BTC/ETH/SOL/XRP
Binance positioning data using four independent asset-specific GitHub Actions
matrix jobs (at most three active simultaneously). Each asset has a separate
digest-checked Actions cache, a bounded 215-partition intake budget and unique
CI artifact name, so a successful source can finish its six-month catch-up
within **one workflow run** instead of requiring nine daily runs. Failed,
unavailable or interrupted partitions retain verified checkpoints and resume
on later runs. This is bounded parallel ingestion, not removal of provenance,
checksum, event-time or model-admission safeguards. Successful completion time
still depends on provider availability, GitHub queueing and authenticated
Drive readback.

The same authorized Drive publisher and verified readback are used when available. The cache is an optimization, not provider
integrity evidence. The upload journal saves receipt IDs only **after**
the publisher has fetched and verified the private Drive bytes.

```bash
python -m parlay_bot.research.supplemental_metrics --days 215 \
  --assets BTC ETH SOL XRP --max-new 100 --plan
python -m parlay_bot.research.supplemental_metrics --days 215 \
  --assets BTC ETH SOL XRP --max-new 100
```

**Timestamp caution:** official Binance public-data issue
[binance-public-data #491](https://github.com/binance/binance-public-data/issues/491)
reports that on 2026-06-25 the USD-M `metrics` daily archive changed
`create_time` labels from end-of-window to start-of-window, creating an
ambiguous duplicated 00:00 boundary in adjacent days. Each normalized record
keeps `source_date`, `source_labeled_at`, `label_regime`,
`boundary_caution`, and a conservatively delayed
`conservative_feature_after_at`. None of these are real-time observation
timestamps, and **no live forecasting model is authorized to use them without
separately validating publication lag and point-in-time availability.**

The standard monthly GitHub historical workflow now runs the bounded public
supplementary acquisition alongside the existing base pipeline. Publishing to
the **already configured RivaL Data Warehouse** uses `--publish` only when
its authorized private Drive folder IDs and OAuth credentials are available.
Without them, an explicitly incomplete status remains in CI artifacts; a
successful CI test is **not** proof of uploaded historical records. The workflow
runs only when its normal scheduled, main-push, or explicitly dispatched
research job executes. No Discloud background worker or new API request on a
Discord command is introduced.

Polymarket/Kalshi history needs **verified, exact contract identifiers**, not
a keyword like "Bitcoin UP". An optional input file supplies these requests:

```json
[
  {
    "venue": "polymarket",
    "condition_id": "0x<exact 64 hex digits>",
    "token_id": "<exact decimal CLOB outcome token ID>",
    "start_ts": 1782864000,
    "end_ts": 1782950400,
    "fidelity": 5
  },
  {
    "venue": "kalshi",
    "ticker": "<official exact market ticker>",
    "start_ts": 1782864000,
    "end_ts": 1782950400,
    "period_minutes": 60
  }
]
```

Use `--contract-manifest path/to/exact-contracts.json`. The placeholders above
are **not real market identifiers**; replace them with actual matched
condition/ticker/token evidence obtained through RivaL's existing public
market readers. Do not construct an outcome token from a condition ID. Each
window must be at most 200 days and contain actual historical provider
observations. A Kalshi market still in the live data tier must use the live
candlestick API; the archived-only endpoint deliberately returns an explicit
unavailable status rather than silently substituting an unrelated market.

## Provenance, gaps and stop conditions

`supplemental-<sha>.json` reports requested, downloaded, rejected, missing,
verified records and a **missing-capabilities matrix**. No call is issued based
on supplemental data alone. In particular, six months of executable spread and
order-book depth, actual account fees/slippage/fills, verified
Polymarket/Kalshi settlement proofs and continuous historical open interest
are not conjured from price candles. A missed hit ordering inside a five-minute
bar is ambiguous unless finer verified path evidence resolves it.

External-provider access, rate limiting and private Drive OAuth availability
must be proven on the actual workflow run. Source outages do not stop the
original six-month OHLCV research; the supplemental job exposes its own failure
status, and later runs resume only partitions with verified matching digests.

## Completion and later integration

The repository's broader Issue #151 remains **OPEN**. To call this feature
complete, RivaL still needs bulk discovery of exactly matched historic
prediction contracts, reliable 2026 October daily catch-up, point-in-time
liquidity and options/on-chain/macro dataset provenance, old-to-live feature
admission, independently calibrated contract-price path models, prospective
first-hit accounting, and venue-specific after-cost governance.

No extra model or user-facing alert system is introduced by this intake.

## First-passage model research built on the existing verified archive

The standard six-month `research.production` pipeline now also evaluates a
**separate** `rival-path-research-v1` offline candidate family on qualified
five-minute Binance futures bars. It reconstructs long and short hypothetical
setups with fixed volatility-normalized TP1 (1σ), TP2 (2σ), and stop (1σ) for
15- and 60-minute horizons. These are *not* venue-native contract payouts.

Every outcome uses only future high/low bars *after* its reference close.
A five-minute bar hitting an unresolved profit barrier and stop simultaneously
has no provable hit order and is **excluded**. Future windows do not overlap;
gaps are rejected. Momentum cohorts, baselines and smoothing are fitted only on
chronological training history. The validation period and purged final test
are scored separately with Brier, calibration and minimum-day gates; research
candidate admission also requires six verified source months. Exclusion counts
and original source digests remain in the production research report and
verified Drive report when publishing is authorized.

This is a measurable step toward Issue #151, not a new competing bot/model or a
backdoor into its prediction menus. Neither an impressive backtest nor a
passed offline candidate proves actual fills, real after-fee profits, live
issuance-time prices, Polymarket/Kalshi settlement outcomes, or true
prospective accuracy. The current live models, alert behavior and forward
ledgers are left unchanged. A separately proven market-specific forward
path/price model and live governance are still needed before user-facing
entry/TP/stop recommendations may use these results.
