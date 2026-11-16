# One-time XMarket original-source research execution

This follows merged [#623](https://github.com/UglyGameFace/RivaL/pull/623)
without reviving weekly training or sending discarded historical candidates
through the model-import folder.

## Critical pre-selection correction

Training-only analysis must also select its optional cross-venue dispersion
inputs using **training-period availability only**. The initial #624 code
reused the production selector, which checks source availability across
train/validation/test when pinning venues. This does not leak outcome labels,
but it leaks later *source availability into research design*. An apparently
better train-only profile from that version is not valid grounds for selecting
a challenger.

The corrected research-only selector restricts complete market clocks and
venue coverage to the horizon-purged training cutoff. Among candidate
venue subsets, it requires the same inner training-fold floor, at least
100 distinct inner evaluation targets, and seven evaluation UTC days.
It never asks which of those venues survived the outer validation/test
period. Production XMarket fitting retains its original all-split source
selection and unchanged numerical gates.

**Evidence rule:** Existing #624 artifacts, if already produced, are
diagnostics only until a run at the corrected commit provides fresh,
exact-source train-only results. Do not call the first run's subset ranking a
validated model improvement. This correction intentionally does not
restart the one-shot workflow or re-open the old holdout.

## Execution contract

- Source: original SHA-verified candle manifests restored by the existing
  archive reader and existing validated Polymarket/Kalshi input bundles
  retrieved from private Drive. The new `read_verified_inputs_only`
  **never** runs native backfill, calls public price endpoints, publishes
  research/source files, advances a cursor, creates model artifacts or
  modifies the RivaL database.
- Scope: BTC, ETH, SOL, XRP; 5m, 15m, 60m. Original training partitions
  and source-identity gates remain. The research comparison uses only the
  chronologically purged outer training split; its last quarter is a
  separately purged inner research evaluation. Outer validation/test labels
  are not scored for candidate selection. Any absence is reported as
  blocked/incomplete and the workflow fails visibly.
- Outputs: bounded sanitized JSON `xmarket-train-only-ASSET-RUNID`
  artifacts showing only row counts, source-verification status, predefined
  ablation names, Brier comparisons and exact availability reasons. No
  source quotes, private Drive IDs, weights, keys or trade calls.
- Scheduling: one owner-authorized main transition from the verified
  `c158cd4a` checkpoint when the new GitHub workflow is merged.
  It has no cron. Later runs require explicit manual workflow dispatch.
  The existing archive model training workflow remains independently gated,
  and this run does not publish/import a passing fit.
- Resource budget: bounded GitHub Actions four-asset matrix, each at most
  180 minutes. No extra hosting or persistent collectors.

## Acceptance and next step

1. PR CI confirms unchanged full SQLite/PostgreSQL suite, read-only
   source access and no outer calibration/test scoring.
2. Merged-main CI and a genuine executed Actions run with real restored
   sources and a downloadable report for each asset.
3. Inspect source-status and time-split failures before model changes.
   If an inner ablation reduces Brier loss, pre-register its hypothesis
   and collect a *new*, untouched forward evaluation period rather than
   retroactively mining the May-Oct sealed holdout.
4. Independently validate the already imported five XMarket fit scopes,
   native quote coverage and evidence gates; no 5m challenger is deemed
   qualified by a training-only comparison.

A failed or skipped workflow does not prove bad model quality. A successful
research report does not prove improved accuracy, reliability or profit.
