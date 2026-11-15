# RivaL active bot work — November 15, 2026

## Objective
Carry the full eleven-area backlog in [docs/RIVAL_REMAINING_BACKLOG.md](docs/RIVAL_REMAINING_BACKLOG.md), while website work stays paused. Current cohesive integration outcome: explain why prospective paper path target settlement remains at zero on Discloud after the merged #606 starvation repair, without altering evidence, no-trade, training or promotion gates.

## Verified production finding
- Source baseline: main `b1ad445a1ad586e3ed68808e861fffda5fd6a6d6`, with #605/#606 merged, no other open PR at start.
- Exact-head main PostgreSQL CI and the non-skipped native [release 43752543040](https://github.com/UglyGameFace/RivaL/actions/runs/43752543040) passed.
- The latest authenticated, non-skipped [host receipt 43752590356](https://github.com/UglyGameFace/RivaL/actions/runs/43752590356) at 19:28 UTC records five fitted/issuing XMarket scopes, 2,614 summed scope-issued `xmarket_v1` shadow rows in 24h and 0 qualified paper edges. The best non-XMarket SOL 5m forward scope has 3,887 independent targets over 16 days and 98.0% paired quote coverage, but fails baseline superiority and after-cost proof.
- Crucially, the paper path receipt shows **38,472 raw side rows issued in 24h, 140,088 pending in 14d, 138,938 overdue, and zero settled close targets in 24h**, on the deployed #606 head. This does NOT yet prove #606 failed: the cohort may lack source-complete future bars, a worker may be blocked, or the snapshot may precede settlement. Long/short side rows are correlated, not independent outcomes. Subsequent release-triggered checks with skipped host reads cannot update this finding.
- XMarket quote gate reporting is confusing but the code is explicit: a >80% raw quote ratio alone does not pass the full gate. It also requires >=150 eligible windows, >=150 covered windows, >=50 challenger trades and separate calibration, superiority and conservative-cost gates. Do not relax any threshold.

## Current PR and implementation
- [PR #607](https://github.com/UglyGameFace/RivaL/pull/607), branch `fix/path-exact-source-settlement-diagnostic-20261115`.
- Add one bounded, SELECT-only exact-source overdue path sample to the existing authenticated host receipt. Sample oldest/newest 15m and 60m paths with deduplicated sides and classify missing source candles, wrong grid, timestamp/causality failures, invalid values and ready-to-settle paths. Explicitly label this a **sample, not a full-backlog census**.
- Extend the existing SQLite/PostgreSQL path regressions for missing, pre-issuance, future-only and complete observed candle paths, including a SELECT-only wrapper.
- Prevent verbose CI logs from ballooning on unchanged Deribit rejection fixtures by giving their 9,000,001-byte and 3,000,001-byte whitespace cases short pytest parameter IDs. Input bytes and assertions stay unchanged.
- A prior successful #606 task record is preserved byte-for-byte at [docs/archive/ACTIVE_TASK_PATH_SETTLEMENT_20261115.md](docs/archive/ACTIVE_TASK_PATH_SETTLEMENT_20261115.md). Its predecessor source evaluation history remains in archive.

## Test and production acceptance
- PR lint and critical regressions passed on earlier heads; the complete PostgreSQL CI on the latest PR head must pass. Earlier full reruns were superseded while the oversized test-id log issue was corrected; don't claim a pass on this head until the job actually completes.
- Only then merge a validated same-head PR. Verify main CI, non-skipped native Discloud source parity, and a **new actually executed authenticated PostgreSQL receipt**, including the new `path_targets.settlement_sample` findings. Do not count a skipped receipt as host evidence.
- If sample reports ready-to-settle paths but zero settlements, examine actual cycle logs and statement timing/error class before repairing worker access. If samples report missing/ineligible source candles, inspect exact venue/contract/quote observations and collector gaps without backdating. Continue bounded investigation and test a source-grounded repair rather than deleting pending forecasts or manufacturing outcomes.
- Keep #151/#6/#299/#138/#347 open pending genuinely independent future path/contract outcomes, calibrated superiority and actual after-cost improvement; 21 forward UTC days cannot be accelerated. Preserve existing zero-live-order policy and unrelated work.
