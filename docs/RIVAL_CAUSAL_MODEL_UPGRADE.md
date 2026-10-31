# Causal model-upgrade milestone (October 30, 2026)

This work extends the existing RivaL architecture. It does **not** introduce
another Discord menu or automatic trading, and does not mark rejected models
as approved.

## Six-month exchange history

`research/live_training.py` now compares a prespecified small family of
full-feature, slower, faster and trend-focused eleven-weight logistic models.
All members use the exact ten features and target contract of the persistent
live mathematical engine. The first chronological segment alone supplies
training labels; a purged validation segment chooses one method; the untouched
last segment is used once for final evaluation. Existing two-stage Brier and
calibration gates are unchanged. Historical files or the old 42-model archive
are **not** live model weights merely because they exist in Drive.

A candidate becomes eligible only with source and feature compatibility,
six-month coverage, strict retrospective performance, then independent forward
challenger evidence. It does not automatically replace the incumbent on a
retrospective win. Existing model checkpoints, history lineage and rollbacks
remain authoritative.

## Source freshness and ten-venue forecast

The existing `MultiExchangeResearch` gathers source-specific, genuinely
prospective predictions, filters invalid or blocked feeds, requires at least
three independent venues, caps a single source at 40% and freezes a single
fused forecast per target. This change additionally discounts sources whose
forecast timestamp, last successful poll or latest completed candle is older.
The actual per-source quality and weight are retained in immutable contributor
evidence. A response from ten providers is **not** ten independent successful
bets, and it does not guarantee more accuracy.

## Polymarket price-to-beat / oracle context

`PolymarketOracleChallenger` is a new isolated **experimental v2** model
with a versioned feature contract and separate state. It includes:
- Exact current/next condition, token, settlement-source and rule identity
- Fresh UP/DOWN book and ten-share depth
- Verified published price-to-beat and live price from **that exact resolution
  source**, never a random futures close
- Time remaining and same-horizon measured volatility
- A normalized signed distance to the strike and a volatility-adjusted distance

This model only records an original observation when an exact-source
`OracleReference` with an immutable original receipt digest, matching
asset/source/method and tightly bounded source timestamps is provided. Short
markets require Chainlink 60-second TWAP rules. Hourly contracts must use their
published Binance spot hourly source; no cross-horizon label substitution.
Training uses only separately verified matching binary terminal receipts,
and weights are persisted per asset/horizon/resolution source. The first
prospective score per contract is frozen. Its Brier score is compared with
the saved market-price baseline; all scores remain research-only.

**Important production blocker:** RivaL's existing Gamma/CLOB reader provides
the published threshold and contract books, but does NOT currently supply an
authenticated live Chainlink TWAP/settlement-matched spot feed or a
source-synchronized horizon-volatility measurement. Accordingly the
`oracle_reference` field stays None in the actual reader; v2 safely abstains
rather than substituting a Bitget/Binance perpetual price. This change builds
the model and source contract but does **not** claim live v2 readiness or an
improved Polymarket win rate. Provider licensing, authentication, live receipt
verification and real oracle-time matched acquisition must be completed before
v2 can receive production observations.

The prior book-only model, real contract AI and immutable terminal outcomes
remain independent. No historical v1 samples are force-converted into v2.

## Automatic challenger governance

The existing runtime `evaluate_promotion` periodically reviews independent
prospective samples, paired losses, 21-day quote/after-cost stress, calibration,
freshness and rollback conditions. This change excludes quote shadows for
unsettled predictions from the short-window trade proof. Do not reduce the
minimum source, 150 windows/21 days, 80% quote coverage, or other thresholds
just to get a higher call volume.

## Hosted acceptance

1. Main CI, PostgreSQL 16 and Discloud release on the exact merge commit.
2. Verify the new historical training produces reproducible candidate SHA,
   real chronological holdouts, no leakage and zero admissions if unsuccessful.
3. On actual collected independent source forecasts, verify the frozen
   contributor JSON includes source quality and per-venue weights; check
   reliability through restarts and live unavailability.
4. Supply and verify official source-matched TWAP/spot oracle receipts and
   synchronized volatility inputs before claiming oracle-v2 live training.
5. Require genuine future Polymarket terminal outcomes, calibration, model
   Brier improvement over the book-implied baseline, and positive after-cost
   executable quote evidence before model promotion.
6. Preserve WAIT for unqualified signals, keep DM notifications quiet,
   and do not interpret AI explanations or model confidence as proven edges.

No orders, new paid API, or new Discord permissions are introduced.
