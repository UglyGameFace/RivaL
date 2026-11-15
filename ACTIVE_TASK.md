# RivaL active bot work — November 15, 2026

## Scope and guardrails
Keep the full eleven-area [RivaL remaining backlog](docs/RIVAL_REMAINING_BACKLOG.md) in view. Model accuracy, independently forward-stressed after-cost proof, missing seven XMarket fits, 8 additional source-matched venues, Polymarket/Kalshi, historical input fidelity, paper paths, database reliability, isolation/backups, Android Discord UX and sports/DraftKings remain independent acceptance requirements. Website repair stays paused. No backdating, invented quotes or real-money orders.

## Latest verified production checkpoint
- Authenticated, non-skipped [host receipt 43752590356](https://github.com/UglyGameFace/RivaL/actions/runs/43752590356) on then-deployed `main@b1ad445` showed five issuing XMarket fitted scopes, 0 qualified XMarket edge, ordinary SOL5m 3,887 independent forward targets over 16 UTC days with 98.0% quote coverage but **no** demonstrated baseline or after-cost improvement.
- That host receipt also showed 140,088 pending 14d paper path side rows, 138,938 overdue and 0 close-target settlements over 24h despite active issuance. Path side records are correlated and not independent targets. Later **skipped** host workflow steps do not supersede this evidence.
- No actual PostgreSQL lock graph for historic 40P01 was captured. Recent bounded diagnostics have zero observed lock failures in the limited window, but verified slow reads reached 20.2s; issue #347 remains open. Issue #151 tracks settlement backlog, #6 tracks prediction quality.

## Delivered engineering
- [PR #607](https://github.com/UglyGameFace/RivaL/pull/607) was **squash-merged into main** at `f6ce16f4b271fea90c643e4bb64955447415f8ce`, after an exact-head passing PR CI with PostgreSQL tests, critical regressions and Ruff.
- Added SELECT-only sample of oldest and newest overdue 15m/60m path forecasts by exact venue/contract/quote to the authenticated receipt, reporting reasons for missing/invalid/not-yet-known future bars versus already-ready path evidence. Does **not** reconcile, modify settlement or conclude backlog resolved. Regressions exercise missing/pre-issuance/future/ready source states.
- Preserved oversized (9MB and 3MB whitespace) Deribit test fixture contents while replacing dangerous verbose pytest parameter names with short IDs. Also archived the prior task record under [docs/archive/ACTIVE_TASK_PATH_DIAGNOSTIC_20261115.md](docs/archive/ACTIVE_TASK_PATH_DIAGNOSTIC_20261115.md).
- Main-branch CI, the subsequent non-skipped native Discloud source parity and a real authenticated host receipt must still independently establish #607 production diagnostics; do not count PR CI alone.

## Current independent change
- [PR #609](https://github.com/UglyGameFace/RivaL/pull/609) on branch `fix/quote-proof-gate-explanations-20261115` explains Discord's misleading `quotes ✗` alongside 100% *raw* coverage, by showing actual coverage and missing minimum 150 eligible windows, 150 completed windows, or 50 trades. It preserves all existing mathematical gates, the source-exact stored verdict, compact embeddable output, and read-only operation.
- Tests cover the actual confusing 100% case, prior missing/duplicate source quote details, model-proof status, and Discord panel length. Validate exact-head Ruff, critical regressions and full PostgreSQL CI before merging. Keep independent [PR #608](https://github.com/UglyGameFace/RivaL/pull/608) for LFM3/Jarvis separate; its changed files do not overlap this PR.
- Then verify main CI and native source parity on the actual latest main SHA. Only after a new **non-skipped** host receipt exposes `path_targets.settlement_sample` should the next source-specific settlement worker/collector repair be selected. If ready paths but no settlement: inspect worker logs, SQL timings/statement class. If source-missing: inspect exact matched candle collection; never backdate or fake labels.
- Sufficient independent future calendar days and statistically valid model profitability are **not** immediate engineering completions. Preserve NO CALL and blocked trading when no qualified edge.
