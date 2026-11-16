# XMarket rejected-fit research: train-only feature ablation

## Source-backed problem

The November 14 original four-asset archive training run
[43647037069](https://github.com/UglyGameFace/RivaL/actions/runs/43647037069)
reported five passing XMarket fits (BTC 15/60m, SOL 15m, XRP 15/60m) and
seven rejected fits. The actual `training_outcomes` JSON in the four
training-job logs records the seven rejected scopes:

| Scope | Rejection codes |
| --- | --- |
| BTC 5m | validation and test Brier gain <= 0.005 |
| ETH 5m | validation and test Brier gain, daily fraction < 0.55, test ECE > 0.03 |
| SOL 5m | test Brier gain, test ECE > 0.03 |
| XRP 5m | validation and test Brier gain, daily fraction, ECE, calibration gate |
| ETH 15m | validation and test Brier gain, daily fraction |
| ETH 60m | test ECE > 0.03 |
| SOL 60m | test ECE > 0.03 |

These are **offline model-quality rejections**, not proof that seven collectors
are broken. The 5m code path exists. The five passing fits remain
*research-only* until independent prospective after-cost evidence qualifies
them.

## What the implementation does

On a **rejected but sufficiently trained** `xmarket_v1` run, the existing
original-source trainer appends a `train_only_feature_ablation` record to its
ordinary Drive research report, not the model-import artifact folder.

The same complete, point-in-time XMarket features and labels from that
run's **training** partition are sorted by UTC target bar. The last quarter
of unique timestamps forms an inner evaluation fold; the inner fit stops
strictly more than one model horizon before that fold. Require at least
300 fitted rows, 100 distinct evaluated timestamps and seven evaluated UTC
days. Otherwise record `insufficient_train_only_fold` without fitting.

Four deterministic, predeclared logistic comparisons use the already
authoritative fitting/standardizing/scoring functions:

1. Full original XMarket feature contract.
2. Exclude three cross-venue dispersion features.
3. Exclude the two lagged Polymarket changes.
4. Keep candle features and native market levels/gap, excluding both groups.

Within each UTC target timestamp, average per-venue squared errors before
averaging targets. Preserve raw score, target counts, dates, and
Brier delta against the full-feature profile. Even distinct adjacent
timestamps can have correlated horizons: **these are exploratory,
uncorrected scores and not independent forward-edge proof**.

The code never uses the outer validation or locked test data for this
research, chooses a serving candidate, modifies the original model
coefficients, changes the calibration thresholds, imports an artifact,
replays old forecasts, or trades. A better inner score may guide a
**pre-registered next-period challenger**, but is not a permission to retune
against May–October holdout results previously examined.

## Acceptance and next work

- Regression coverage for horizon-purged fit/evaluation, required sample
  floor, real deterministic logistic scores, correlated venue-row accounting,
  invalid labels/features and no selected model artifact.
- Exact PR-head PostgreSQL/SQLite CI, merged-main CI, and original-source
  artifact publication must pass before claiming operational integration.
- On the **next verified, non-skipped original-source training run**, inspect
  all seven rejected reports for their train-only ablation evidence.
- Decide candidate architecture using only train-only results and a newly
  accumulated, locked *future* evaluation period. Keep unchanged 0.005 Brier,
  0.55 daily fraction, 0.03 ECE, calibration and source-specific live
  150-target/21-day/80%-coverage/conservative/severe after-cost gates.
- Continue collecting and evaluating the five currently imported models
  independently; an offline experiment must never disrupt issuance.

This change advances the research pipeline, **not model quality
verification**. Whether any of the seven rejected scopes can be improved
remains an empirical question.
