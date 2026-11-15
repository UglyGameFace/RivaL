# RivaL active bot work — November 15, 2026

## Objective
Finish source-exact XMarket quote/settlement evidence and complete, attributable family evaluation. No real-money orders, fabricated historical inputs, duplicate training, weakened gates or parallel deployment path.

## Verified baseline
- Main: `fe6c2cf5ab0c9f0ed323d67f6b4e44c9f06a9709`; #603/#604 merged, full PostgreSQL CI/native parity/deployed panels passed; both owned branches retired.
- Fresh authenticated host read, [run 43742464840](https://github.com/UglyGameFace/RivaL/actions/runs/43742464840): current boot began 17:32:44 UTC; completed scope census at 18:00:52 UTC; five fitted scopes issuing, zero silent scopes/open silence incidents, fresh heartbeat at 18:08:54 UTC; 2,447 immutable XMarket shadow rows over 24h.
- Five source-specific evaluations exist; none qualifies after costs. These saved evaluations are not a real-time quote counter, and raw forecast rows are not independent targets. Real live alert/recovery transitions have not been induced or verified.

## Current implementation
- Branch: `fix/source-proof-complete-family-inputs-20261115`; no overlapping open PR at discovery.
- Reproduced on unchanged main: three 8,000-row boosted-family histories consume the evaluator's globally sorted LIMIT 24,000; a real pinned XMarket family disappears from the evaluation entirely.
- Repair: discover eligible family names independently; read each source-exact family using the existing indexes and a 24,000-row memory bound. Filter the active artifact before the bound. Detect overflow with one extra row; incomplete input cannot promote or appear as verified proof. Preserve earliest-target deduplication, costs, calibration, source identity and all numerical gates.
- Extend the existing authenticated receipt with SELECT-only recent exact-source forecast/quote entry/exit counts and timestamps, matched by original forecast key, artifact hash and issuance time. Label raw rows as operational evidence; expose saved evaluation timestamps separately. No new worker, schema or price replay.

## Validation and handoff
- Baseline saturation reproducer fails before the repair. Targeted regressions cover the global-cap loss, queued-artifact crowding, incomplete-input non-promotion, earliest targets, original quote windows, actual stream caller, source/hash isolation, timestamps and SELECT-only observation.
- Final targeted local run passes 54 tests with 35 PostgreSQL fixtures skipped; repository Ruff and diff checks pass. PostgreSQL CI, merge/native parity and fresh production quote progress remain required.
- Keep #299/#6/#138 open for genuine independent outcomes and after-cost superiority; keep #347 open for missing production deadlock lock-graph evidence. Do not claim profitable skill from imports, new capture rows or weight updates.
- Next: finish validation, integrate this cohesive repair, verify actual recent source-matched quote entries/exits and full source evaluations on Discloud, reconcile issue #299 and safely retire only the owned branch.

[Earlier task and production evidence](docs/archive/ACTIVE_TASK_THROUGH_20261115.md) is preserved as history, not an active work lock.
