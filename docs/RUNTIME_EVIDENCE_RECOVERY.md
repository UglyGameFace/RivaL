# Runtime evidence and recovery

## November 12 findings

The six-minute-old status screenshot classified recent warnings as startup-only
before a quiet interval existed. Durable 24-hour counts were described using
only the last occurrence. Progress lines could combine the observation age of
one scope with another scope's targets. A connected book socket with no accepted
book was treated as blocked without an HTTP rejection. Status messages could
remain unchanged until the signature changed or another day elapsed.

The four challenger regression alerts represent worse paired scores, not a
crashed bot. The existing per-stream early-stop rule remains authoritative;
an alert alone is not permission to stop the incumbent or promote a challenger.
The model diagnostic record additionally highlights incumbent scores above the
flat-probability Brier baseline of 0.25. These raw samples do not prove a
statistically qualified loss or tradable edge.

The latest source-native and Gate archive failures inspected during this change
reported Drive OAuth `invalid_grant`. A later archive-training run authenticated
successfully. Therefore the older failures are evidence about those runs, not
proof of a continuing universal credential failure. Missing source months and
access failures must remain separate.

Archive training completed with all 36 baseline fits rejected in the inspected
run. Rejected fits are valid negative research results. The workflow trained
GBM twice and swallowed unexpected per-horizon failures; those execution defects
are repaired without reducing model acceptance thresholds.

## Recovery behavior

| Failure | Recovery and evidence |
| --- | --- |
| Stale status message | Edit one persisted channel/message identity every two minutes. Recreate only a deleted message (Discord 10008); permission and channel errors remain visible. |
| Recent warning mislabeled as startup-only | Require ten quiet minutes after the last startup occurrence. Display 24-hour and current-boot counts separately; preboot silence is not a repair claim. |
| Worker exits or settlement backlog | Existing watchdog restart/backlog repair remains authoritative; the card separately checks the age of the last completed watchdog pass. |
| Connected socket produces no book | Reconnect after 120 seconds without an accepted book, using existing bounded backoff. Explicit HTTP access denial gets the existing one-hour cooldown. |
| Concurrent expensive menu reads | Share one in-flight descriptive aggregate per key and cache successful results for five minutes. Historical-clock reads bypass this cache; issuance and admission remain uncached. |
| Recurring SQL latency or errors | Every ten minutes, record bounded fingerprints, timings, SQLSTATE, pool occupancy, own-connection wait kinds and one structural EXPLAIN without ANALYZE. Deadlocks retain redacted lock edges and active pool owners captured at failure time. Raise a durable watchdog incident after recurrence. SQL, bindings and predicates are not persisted. |
| Source research cannot restore data or authenticate | Persist a typed failure, exact six-month period, available scope details, report SHA and first-party Actions link. Existing scheduled acquisition/research jobs provide later attempts. An access rejection never counts as missing-history proof. |
| Later successful source research | Reconcile legacy blockers only with a newer verified report covering the relevant period and enough source scopes. Model rejection does not invalidate verified source data. |
| Empty research loop | Persist the driver's last outcome and scheduling reason, including weekly design limit, pool capacity, cooling period or exhausted grid. The watchdog detects stale/missing driver outcomes and failed handoffs. |
| BTC call consumes local spacing before XRP | Wait for the persisted next-capacity deadline and save each result independently. Keep atomic spacing, requester fairness and backend quarantine; the obsolete default 18/day cap becomes zero (unlimited). |
| Training crashes or produces no reports | Retain per-family exception class/stack locations, finish other families, exit nonzero for execution errors, and require all nine expected reports per asset. Summaries distinguish accepted, rejected, blocked and missing results. |

`rival_runtime_diagnostics` retains the latest database, research-loop and
model-evaluation snapshots. The existing live-learning host receipt exports
their timestamps and evidence without collecting plans or writing. Sentry and
the existing durable incident ledger continue receiving recurrence; a quiet log
alone is never labeled a verified fix.

## Operational limits

Unknown SQL problems are diagnosed, not answered with arbitrary indexes, larger
pools, blind transaction retries or automatic destructive changes. Review the
recorded wait class and structural plan before changing the affected query.
Sampling uses read-only transactions and a 1.5-second statement limit; persistence
has a two-second statement limit. Sampling runs beside the main watchdog so it
cannot suppress the health pass.

Historical bytes, permissions, refreshed credentials and genuinely better future
model results cannot be invented. Acquisition receipts must establish the actual
source coverage; model promotion still requires the existing distinct-outcome,
duration, matched-quote, calibration, baseline and after-cost checks. None of
these repairs guarantees profitable predictions or every possible future fault.
