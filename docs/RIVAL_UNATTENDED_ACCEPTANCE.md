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
python main.py --acceptance
```

Run this in the Discloud application's console or via its authenticated
application-console API, **not** in an unrelated GitHub runner without the
production database connection. The supported entry point is the shipped root `main.py`, and the
acceptance implementation is in `src/parlay_bot/perpetuals/acceptance_cli.py`.
Do not depend on a separately deployed `tools/` directory. The source-tree
`tools/unattended_acceptance.py` path remains a local compatibility wrapper.

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

## All checks from the existing Discloud console

No separate PostgreSQL editor is needed.

For a quick watchdog-only result, run `python main.py --health`. This
checks persisted host evidence without opening Discord or sending orders.

**Read the separate statuses:** `status` retains its old aggregate meaning,
so it may show `DEGRADED` when *only experimental challenger models* are
underperforming. `operational_status` is the host/recovery signal: a fresh
watchdog without worker, feed, import, quote or settlement incidents is
`HEALTHY`, while genuine operational incidents remain `DEGRADED` and a
stale/missing watchdog remains `UNVERIFIED`. `research_status` is
`UNDERPERFORMING` when there are model regression alerts. The original
`open_incidents`, plus separate `research_regressions` and
`operational_incidents`, remain visible. The CLI exits successfully for a
healthy *host* even when research models are losing, and a controlled restart
can pass under that same condition. This never approves a losing model, hides
its evidence, changes promotion safeguards, or substitutes for off-host uptime
checks.

Run the ordinary command to see the causal receipt plus a new host_health
section (last watchdog timestamp, process boot identity, and open incidents):

    python main.py --acceptance

For rigorous restart verification, mark a fixed sample of finalized production
records BEFORE pressing Discloud Restart:

    python main.py --mark-restart

The result must be BASELINE_SAVED. This saves exact immutable row fingerprints
inside the persistent production database. Restart RivaL only, leave the workers
to issue new forecasts, and then run:

    python main.py --verify-restart

PASS requires a distinct bot boot after marking, an advanced fresh watchdog,
the previously saved finalized forecast and quote rows unchanged and present,
no reported open incidents, and at least one new forecast issued by the restarted bot (after its new boot).
UNVERIFIED explains missing evidence. Rerunning verification is safe.
Do not mark a new baseline after the restart until the old one is verified.

Earlier rolling 200-record summaries cannot prove individual row identity,
so the already performed restart alone cannot pass this stricter new gate.
This host-side check does not replace independent external uptime monitoring:
a completely stopped process cannot update its own heartbeat.

## When to declare operational acceptance

Record the exact running deployed commit, then check the following *against
the actual host and DB*, not a test fixture or a successful deployment upload:

1. **Causal receipt:** host CLI exits 0 and returns `HOST_EVIDENCE_PRESENT`.
   Confirm at least one source-matched challenger has issuance, verified
   pre-outcome identity/probability, independently later settlement, scored
   incumbent comparison and real prospective matching entry/exit book quotes.
2. **Controlled process restart:** capture the original forecast/quote
   primary keys, counts and settlement timestamps, restart the *Discloud
   bot process*, then check the same records still exist, the old rows were
   not duplicated or overwritten, and a later worker cycle processes new
   live input. Merely reopening a DB pool in CI is not a hosted restart.
3. **External health:** an independent Discloud status check reports the
   bot running after restart. Compare `host_watchdog.checked_at` at two
   separate observations and ensure it advances; confirm the critical
   workers/feed health and eligible settlement backlog have no unresolved
   incidents. Verify the operator alert route actually delivers a test
   incident if external alert delivery is required.

Only after all three steps are independently demonstrated should a dated
operational release receipt read `OPERATIONAL_ACCEPTANCE: PASS`.
Otherwise use `UNVERIFIED` or `FAIL` with a named blocker.

This is NOT `PREDICTIVE_EDGE_APPROVED`. That still requires prospective
21-day/150-outcome, source-matched quote coverage and the existing
calibration/after-cost governance gates. Neither operator actions nor the
host CLI should override `NO CALL`.

### Evidence handoff

Discloud's official application client exposes authenticated app status,
logs, restart and console-command operations. Use its existing owner's
account/credentials through approved host tooling; never paste the Discloud
token or PostgreSQL URL into a public issue, build log or chat. If the
authenticated console API returns only command acknowledgment rather than
stdout/exit status, obtain the actual probe receipt from the running host
or an explicitly scoped read-only DB connection. An acknowledged command
is not an acceptance result.


## Independent off-host application availability

A GitHub Actions monitor runs separately from the Discloud host at approximately
ten-minute intervals, using the existing CI secrets `DISCLOUD_TOKEN` and
`DISCLOUD_APP_ID`, with explicit `issues: write` access. The monitor reads
`GET /v2/app/{appId}/status` twice, compares the exact authenticated app
identity, and opens/reopens **one** `[Uptime] RivaL Discloud application
offline` issue on confirmed `Offline` status. The same issue is closed on
confirmed recovery. It never starts, stops, or restarts the bot; no user
credentials, API payloads or database data are logged. Malformed, unauthorized,
timed-out, or unknown provider responses produce an `UNVERIFIED` failed
workflow rather than being mislabeled online or offline.

Manual probe: GitHub Actions → **RivaL independent Discloud uptime** →
**Run workflow**. Workflow schedule is best-effort, not exact ten-minute
SLA. No separate service subscription is required. GitHub's availability is
also a dependency. This is genuine independent *process/container* status,
**not** proof that the database, feeds, model evaluations, or 5m quote coverage
are healthy; continue to use `python main.py --health` for those host-sourced
facts. When reviewing production readiness, cite both sources separately.

## Remaining source-native and forward-evidence gates

The retained finalized-row restart result and genuinely post-boot forecasts
prove autonomous persistence/continuity, **not** model skill or historical
training. Source-native six-month readiness requires six full consecutive
calendar months of immutable 5m bars from the **same** real venue, instrument
and quote, successfully published/restored from the private Drive manifests
and then used to train a source-compatible registered model. Binance-only
boosted artifacts do not count as another venue's native model. Keep training
and imports blocked for missing or inconsistent source-month receipts.

Learning must keep issuing and settling genuinely future-only shadow
predictions, with source-matched bid/ask observations. The governing
requirements remain at least 150 outcomes over 21 days, at least 80%
matched quote coverage, stable/positive comparison to incumbent and after-cost
stress tests. No backfill can manufacture those future outcomes. The system
must retain NO CALL when evidence is insufficient. A GitHub successful
deployment and a Discloud `Online` status do not waive any of these gates.
