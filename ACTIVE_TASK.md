# RivaL active bot work — November 15, 2026

## Objective and scope
Restore timely prospective close-target settlement while preserving bounded recovery of older source paths. Carry the full eleven-area [remaining backlog](docs/RIVAL_REMAINING_BACKLOG.md); website work stays paused. Preserve source clocks, immutable forecasts, model/after-cost qualification and zero live orders. Independent Jarvis/LFM3 PR #608 is outside this change.

## Evidence and reproduced cause
- Baseline `main@41ca36ec759e56c6dd11962607f6282551b2559a` includes #606/#607/#609. Main CI and native Discloud source-file parity passed.
- An actually executed authenticated [host receipt 43758200220, attempt 2](https://github.com/UglyGameFace/RivaL/actions/runs/43758200220/attempts/2) at 23:12 UTC still shows 135,856 overdue parent side records and zero recent close-target settlements; its bounded source sample has four ready paths and four missing-source paths. Heartbeats are fresh; no recent path SQL error or deadlock was observed. These are not independent targets or proof of profits.
- The worker always selects the oldest 100 ready source paths. Barrier forecasts existed before close-only companions were introduced on November 12. A regression on unchanged main reproduces all eight side records of a bounded cycle settling older parents while all six current companion heads remain pending. This establishes the scheduling defect; exact production cohort composition remains an observation, not an inferred full census.

## Implementation
- Branch `fix/path-current-and-backlog-settlement-20261115`. Reuse the one authoritative path ledger and worker; no new scheduler or migration.
- Read two disjoint, bounded age queues with the same source/count/first-observation requirements. Share the existing maximum 100 source-path / 200 side-record write budget: at least 80 slots for newest ready targets in the last two hours and 20 for oldest older paths when both queues are full; either queue borrows unused capacity. Preserve both sides together and the full Python causal/price/ambiguity checks.
- Expose resolved parent side records over 24h separately from recent close-target results, using the existing read-only lifecycle aggregate. Old-parent progress must not be mistaken for current-head scoring or independent evidence.
- Regress sustained issuance over six worker restarts, full 40-stream/two-horizon live capacity with a larger older ready backlog, borrowing capacity in either direction, missing/future/wrong-source evidence, original forecast immutability and absent legacy companion heads. Preserve the previous task record byte-for-byte in docs/archive/ACTIVE_TASK_QUOTE_EXPLANATIONS_20261115.md.

## Validation and production acceptance
- The new mixed legacy/current regression failed on unchanged main with six current heads still pending. Focused SQLite/integration checks and repository-wide Ruff pass; PostgreSQL coverage must pass on the exact PR head in repository CI. Full-suite results and any environment-only limitations are recorded with the PR.
- Before merge, inspect current PR overlap and require exact-head full PostgreSQL CI. Then verify merged-main CI, executed native source parity and a new non-skipped authenticated receipt after the normal worker cycle. Require actual recent close-head settlements plus ongoing older-parent progress; a green workflow alone is insufficient.
- Keep #151, #6, #299, #138 and #347 open for their remaining target calibration, market coverage, forward quality, after-cost proof and reliability requirements. No claim of 100% completion or qualified edge. If production remains at zero, inspect selected source cohorts and actual cycle logs/timing before choosing another repair.
