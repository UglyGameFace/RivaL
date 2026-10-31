# Unattended production acceptance and runtime monitoring

This release extends the **existing** RivaL forecaster, model competition,
verified quote ledger and candidate registry. It never creates orders, revises
issued probabilities, promotes research models, or manufactures market proof.

## Production-process watchdog

The host process runs `rival-process-health-watchdog` every two minutes,
independently of Discord commands. It records real check timestamps and
de-duplicated open/resolved incidents in PostgreSQL:

- unexpectedly exited enabled workers (bounded restart: maximum three per hour,
  one per minute; never duplicate a still-running or blocked worker);
- stalled learning heartbeats, failing research stages, and eligible inputs
  that inexplicably produce no forecast;
- failed/missing source attempts and stale **completed** candles;
- causally settleable backlog, repaired with original idempotent settlement
  logic where target observations genuinely exist;
- low source-matched quote coverage on actionable forecast families;
- per-instrument challenger Brier regressions on at least 50 paired outcomes;
- unverified, failed or stale private-Drive model-import scans.

The watchdog writes every check to `rival_runtime_health_checkpoints`.
A fresh checkpoint proves *the watchdog ran*, not that individual providers
worked. Incidents go to the host ERROR log on transition, then show in the
existing `/rival tools health` screen. For owner notifications without opening
Discord, configure `RIVAL_HEALTH_DISCORD_WEBHOOK_URL` as a dedicated Discord
webhook URL in the Discloud **host environment**, not in GitHub source or a
tracked file. It sends state changes only, not a message every poll, and
never posts predictions or credentials. No webhook means host logs and durable
incidents only; do not claim an external page was sent.

A process cannot notify about its *own* complete termination. Discloud's
external app/process uptime monitor or another independent outside observer
must check that the process is alive and `host_watchdog.checked_at` advances.
This is distinct from GitHub workflow success.

## End-to-end real database receipt

With the host's actual `RIVAL_DATABASE_URL` set, run:

```sh
python tools/unattended_acceptance.py
```

No Discord command, login or UI interaction occurs. Exit code **0** means
at least one actual persisted source-exact model and incumbent pair meets:

1. immutable at-issuance model family, training fingerprint and probability;
2. a strictly later, originally time-verified target settlement;
3. both models' properly paired probability scores against that target;
4. a matching source/instrument bid/ask entry and later exit quote, both valid,
   so hypothetical spread and cost analysis has observable inputs.

Exit code **2** means **UNVERIFIED**, not passed. This is an operational
evidence receipt, NOT sufficient proof of profitable skill or automatic
promotion. It cannot retroactively verify fills or estimate funding from
candles. It intentionally leaves `model_approved=false` and
`real_fills_verified=false`.

The SQLite cold-restart acceptance test uses deterministic fabricated
*test-only* candles, samples, and quotes to exercise real persistence,
causal settlement, model scoring, duplicate handling and source identity.
The tests do not populate the live database. Their pass does not establish
that Discloud has already passed acceptance. A real host restart and another
receipt/row-count check are separately necessary to verify the deployed
process and its persistent PostgreSQL records.

Release criteria remain 21+ days and 150 genuinely future unique targets
per model/source, sufficient real quotes (80% or better), calibrated
superiority to meaningful baselines and positive after-cost stress
results. NO CALL remains authoritative until those independent gates pass.
