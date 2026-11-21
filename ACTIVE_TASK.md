# RivaL ACTIVE_TASK — canonical current execution plan

Updated 2026-11-20 21:14 EST. This is an execution checkpoint, not a global task lock. Prior detailed evidence is preserved in Git history.

## Objective and gates
Restore source-authentic XMarket research history, preserve immutable prospective forecasts, evaluate executable matched quotes against source baselines after fees/slippage, and verify safe Discord delivery and promotion/rollback. Never trade real money, fabricate/backdate data, mine holdouts, trigger recurring historical training, or relax qualification (>=150 independent forward targets, >=21 days, >=80% matched quote coverage, robust positive after-cost baseline comparison).

## Fresh repository reconciliation
- Current main at inspection: `a47e578a68a3f8dba78a63897f2bae012e1f405f` (2026-11-21 01:13 UTC), merging #736 Polymarket 1h slug repair; no open PRs returned. Preserve independent work.
- [CI #44844632198](https://github.com/UglyGameFace/RivaL/actions/runs/44844632198) completed **success** at 2026-11-21 00:41 UTC on earlier main. [Evidence Board receipt #44854407059](https://github.com/UglyGameFace/RivaL/actions/runs/44854407059) completed success on current `a47e578a`; this is not full current-SHA SQLite/PostgreSQL CI proof.
- GitHub `discloud/commit` status on `a47e578a` reports success; actual running Discloud process SHA, host health and live historical recovery remain unverified.
- Daily records publisher traced to `RecordsBackup.save_day()` -> `GoogleDriveArchiveClient.ensure_history_file()`; existing-object replay verifies size/MD5 without requesting `appProperties.rival_sha256`, whereas training warehouse admission requires SHA-256. This is a source contract gap, **not evidence that rejected production originals have that metadata defect**. Do not alter immutable originals or relax admission absent authenticated readback.
- #712 diagnostics and #723 host-read classification remain merged; no duplicate repair. No source code, tests, archives or training modified in this inspection.

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
