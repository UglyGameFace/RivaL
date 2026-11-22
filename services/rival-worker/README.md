# RivaL Worker — scorecard materialization (Slice 1)

A small Discloud service that moves the bot's heavy *descriptive*
scorecard aggregates out of the bot process. Every five minutes
(offset 150 s past the boundary) it runs the existing uncached functions
verbatim — `ForwardAccuracyMonitor._summary_uncached`,
`_calibration_uncached`, `_active_learning_evidence_uncached`, and the
Polymarket learner's status assembly (`summary` + `pooled_summary`) —
and upserts the result dicts into the `rival_materialized_reads` table
(40 keys: 4 assets × the summary/calibration/active-learning/Polymarket
kinds across horizons 5/15/60).

The bot reads those rows only behind `RIVAL_MATERIALIZED_READS`
(default off) and only while fresh (≤ 15 min); any miss falls back to
the exact live computation. Nothing that issues, settles, promotes, or
admits ever reads the table.

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

## Shadow mode and the canary

`RIVAL_WORKER_SHADOW` defaults to `1`: the worker writes only
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
