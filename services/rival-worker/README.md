# RivaL Worker — scorecard materialization (Slice 1) + proof review (Slice 2)

A small Discloud service that moves the bot's heavy periodic work out of
the bot process. It runs two jobs on its own pool (`max_size` 4):

**Job 1 — scorecards (Slice 1).** Every five minutes (offset 150 s past
the boundary) it runs the existing uncached functions verbatim —
`ForwardAccuracyMonitor._summary_uncached`, `_calibration_uncached`,
`_active_learning_evidence_uncached`, and the Polymarket learner's
status assembly (`summary` + `pooled_summary`) — and upserts the result
dicts into the `rival_materialized_reads` table (40 keys: 4 assets × the
summary/calibration/active-learning/Polymarket kinds across horizons
5/15/60). The bot reads those rows only behind `RIVAL_MATERIALIZED_READS`
(default off) and only while fresh (≤ 15 min); any miss falls back to
the exact live computation. Nothing that issues, settles, promotes, or
admits ever reads the table.

**Job 2 — proof review (Slice 2).** Every twenty minutes, on the exact
slot schedule the bot's perp cycle uses, it drives
`FamilyPromotion.evaluate` for the slot's scope
(`perpetuals/promotion_driver.py`): the same
`ShadowLearningMonitor._eligible_streams` → `select_live_streams` →
`family_proof_review_slot` / `select_family_proof_scope` inputs, the
same `evaluate()` call with `now` pinned to the slot boundary, and the
same idempotent upsert into `rival_family_promotion` — the table every
Discord reader already point-reads, so no reader changes. The job fires
450 s past each 20-minute boundary (inside the slot window, staggered
off Job 1 on the shared pool); the evaluation stamp is the boundary
regardless of the offset.

## Layout and deployment

Deployed as its own Discloud app from the **same main SHA** as the bot:
the app zip contains this folder's files at the root **plus the repo's
`src/` tree**, so `main.py` can import `parlay_bot` (it also finds
`../../src` when run from a repo checkout). Python is pinned to 3.11 in
`discloud.config` — byte-identity of the streamed float accumulations
was proven under 3.11's naive `sum()`; 3.12's compensated summation
differs in the last ulp.

Environment: only `RIVAL_DATABASE_URL` (the same value the bot uses;
copy it through the Discloud env UI/helper at deploy time — never commit
it). The worker builds its own pool (`max_size` 4).

## Shadow mode and the canaries

`RIVAL_WORKER_SHADOW` defaults to `1`: Job 1 writes only
`shadow:`-prefixed kinds, which no reader serves. Run the canary
against a day of shadow writes:

```
python tools/materialized_reads_canary.py
```

It recomputes every key through the live bot path and compares
canonical JSON byte-for-byte, printing per-key MATCH/DIFF with SHA-256
hashes and byte lengths (never payload contents). Only after a clean
24-hour canary: set `RIVAL_WORKER_SHADOW=0` here and
`RIVAL_MATERIALIZED_READS` on the bot. Rollback at any point is
unsetting the bot's flag (readers return to today's live path) and/or
stopping this app; the table is inert and no migration touches an
existing table.

`RIVAL_WORKER_PROMOTION_SHADOW` defaults to `1`, independently of Job 1:
Job 2 then computes each slot's evaluation snapshot-isolated — it
hashes the exact promotion row `evaluate()` wrote, then restores the
promotion row, receipts, and offline-pin rows byte for byte, so the bot
remains the only real writer. (If a shadow evaluation ever *selects* a
family, nothing is restored and the cycle logs PROMOTION_DETECTED: a
genuine first promotion is never silently rolled back.) Prove it with
the pinned-input canary, passing the row hash the worker's shadow cycle
logged for the slot:

```
python tools/family_promotion_canary.py --slot N --expect-sha256 HEX
```

MATCH is byte-identity of the entire evaluation record (status,
selection, reason, full evidence JSON) under identical inputs. Only
after MATCHes across several slots, flip in this order: set
`RIVAL_WORKER_PROMOTION_SHADOW=0` here and commit the worker zip, then
set `RIVAL_PROMOTION_WORKER_DRIVEN=true` on the bot and commit the bot
zip from the same main SHA. Rollback is the reverse: unset the bot's
flag (its cycle resumes driving on its next slot) and/or re-enable the
worker's promotion shadow; an overlap window is safe because the write
is an idempotent upsert of an identical computation.
