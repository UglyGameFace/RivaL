# RivaL ACTIVE_TASK — canonical current execution plan

Updated 2026-11-19 18:31 EST. This is a short status checkpoint, not a global work lock. Earlier detailed evidence remains in Git history (prior blob 83db098437f104f2bbb5b846ed5cefba909e0cae); verify current main and parallel work each run.

## Objective and governance
Source-authentic research-only XMarket recovery, immutable prospective scoring, executable paired quotes, after-cost baseline comparisons, safe promotion/demotion/rollback, and truthful Discord delivery. No real-money orders, retrospective forecasts, holdout mining, recurring retraining, lowered thresholds, or fabricated source history. Edge requires >=150 independent forward targets, >=21 days, >=80% matched quote coverage, and robust positive after-cost comparison.

## Latest verified repository evidence
- Main: `9844db0e6580658717df1422709d96c653915e04` (PR #711 merged). Open PR search returned none at this checkpoint.
- #710 merged: Evidence Board collector KV write budget; #711 merged: alert deduplication fallback. Their merges do not prove Cloudflare production recovery.
- Earlier #701 added bounded recorded-partition diagnostics; #703 removed temporary OAuth fingerprint workflow. The current publisher in `perpetuals/records_backup.py` uses `ensure_history_file()`; the Drive publisher supplies `rival_sha256`. This does **not** prove the metadata of stored original files.
- Authenticated recovery run [#44517973204](https://github.com/UglyGameFace/RivaL/actions/runs/44517973204) rejected recorded-partition originals with `records original lacks verified SHA-256/size metadata`. The reader checks original identity, MD5, `rival_kind=history`, SHA-256 and bounded size; the exact defective field and original publication lineage are not established.
- Earlier OKX source conflicts affect BTC/ETH/SOL/XRP native-month manifests. Existing typed `NativeMonthConflictError` and `docs/OKX_NATIVE_MONTH_CONFLICT_RECEIPTS.md` already provide bounded conflict evidence; do not duplicate or relax them.
- Earlier first XMarket train-only ablation had 2 distinct eligible inner days vs required 7. No later eligible-day gain is independently verified. Five fitted research scopes (BTC 15/60m, SOL 15m, XRP 15/60m), seven missing, and no qualified after-cost edge were the last evidenced counts; remeasure before reporting current counts.
- Parallel price-first changes affect directional challenger issuance. Respect current main and do not silently re-enable it. Jarvis work is unrelated.

## Active implementation
- Draft [PR #712](https://github.com/UglyGameFace/RivaL/pull/712), branch `fix/records-original-integrity-reasons-20261119`: classify exact rejected records-original metadata field without exposing private identifiers, add fail-closed malformed-size handling and parameterized regression tests. CI #44615546070 passed on head `be8b9b8f` (lint, critical regressions, full test job with PostgreSQL service); verified historical research #44615545838 passed. Authenticated original readback remains pending. This is diagnostic progress, **not** restored history or proof of publisher root cause. Main remains `f13a4c93` until merge.

## New authenticated read-only Drive evidence (2026-11-19)
- Connected UglyGameFace Google Drive account can enumerate the `RivaL Data Warehouse` root and its `history`, `manifests`, `staging`, and other folders. This disproves a blanket lack of access to this connected account's warehouse root; it does **not** establish parity with the credentials or Drive folder IDs used by production workflows.
- A metadata-only query for direct children of this connected account's `manifests` folder returned no items. Metadata-only history queries for filenames containing `records`, `okx`, or `source_native` returned no matches; an unfiltered history listing returned many Binance auxiliary checksum files. These are bounded search observations, **not** proof that the rejected originals were deleted or never published: originals may be elsewhere, named differently, or accessible only to the workflow identity.
- No original-file identity from failed recovery run #44517973204 was available to correlate with these results, so no SHA/size defect can be attributed to a particular file and no admission change is justified.

## Highest-priority actionable repair
1. Correlate the exact rejected original IDs and production workflow Drive principal/folder configuration with the connected warehouse root; then obtain authenticated read-only metadata for **each rejected original**: bounded file identity, declared SHA/size/kind, actual Drive MD5/size, source run and publication provenance. Identify the exact failing field without exposing credentials, folder IDs, or private content.
2. Correlate both real original OKX manifest SHA/provenance identities for each venue/contract/month; distinguish identical replay, differing manifest provenance, differing history bytes, tampering, and cross-asset collision. Preserve unresolved divergence fail-closed.
3. Repair the authoritative publisher/restore boundary **only when the originals prove a root cause**; test divergent, identical, tampered, cross-asset and legacy metadata paths. Run lint, critical tests, full SQLite/PostgreSQL CI, then credentialed source readback and chronological eligibility. No new training until verified coverage and explicit authorization.

## Independent operational work
Verify fresh Discloud deployed SHA and health, source-specific forward quote matching, fee/slippage/baseline grading, next-target Discord channel delivery, and overdue path settlement. Investigate historical RAM-kill recurrence using fresh host measurements before declaring fixed. Do not confuse Model Lab six-year candidates with XMarket fits.

## Integration status and next proof
No source-authentic history repair, fresh eligible-day increase, full current-main CI, live Discloud SHA parity, or model promotion was established at this checkpoint. Draft PR #712 is open with CI passing but original-file readback and live parity pending. Preserve other branches and independent changes. Next proof is authenticated original-file metadata and publication lineage, followed by an evidence-backed cohesive repair; keep relevant issues open until production acceptance.

At every run: reread this file and current main/PRs/CI/host/source artifacts; recover existing work, implement and test a meaningful repair, integrate only with authorization, verify live outcome, safely reconcile fixed issues/obsolete branches, and update this document in place. Git history retains earlier detailed receipts.
