# RivaL: one-PR prediction-evidence acceptance and follow-through

This is the **implementation and verification contract** for PR #280, not a
claim that live predictions already beat the market. Research and paper
qualification do not authorize bets, wallet access or real-order execution.

## 1. Actual model improvement

- Maintain a source/contract/quote/horizon-specific incumbent logistic model
  and challengers (Gaussian NB, kNN, screened portable boosted families).
- Use only labels known **before issuance** for live training. Verify original
  source SHA256, complete UTC history and chronological/purged validation
  for historical candidates. Never synthesize candles or mix exchange feeds.
- A candidate failing retrospective validation stays archived as research,
  not in a live import path. Compare frozen, pre-issued probabilities against
  the same genuinely future target and a simple baseline.
- An improvement on already-viewed history is **not** fresh evidence.

## 2. Independent future outcomes and quote coverage

- Track each `(venue, contract, quote, horizon, model family, target)` once.
  If several forecasts target one outcome, choose the earliest issued record
  **before** examining quote availability or the result; report ignored
  overlapping records explicitly.
- Require >=150 distinct targets across >=21 UTC calendar days, same-source
  incumbent and baseline scoring, daily-block gain, and calibration screening.
- Track eligible decision windows separately from genuine matched entry/exit
  quotes, at least 80% coverage, and >=50 candidate paper trades.
  Missed entry or exit is missing evidence, never a synthetic favorable fill.
- Report observed, missing and duplicate counts and gate reasons. A low
  coverage count is not an indication of profitable or unprofitable strategy:
  it means **NOT PROVEN**.

## 3. After-cost strategy proof

- Use pre-issuance paper decisions and genuine executable-time bid/ask snapshots.
  Track LONG, SHORT and CASH consistently for challenger and incumbent.
- Apply both conservative and severe fees, extra slippage and funding stresses.
  Require positive candidate-net and paired-gain daily-block lower margins,
  >=80% positive days, and the same 150/21/80 coverage thresholds.
- Qualification selects only **practice/paper** research, with expiry and
  automatic demotion on a failing recheck. Never claim authenticated
  real-money fills or start live execution from a paper success.

## 4. Private LFM3 Intelligence hypothesis experiments

- The only AI provider is the separate, token-authenticated local LFM3 service.
  No Gemini, OpenRouter or Cloudflare API fallback, daily provider budget,
  or model-generated trade probabilities.
- The LFM proposes bounded predetermined feature-family ablations, tied to
  immutable source digests. Apply deterministic, training/validation-only
  tests against the ordinary logistic baseline for BTC/ETH/SOL/XRP.
- Keep sealed holdout, incumbent weights, model imports and order execution
  untouched. No improvement is claimed until new independent future
  outcomes, including after-cost quote comparisons, prove it.

## 5. Tests and required real-world signoff

- Run Ruff and all Pytest checks, including PostgreSQL-backed storage,
  overlapping 15m target-window regression, forward timestamp/source identity
  checks, missing/invalid quote paths, cost stress and fail-closed model gating.
- Verify PR CI then merged main CI, main Discloud deployment, separate
  Intelligence release and authenticated hypothesis smoke.
- Verify source-native archive manifest/readback SHA256 and first persisted
  post-issuance hypothesis plus actual comparative validation receipt.
- **Cannot be completed by merging:** 150 new independent future results,
  21 elapsed days, 80% verified quotes, a profitable after-cost lower bound,
  or authenticated trades. Keep these recorded as open until evidence arrives.

Last observed report: 200 eligible *joined comparison records*, 18 matched
quotes, 0 authenticated trades, with example Gaussian NB and kNN XRP Brier
scores worse than the incumbent. These counts cannot establish edge.
