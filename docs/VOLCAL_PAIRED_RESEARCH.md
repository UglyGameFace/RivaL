# VolCal: paired ZIP-derived feature evidence

**Status:** Offline research diagnostic only. No new model, changed probability, automatic promotion, trade, or forward-performance claim.

## Why this exists

RivaL already reimplemented the useful volatility-regime, compression, volume-surprise and confirmed directional-change (DC) feature groups from the supplied `perp-engine` study using RivaL's own source candles and training. The scheduled research grid included ablation variants scattered across **different assets** (for example no-vol ETH and no-compression SOL). Their raw score differences cannot establish incremental value because market difficulty and observations differ.

In the normal weekly archive job, every BTC/ETH/SOL/XRP × 5/15/60-minute VolCal run now evaluates **one additional, preregistered group-off refit for the same asset and horizon**, on the exact same source frames, label targets, purged chronological train/validation/test masks and train-only baseline as its full model. The group is chosen by a fixed UTC-week rotation, offset by asset, independent of model scores. Every asset/horizon covers all four groups over four weekly runs when those runs actually finish.

This is **not four new models per horizon**. Only the usual full VolCal artifact remains eligible for Drive publication, and its existing fitting, calibration, gates and validation are unchanged. An ablation's fitted weights and model artifact are intentionally discarded. Failed research is reported as unavailable without changing the baseline model result. One extra logistic refit per asset/horizon is bounded; re-running the expensive optional volatility-blend diagnostic on the ablation is skipped.

## Evidence and interpretation

Each weekly VolCal *report only* includes `paired_feature_ablation_research` with the group, frozen source-clock identity, held-out validation/test rows and days, both full/ablated Brier gains against their identical training-only up-rate baseline, and the difference:

`test_gain_from_including_group = full test Brier gain - ablated test Brier gain`

A **positive** difference suggests that including the group helped this particular held-out sample; a **negative** difference suggests the ablated fit was better. Neither is a credible perpetual winner by itself. Results are descriptive and expressly not multiple-comparison adjusted, not prospective, and not after execution costs. They do not affect model selection, Route B gates, venue fills, Discord calls, alerts, or promoted weights.

The code checks same asset/horizon, feature contract, train cutoff, test start and exact validation/test row and day counts before reporting a paired difference. Both fits use `volcal_training.train_run` on identical in-memory frames; masking occurs only *after* the shared full-feature eligibility filter. These invariants rule out asset-mixing and changed sample populations. They are not substitutes for later source-paired prospective significance, stress testing or economic execution receipts.

## Operational acceptance

Exact-head Ruff and full PostgreSQL CI, merged-main CI, actual Discloud deployment, and the first weekly archive report containing this field are separate milestones. A synthetic test checks behavior but is not actual market evidence. Keep the prespecified rotation until all four asset/horizon groups have real reports; retain rejected experiments rather than hiding them.

**Project order:** The XMarket completed-day/Kalshi fixes in merged PRs #549 and #550 remain first; their independent prospective source data and model fit are not yet proven. This diagnostic is the second stage (ZIP-derived features). After source-matched prospective shadow tests and after-cost gates comes repair of the signed Evidence Board publication.
