# RivaL Evidence Board Worker

This directory stores the **exact read-only Worker script** deployed to Cloudflare as `rival-evidence-board` version `7081074f-af3b-4c20-a35d-9a900c08f783` on November 13, 2026. The corresponding Cloudflare production service binding is `RIVAL_EVIDENCE_RECEIPTS` → `rival-evidence-collector`.

The site uses Cloudflare's legacy **Service Worker** script format. Uploads of the source through the module-only `/versions` endpoint do not work; use the validated Service Worker `PUT /accounts/{account_id}/workers/scripts/rival-evidence-board` path and preserve the service binding, compatibility date, CSP, and GET/HEAD-only access. The source is now versioned so future edits can be compared and validated before deployment. Never upload environment secrets or bearer credentials into the public script.

## Real source vs display

| Screen | Actual data | What is not verified |
| --- | --- | --- |
| Overview | Signed source receipt timestamp, Discloud/DB aggregates, model reports and paired Brier scores | Current live status between source observations |
| Experiments | Actual bounded GitHub PR records, independently verified XMarket training reports and per-scope paired score observations | Full Model Lab genome inventory, active artifact SHA, a profitable model |
| CI Queue | Signed bounded GitHub workflow runs; on-demand **public** GitHub job steps and artifact listings | Private job logs, full run history, successful production uptake |
| Evidence | Signed Drive reports plus source-specific host/DB counters and independent historical candidate bars | Source matching where the training gate has blocked it |
| Audit | Signed GitHub PR metadata, verification timestamps and modeled missing proof | Claims that a merge/CI success implies a live model |

The Worker serves only GET/HEAD and never makes write calls to Discloud, PostgreSQL or Drive. The public `/api/run` read-through calls GitHub's public API for a run already found in the signed bounded snapshot. Missing rate-limited results are not interpreted as zero jobs or zero artifacts. This is not an authenticated private GitHub log proxy.

The web page checks cached receipts every 60 seconds while visible. **Source collection is different**: the GitHub Actions workflow `.github/workflows/evidence-board-receipt.yml` nominally runs every 10 minutes, with GitHub OIDC signing and an independently verifying Cloudflare collector. GitHub can delay scheduled work; at the time this source was committed, real recurring `event=schedule` runs had not been proven. Track that separately in [issue #554](https://github.com/UglyGameFace/RivaL/issues/554). Do not label old receipts LIVE.

Model-source constraints stay strict: 12 XMarket November 12 reports had no eligible matched prospective bars; no fitted/imported XMarket model or shadow rows were shown by the last signed host receipt. A separate 8-scope paired Brier score sample and 102 observed changing-weight streams are **not after-cost, out-of-sample proof of superior model performance**.

## Regression

### Per-timeframe scores and the completed-day evidence chain

The incumbent scoring receipt reads at most 500 valid settled source pairs
**per asset and horizon**, using the existing asset/horizon/issuance index.
The previous shared per-asset limit could let busy 15/60-minute scopes crowd
out a 5-minute scope. Every receipt now identifies its sampling policy,
distinct target windows and venue count. Venue pairs remain correlated
observations, not independent trades. Settlement must already exist at the
capture time. The chart never joins snapshots with different sampling policies.

Host XMarket receipts preserve the actual capture/export/fit/import/shadow
stage, the eligible-day cutoff, the recent pending backlog and published-day
counts for each asset. Private Drive file IDs and raw rows are discarded.
An old blocked training report keeps its own date; it cannot override a newer
host export acknowledgment or prove that the trainer restored those bytes.
Offline source/split/gate rejection reasons survive the publisher and receiver.
Offline acceptance still does not prove import, prospective shadow issuance,
promotion or after-cost edge.

`collector.js` versions the production **module Worker** receiver. Its OIDC
issuer/signature/repository/ref/workflow checks and KV binding are preserved.
Deploy it with `main_module: "collector.js"` and `EVIDENCE_KV` bound to the
existing namespace; deploy the board with the original service binding and
legacy Service Worker format. Receiver changes are backward compatible with
older signed payloads. Deploy the receiver before the publisher emits new fields.

`node tools/test_evidence_collector.js` exercises the actual receiver with a
locally generated RSA key, signed fixtures, claim/signature rejection, the
sanitized KV round trip and private-field removal. It makes no network calls
and creates no production evidence. PostgreSQL CI also executes the actual
bounded scoring query against busy mixed-horizon fixtures.

Run `node tools/test_evidence_board_ui.js`. The normal Python test suite also runs this via `tests/test_evidence_board_ui_source.py` when Node is available. The test parses and compiles the **real** Worker-served browser script, populates synthetic signed-source fixtures without network or secrets, and exercises all research queues, Experiments, CI, Audit, report/PR drill-downs, and read-only route invariants.

This source snapshot and Cloudflare's previous Worker versions provide rollback evidence; any later production change must update this source and tests in the same cohesive change. The test's synthetic fixtures prove UI behavior only, not source-model accuracy.
