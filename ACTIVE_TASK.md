# RivaL active bot work — November 15, 2026

## Objective
Progress the [complete backlog](docs/RIVAL_REMAINING_BACKLOG.md), keeping website work paused. Current cohesive outcome: unblock source-exact paper path settlement and distinguish missing issuance from missing outcomes. No model qualification or profitable prediction skill is inferred from engineering checks.

## Baseline and findings
- Inspected `main@dc692ef7994991cba53f85b24a24aa8fc7c5b0e7`, live branches, all open PRs (none), callers, source candle persistence, path ledgers and existing regression tests before editing. No overlap found.
- #605 is merged. Authenticated host [receipt 43748283578](https://github.com/UglyGameFace/RivaL/actions/runs/43748283578) at 18:42 UTC reports five issuing XMarket scopes, 2,524 immutable shadow rows over 24h, zero qualified paper scopes, and **zero settled close-only path targets**. Its old target receipt cannot distinguish absent issuance from settlement starvation.
- Reproduced unchanged-main defect: 102 older pending paths with missing source candles occupy the oldest LIMIT 100 on every pass; two newer complete future paths never settle. The same failure occurs with pre-issuance or future first-observation timestamps. This proves a code defect, not yet that it explains every production zero.

## Implementation
- Branch: `fix/path-settlement-progress-20261115`.
- Apply complete, already-observed same-source candle eligibility before the existing settlement work bound, using the candle primary key. Preserve exact grid/time checks, original probabilities, immutable source provenance, ambiguous TP/stop treatment, companion close-target scoring and all research/after-cost gates.
- Incomplete paths remain pending for later valid source evidence. Newly complete paths progress without a process-local cursor or skipping unpreserved work, including across worker restarts.
- Extend the existing SELECT-only host receipt with issued, pending and due counts, oldest due target and horizon breakdown. Distinguish `no_recent_issuance`, future targets, backlog and observed settlements. Correlated side rows are explicitly not independent targets or executable-cost proof.

## Validation and production acceptance
- Baseline reproducer failed before repair. Focused SQLite regressions pass; PostgreSQL fixtures run in repository CI. Tests cover full-batch starvation, source/time exclusions, restart, later-arriving valid source data, bounded multi-pass progress, frozen forecast values, source-isolated 15m/60m settlement and read-only lifecycle counts.
- Require exact-head and merged-main PostgreSQL CI, non-skipped native Discloud source parity, and a fresh authenticated path lifecycle receipt. If issuance is absent, investigate the empirical training/caller path next; if complete paths remain due, inspect their exact source evidence. Do not call the target feature production-complete from a merged selector repair.
- Keep #151/#6/#299/#138 open for calibrated probability/target and after-cost proof; #347 remains open for missing actual deadlock lock-graph evidence. Only retire this task's branch after validated integration; preserve unrelated work.

The prior [source-evaluation task record](docs/archive/ACTIVE_TASK_SOURCE_EVALUATION_20261115.md) is preserved byte-for-byte. It is history, not an active task lock. The PR and linked issue carry exact CI, integration and post-release acceptance results.
