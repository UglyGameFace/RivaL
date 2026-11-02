# RivaL mathematical validity and prospective accuracy

## Evidence boundary

RivaL applies live-compatible ten-feature calculations, chronological purged
historical train/validation/test partitions, per-issuance prediction/model hashes,
a source-exact forward-outcome ledger, and paired squared-error (Brier)
comparison. These are **implemented algorithms**, not a guarantee of trading
edge. Exact forward outcomes, independent executable quotes, costs and a
sufficient time horizon are required to establish predictive value.

## November 2, 2026: corrected multi-exchange score

The previous `ForwardAccuracyMonitor.active_learning_evidence` query discarded
the **entire UTC target** whenever different exchanges settled in opposite
directions. Real disagreements are not an invalid outcome and eliminating them
can bias the measured scores. It also gave a venue with multiple contracts
more weight than a single-contract venue.

This correction retains each venue's independently settled, immutable squared
errors, averages **within a venue and UTC target**, then averages **equally
across venues** for that target. Finally, the existing paired-score function
counts the target once even if many venues reported it. It does not average
prices or directions across different exchanges. Source-specific history,
settlement causality, model versions and training lineage filters remain intact.

### Independent numerical reference

For two venues with opposing outcomes on the same UTC target:

- Venue A: up outcome, model p(up)=0.9 => Brier 0.01
- Venue B: down outcome, model p(up)=0.8 => Brier 0.64
- Both use p(up)=0.5 baseline => Brier 0.25
- Equal-venue model Brier = (0.01 + 0.64) / 2 = **0.325**
- Baseline Brier = **0.25**, paired model gain = **-0.075**

A second identical contract at venue A must not change 0.325. Seven such
independent target times produce seven scored targets, not zero, even when
the venues disagree. CI's synthetic immutable-ledger regression checks this
exact calculation. Golden scoring tests independently check Brier, accuracy,
log loss and ten-bin calibration.

### Status and limitations

The `active_learning_evidence` result is **14-day adaptive shadow-research
screening**, not after-cost profitability, proof of 21-day production accuracy,
a quote-coverage certificate or approval to trade. The older research
`qualified_shadow` screen requires only 100 targets over seven days and is
**not** the owner's stronger production proof threshold of 150 independently
forward-settled targets, 21 days, at least 80% quote coverage and positive
after-cost performance. No model is automatically promoted by this correction.

Once main deploys, verify real PostgreSQL counts, target/venue disagreements,
source freshness, settled outcomes and matched quotes in production. Tests
confirm formula correctness on controlled inputs, not empirical predictive
accuracy for live crypto markets.
