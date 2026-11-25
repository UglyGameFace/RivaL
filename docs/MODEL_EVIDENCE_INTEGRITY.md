# Model evidence integrity correction — 2026-11-25

This change repairs measurement. It does not establish a profitable model,
resume stopped trials, change trained weights, or authorize a promotion.

## Signed measurements survive publication

The live-grading collector formerly reused the operations-metric sanitizer:
it rejected negative numbers and rounded nonnegative numbers to two decimals.
Consequently, a Brier improvement of -0.00283 became unknown, an equally small
degradation became zero, and 50.34% accuracy became 50%.

Scores, confidence bounds, thresholds and net outcomes now retain their sign
and six decimal places. Probabilities remain restricted to [0, 1], counts
remain nonnegative integers, and invalid/missing values remain unknown. The
authenticated receiver, persistent receipt and browser rendering are covered
by the existing integration tests. Previously sanitized receipts cannot be
repaired from their stored values; only a new source read restores precision.

## G-D counts target clocks, not instruments

`gd-target-clock-v2-20261125` corrects the G-D measurement unit to
`asset_target_clock`: the coin plus the recorded target candle opening in
UTC. Venue, contract, quote and horizon do not multiply the cluster count.
Different clocks from the same instrument add evidence over time. Missing,
invalid, naive or already-expired target clocks are excluded, never inferred
from instrument identities or reconstructed prices.

The 500-row, 7-scored-day, 100-call, 40-cluster floors, seven-day burn-in,
chronological fitting/embargo, multiple-comparison adjustment and verdict
legs are retained. Target-clock counts alone do not prove independent
overlapping returns: confidence intervals still use whole UTC-day blocks.
Every new G-D report/result carries its grading version and cluster unit;
the results ledger appends a new timestamped row and retains old records.
No prior KILL or frozen test verdict is recomputed by this patch.

G-D's existing baseline-implied unit-payout diagnostic contains no executable
perpetual order book or recorded venue fees. Its screen PASS can license only
a research proposal. It is not an after-cost trading result or a promotion.

## Distinct baseline contracts

The live exchange model's `baseline_up` is the smoothed historical UP frequency
frozen at issuance. It is not a market-implied probability or an executable
price. The live grade publishes that identity and reports raw venue/horizon
rows separately from coin/target clocks, including per-horizon counts.

Polymarket book screens compare against the stored issuance-time book-implied
probability. Contract profitability still requires actual executable quotes,
settlement terms and costs. Their economic proof cannot be transferred from
the exchange base-rate comparison.

The live exchange grade now streams bounded chunks and retains only sums,
counts and target sets. It preserves the prior per-row point estimates and
equally weighted day-mean intervals, avoiding a full-week row/score copy on
the bot while its memory plateau remains under observation.

A deterministic synthetic 183,244-row comparison against the base commit
returned identical rounded 24h/7d/per-horizon metrics. `tracemalloc` peak
allocation was 123.49 MiB before and 6.89 MiB after (10.60s versus 11.06s
under tracing). These are local Python allocation measurements, not live
process RSS, PostgreSQL query plans or production recovery evidence.

## Next research decision

Candidate expansion stays paused while measurement is verified and current
registered tests reach their frozen verdicts. Existing collectors and
registered tests continue; trial issuance disabled by #704 stays disabled.

The next permitted proposal should target **Polymarket BTC 15m**: one exact
market contract and fee/settlement definition, compared with its own frozen
book price. This scope is chosen for operational focus, not because it won
a retrospective search. Change one receipt-backed input family at a time,
with its definition, minimum detectable effect, independence/overlap policy,
evaluation dates and cost assumptions frozen before collecting test outcomes.

Replays can reject weak ideas. Any replay PASS must be tested on untouched
future events under the standing >=150 independent forward-target, >=21-day,
>=80%-matched-quote and positive robust after-cost gates. Do not re-label a
pooled test failure as a BTC subset success, tune repeatedly on the same
holdout, soften a frozen test, or restart a killed family under a new name.
