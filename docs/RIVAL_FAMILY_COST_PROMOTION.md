# Source-exact, cost-gated live model families

RivaL already issues independent online-trained Gaussian Naive Bayes and
21-neighbor forecasts with source-matched future outcomes. This release adds
the missing **contemporaneous quote comparison** to the same five-minute
research and read-only signal path.

## Training, immutable forecasts, and quote accounting

- The existing live learner (logistic) remains the safe default. On the same
  venue, contract, quote and horizon, the NB and kNN challengers train only
  from matured live examples. Their training SHA and forecast probabilities
  are frozen at issuance. Six-month Binance history is NOT assumed compatible
  with a different venue's instrument or quote.
- When the existing verified quote watcher sees a fresh matching public
  best-book update no later than 90 seconds after forecast issuance, it
  persists hypothetical entry bid/ask for both challenger and incumbent,
  including cash/no-trade decisions. They share exactly the same entry and
  exit source book, never inferred OHLC, cross-venue prices or fills.
- A source-matched observed exit must arrive at the actual target close
  within 90 seconds. Missing entry, stale book or missing exit are part of
  the sample coverage denominator, not retroactively priced successes.
- The additional quote and promotion tables are additive. Failures in
  optional model research never roll back the incumbent forecast or original
  quote result. Models never place real orders or request trading credentials.

## Automatic evaluation and guarded promotion

A bounded background governance cycle assesses an exact asset, venue,
contract, quote, horizon and model family against its contemporaneous
incumbent, past-rate baseline, and coin flip. It requires all of:

- At least **150 unique time-aligned future targets over 21 UTC days**;
  Brier wins over all three reference forecasts and independent day-block
  stress tests; calibrated predicted probabilities (ECE <= 10%).
- At least **150 matched closed quote windows**, at least 50 challenger
  paper trades, at least **80% quote coverage**, and no corrupt or ambiguous
  quote evidence. No retrospective forecast or historic candle is eligible.
- Positive conservative **and severe hypothetical after-cost paper net**
  performance and paired improvement over the incumbent, with positive day
  results and conservative day-block lower bounds.
- A fresh (at most 48 hours old) same-source governance decision.

Only then is the selected family reflected in the READ-ONLY RivaL watch
path with its own model identity and proof. Stale, losing, missing or
inconsistent evidence reverts to the existing live incumbent. Model families
do not inherit the old logistic model's performance record. Automated
promotion records are research/paper and do NOT authorize real trades.

## Cost interpretation and remaining constraints

The house conservative/severe assumed extra-cost scenarios are stress
assumptions, **not independently confirmed actual exchange fees, funding,
slippage or fills**. Even a qualified paper candidate is not proof of
execution profitability. Source/matching quote and realized book move are
verified in the observational ledger, not guaranteed fill execution.
Harder funding/contract-specific payout validation and venue-specific
historical tree artifacts remain open in Issues #151, #138 and #6.
A missing live book fails closed. Existing future outcomes cannot be
fabricated just to fill a promotion quota.

## Rollback and verification

Production state is additive and original forecasts/models/quote tables
remain immutable. A code revert restores original live model selection and
keeps existing audit tables for accountability. Unit and isolated PostgreSQL
tests cover no-quote abstention, source mismatch, sample coverage, calibration,
severe after-cost qualification, current family watch proof, stale fallback
and demotion. Merge only after exact-head tests and main/deployment proof;
host acceptance does not prove live model superiority.
