# RivaL Evidence Board Worker

This directory versions the Cloudflare `rival-evidence-board` Worker source. The last independently verified live deployment before this change was `a9111fab-5b7d-4a9c-9f34-343427537636` on November 12 at 23:52 EST (November 13 at 04:52 UTC). This source must be tested before release and the production version compared after deployment. The `RIVAL_EVIDENCE_RECEIPTS` service binding points to `rival-evidence-collector`.

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

The outcome clock is the **close of the target five-minute candle**, at
`target_opened_at + 5 minutes`, for every horizon. This is the same clock
used by `ForwardAccuracyMonitor.run_once` and its settlement lookup. Using
the opening time as a deadline incorrectly excludes normal 5-minute calls
issued after that candle opens. Such calls are prospective only until the
closing outcome; the score receipt shows how many were issued inside the
target candle and their minimum remaining lead time. It rejects issuance
at/after the close, settlement before the close, naive clocks and settlement
after the snapshot. V3 snapshots are kept separate from the earlier V2
pre-opening-only sample. No prediction, label, model, baseline, execution
or promotion rule is changed by this read-only reporting correction.

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

## November 14, 2026 source-fidelity audit

Cloudflare Browser Rendering independently loaded the mobile website, but the most recent verified signed receipt was captured at `2026-11-14T20:27:33Z`, over two and a half hours before the audit. GitHub Actions run [43637558551](https://github.com/UglyGameFace/RivaL/actions/runs/43637558551) succeeded as a scheduled publication. The workflow has a nominal ten-minute GitHub cron, not an SLA; browser polling can never create a new authenticated Discloud/PostgreSQL/Drive receipt. Confirm multiple subsequent `event=schedule` runs and KV provenance before claiming that automatic collection is reliable. Issue #554 remains unresolved until evidence establishes recurring consistency.

The previously labeled `Matched historical scopes` number only counted asset/horizon reports with nonzero *separate provider comparison pairs*. It did **not** verify aligned historical training examples. It is now labeled `Provider comparison scopes` and explicitly excludes eligibility claims. All blocked-fit labels remain attached to dated reports and cannot override more recent recovery evidence. A source-health endpoint `/api/status` returns HTTP 503 when the signed capture is older than 25 minutes or unavailable (and 200 only for a recent verified receipt). `/health` remains a separate liveness-only check and **never** represents source/model health. Neither endpoint trains, publishes, trades, or changes the collector's OIDC trust boundary. Source proof, import hashes and prospective model-edge gates are unchanged.

## Independent publication triggers (November 14)

To mitigate GitHub's best-effort scheduled delivery, the signed publisher keeps its ten-minute cron and also runs on scoped `main` changes affecting the bot or Evidence Board, and **after successful same-repository `main` Production Release workflow completions**. The additional `workflow_run` gate explicitly requires conclusion=success, head_branch=main and the same source repository, and always checks out trusted `main`. It consumes no release artifacts or untrusted code, and preserves GitHub OIDC validation and provider secrets in Actions. This is not a guarantee that idle periods will receive a receipt within ten minutes: check actual source timestamps and repeated independently observed publications, and keep issue #554 open until the source cadence is reliable. A GitHub release is not proof Discloud runs the same SHA; the live receipt's host observations remain separate.

## Mixed XMarket training verdicts in the November 14 receipt

The signed source captured at 2026-11-14T23:54:42.516Z reports **five XMarket imported scopes but zero issued XMarket shadow rows** in the 24h window. The twelve newer/mixed-age Drive reports include **seven offline-gate rejections with positive report-reported eligible rows** and **five blocks with insufficient labeled rows**; none of the twelve reports is marked as a passing fit. The two categories are different evidence types: the host import counter has no artifact-hash correspondence to each latest report. The board must not describe all twelve as missing prospective history, claim their provider comparison pairs prove fit, or infer these five imports are newly qualified models. The historical Nov 12 candidate chart remains explicitly dated.
