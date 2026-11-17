# Source-authentic XMarket training and guarded offline candidates

The existing private Drive native-source writer already runs four times a
day. Only the 20:43 UTC slot now proceeds to model research after all four
assets complete. Push events provide dry-run acceptance. A manual source run
may opt into research and independently opt into publication. This creates
no extra provider collection loop, rate budget or concurrent Drive writer.

The workflow freezes one source date before collection, shared across all
assets and the downstream model job even if a runner crosses midnight.
It restores provider-SHA-verified market candles using a pinned original OKX
V5 source family, plus independent verified Polymarket and Kalshi originals.
No unverified source is synthesized, backfilled as a prospective outcome,
or used in fitting. Raw price and source IDs are never uploaded to GitHub.

For every asset and 5m/15m/60m horizon, the **existing** purged inner
research experiment must contain seven unique UTC evaluation days,
100 independent evaluation timestamps and 300 fitting rows. Incomplete
scopes are reported as awaiting genuine history; the outer validation/test
holdouts are not opened. Once the inner gate passes, the existing
production fit and validation gates run without altered thresholds.
Only passing, validated candidates are written temporarily and
published idempotently into the existing PRIVATE Drive model path.
On-host candidate imports, independent prospective scoring and
promotion/demotion gates remain separate. No real-money orders.

Artifacts contain ONLY whitelisted source verification, dates, fold counts,
offline gate verdicts and publication booleans. They omit models, weights,
private Drive IDs, prices, and credentials. Real exceptions exit nonzero
with only their class recorded. Missing verified source or insufficient
inner history is a visible pending state, never a fake pass.

## Baseline and required production proof

On 2026-11-17, the authenticated four-asset train-only run
https://github.com/UglyGameFace/RivaL/actions/runs/43996911249
measured **3 of 7** required inner days in all 12 scopes. It was
correctly blocked. Five scopes presently have older imported models,
and zero have an after-cost trading edge. The newest actual native
Discloud release https://github.com/UglyGameFace/RivaL/actions/runs/43998168038
passed source parity and PostgreSQL/Discord Performance rendering, but
that does not prove model profit.

Acceptance for THIS PR requires exact-PR and merged-main CI, an
actual successful authenticated four-asset source run, and the
downstream four-asset source-to-model receipts. While source history
remains at 3/7 days, the new job must issue zero candidates and perform
zero sealed holdout evaluations. On later eligible days, passing
offline model artifacts may be published only through the existing
independent private import and prospective governance gates.

Forward proof still requires at least 150 independent prospective
outcomes spanning 21 UTC days, at least 80% source-matched executable
quote coverage, baseline improvement and positive realistic stressed
after-cost results. Missing future evidence cannot be accelerated by
renaming historical candles. Evidence Board PR #638 and PostgreSQL
deadlock #347 are intentionally separate work.
