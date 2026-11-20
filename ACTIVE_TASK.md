# RivaL ACTIVE_TASK — canonical current execution plan

Updated 2026-11-20 01:44 EST. This is an execution checkpoint, not a global task lock. Prior detailed evidence is preserved in Git history.

## Objective and gates
Restore source-authentic XMarket research history, preserve immutable prospective forecasts, evaluate executable matched quotes against source baselines after fees/slippage, and verify safe Discord delivery and promotion/rollback. Never trade real money, fabricate/backdate data, mine holdouts, trigger recurring historical training, or relax qualification (>=150 independent forward targets, >=21 days, >=80% matched quote coverage, robust positive after-cost baseline comparison).

## Fresh repository reconciliation
- Latest returned main commit: `3992ef79253c1888e78c8d1d2e512ecec8f17a3f` (2026-11-20 06:16 UTC), merging #723, a host-read retry and error-classification fix. #712 source-integrity diagnostics **merged** at `97f61808b3c011cc8469fac6a8c1f92df2f363e5` (2026-11-20 06:01 UTC). Do not describe #712 as draft/open or repeat its diagnostic patch.
- GitHub open-PR search returned zero open PRs at this inspection. This is a point-in-time result, not a branch-deletion instruction. Parallel forward recorder, evidence-board and host work must be preserved.
- Commit-specific workflow lookup returned no PR-triggered workflow runs for either merge SHA; this endpoint does not establish current-main CI or production success. Fresh full SQLite/PostgreSQL CI, Discloud running SHA/health and live source readback remain unverified.
- The merged #712 distinguishes rejected original integrity metadata fields, but does not prove that the stored Drive originals have correct metadata or repair their publisher lineage. The merged #723 addresses host-read classification, not OKX history recovery.

## Source evidence and highest-priority actionable blocker
- Authenticated recovery run [#44517973204](https://github.com/UglyGameFace/RivaL/actions/runs/44517973204) rejected recorded-partition originals for missing verified SHA-256/size metadata; actual rejected file IDs, declared fields, Drive MD5/size, production principal and publication run lineage are not yet correlated.
- Earlier OKX native-month BTC/ETH/SOL/XRP conflicting manifest snapshots remain unresolved. Typed `NativeMonthConflictError` and `docs/OKX_NATIVE_MONTH_CONFLICT_RECEIPTS.md` already provide bounded conflict handling. Do not choose latest, silently coalesce divergent originals, or expose private IDs.
- First train-only ablation had 2 distinct eligible inner evaluation days versus 7 required across 12 5/15/60m scopes. Five fitted research-only scopes and seven missing are historical counts, not freshly measured. No newly recovered eligible days or after-cost qualified edge established.
- **Next code outcome:** inspect the current source-native restore and publisher against *authenticated, read-only* rejected-original metadata and recorded publication runs; correlate both real SHA/provenance identities per venue/contract/month; fix the authoritative publisher/restore boundary only where root cause is proven. Test identical replay, divergent originals, tampering, cross-asset collisions and legacy metadata; keep unresolved conflicts fail-closed. Validate lint, critical tests, full SQLite/PostgreSQL CI and credentialed readback.
- If private original-source credentials or receipts are inaccessible, record the exact access boundary and advance independently verifiable forward quote matching, fee/slippage/baseline grading, overdue path settlement or next-target Discord channel delivery. Do not generate another speculative integrity patch.

## Integration and production verification
- #712 and #723 are already merged; check their current-main tests and production receipts rather than reopening duplicate PRs. No current Discloud runtime SHA parity or production recovery was established by this inspection.
- After *new verified* source coverage, count distinct eligible days and only run explicitly authorized source-exact train-only experiments under original chronological/Brier/ECE/daily gates. Keep existing research scopes prospectively issuing independent shadows.
- Reconcile issue #295 (history) and issue #6 (forward qualification) only on demonstrated outcomes; do not close unresolved issues. Retire branches only after checking for unmerged commits, active work and protections.

Each run: inspect newest main, ACTIVE_TASK.md, PRs, source, CI, host/source receipts and parallel work; implement and validate the highest-priority evidence-backed repair; update this single file in place. Git history preserves prior checkpoints.
