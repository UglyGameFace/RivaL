# RivaL ACTIVE_TASK — canonical current execution plan

Updated 2026-11-19 22:43 EST. This is a short status checkpoint, not a global work lock. Earlier detailed evidence remains in Git history (prior blob 83db098437f104f2bbb5b846ed5cefba909e0cae); verify current main and parallel work each run.

## Objective and governance
Source-authentic research-only XMarket recovery, immutable prospective scoring, executable paired quotes, after-cost baseline comparisons, safe promotion/demotion/rollback, and truthful Discord delivery. No real-money orders, retrospective forecasts, holdout mining, recurring retraining, lowered thresholds, or fabricated source history. Edge requires >=150 independent forward targets, >=21 days, >=80% matched quote coverage, and robust positive after-cost comparison.

## Latest verified repository evidence
- Main: `dfadc9834a600f5d66634a220bcd17f3e3b64a99` (PR #717 merged, Polymarket performance presentation). PR #716 merged settlement-index recorder/read-only evaluator; #715 merged NO EDGE display. Open PR search returned only #712 (source-recovery draft). These independent changes do not prove production readiness.
- #710 merged: Evidence Board collector KV write budget; #711 merged: alert deduplication fallback. Their merges do not prove Cloudflare production recovery.
- Earlier #701 added bounded recorded-partition diagnostics; #703 removed temporary OAuth fingerprint workflow. The current publisher in `perpetuals/records_backup.py` uses `ensure_history_file()`; the Drive publisher supplies `rival_sha256`. This does **not** prove the metadata of stored original files.
- Authenticated recovery run [#44517973204](https://github.com/UglyGameFace/RivaL/actions/runs/44517973204) rejected recorded-partition originals with `records original lacks verified SHA-256/size metadata`. The reader checks original identity, MD5, `rival_kind=history`, SHA-256 and bounded size; the exact defective field and original publication lineage are not established.
- Earlier OKX source conflicts affect BTC/ETH/SOL/XRP native-month manifests. Existing typed `NativeMonthConflictError` and `docs/OKX_NATIVE_MONTH_CONFLICT_RECEIPTS.md` already provide bounded conflict evidence; do not duplicate or relax them.
- Earlier first XMarket train-only ablation had 2 distinct eligible inner days vs required 7. No later eligible-day gain is independently verified. Five fitted research scopes (BTC 15/60m, SOL 15m, XRP 15/60m), seven missing, and no qualified after-cost edge were the last evidenced counts; remeasure before reporting current counts.
- Parallel price-first changes affect directional challenger issuance. Respect current main and do not silently re-enable it. Jarvis work is unrelated.

## Existing implementation and integration state
- Draft PR #712 (`fix/records-original-integrity-reasons-20261119`, head `6270aa77`) adds fail-closed field-specific metadata rejection and seven regression cases. CI #44615546070 passed on an earlier head; its latest documentation-only commits have not been retested. Compared with current main, branch is 6 commits ahead and 9 behind; integration remains unresolved. Current-main and PR versions of `training_warehouse.py` confirm the PR classifies rejected metadata fields but does not establish the original publication defect. Reconcile main changes without overwriting parallel work, then retest before integration.
- Read-only connected Drive enumeration previously found warehouse folders but did not correlate them with production workflow credentials or rejected original IDs. No authenticated rejected-original metadata or publication lineage has been established. Do not interpret missing filename matches as deleted originals.
- #717 is independent presentation work; do not alter it. Current main and host SHA parity remain unverified.

## Highest-priority actionable repair
1. Obtain authenticated read-only metadata for **each rejected original**: bounded file identity, declared SHA/size/kind, actual Drive MD5/size, source run and publication provenance. Identify the exact failing field without exposing credentials, folder IDs, or private content.
2. Correlate both real original OKX manifest SHA/provenance identities for each venue/contract/month; distinguish identical replay, differing manifest provenance, differing history bytes, tampering, and cross-asset collision. Preserve unresolved divergence fail-closed.
3. Repair the authoritative publisher/restore boundary **only when the originals prove a root cause**; test divergent, identical, tampered, cross-asset and legacy metadata paths. Run lint, critical tests, full SQLite/PostgreSQL CI, then credentialed source readback and chronological eligibility. No new training until verified coverage and explicit authorization.

## Independent operational work
Verify fresh Discloud deployed SHA and health, source-specific forward quote matching, fee/slippage/baseline grading, next-target Discord channel delivery, and overdue path settlement. Investigate historical RAM-kill recurrence using fresh host measurements before declaring fixed. Do not confuse Model Lab six-year candidates with XMarket fits.

## Integration status and next proof
No source-authentic recovery, fresh eligible-day increase, full current-main CI, live Discloud SHA parity, or model promotion was established at this checkpoint. Draft PR #712 remains unmerged; #717 merged independently. Preserve other branches and independent changes. Next proof is authenticated original-file metadata and publication lineage, followed by an evidence-backed cohesive repair; keep relevant issues open until production acceptance.

At every run: reread this file and current main/PRs/CI/host/source artifacts; recover existing work, implement and test a meaningful repair, integrate only with authorization, verify live outcome, safely reconcile fixed issues/obsolete branches, and update this document in place. Git history retains earlier detailed receipts.
