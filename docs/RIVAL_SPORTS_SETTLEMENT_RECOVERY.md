# Sports settlement recovery

The finalized-result ledger and prospective forecast ledger describe different
evidence. A valid provider result is retained even when an existing forecast
cannot legitimately be scored. Original forecast and result fields stay immutable.

`rival_sport_settlement_exclusions` is an additive, restart-safe table keyed by
event ID, with a fixed reason and the actual exclusion time. Identity or kickoff
mismatches, forecasts issued at/after kickoff, invalid clocks or probability
vectors, unsupported basketball ties, and malformed legacy result records are
terminal exclusions. They never receive a win/loss label, Brier loss, settled
timestamp, or promotion credit. Soccer draws still settle normally.

The existing bounded reconciliation worker excludes these IDs before its limit.
Thus even a full 100-row poison batch is retired and the next cycle can reach
later valid forecasts. Identical redelivery is idempotent; contradictory final
scores still raise an integrity error. Infrastructure failures still roll back
the transaction and remain retryable, rather than becoming exclusions.

The six-hour sports integrity log reports actionable `unsettled_finalized`
separately from `exclusions` grouped by reason. A zero backlog is not evidence
that all forecasts were scored. Inspect both counters and the exclusion ledger.
New exclusions also emit an event ID and fixed reason without raw provider data.

Deployment creates the table through the existing schema initialization path.
No ledger data is deleted or rewritten. Previously rolled-back valid results
must arrive through the existing bounded provider poll; rows already stored can
be reconciled locally. Provider quotas and checks remain enforced.

Rollback: reverting the code leaves the additive table and all evidence intact,
but old code ignores exclusions and can resume retrying poison rows. Do not
delete exclusions to make counts look better. Any later remediation must verify
the original evidence and explicitly account for the prior exclusion.

SQLite and PostgreSQL regressions cover result retention, restart/redelivery,
more than 100 poison rows, ties, invalid vectors/clocks, malformed legacy results,
and retryable infrastructure failures. GitHub CI provides PostgreSQL execution;
successful CI or file upload alone does not establish live provider recovery.

Auto-Hunt remains intentionally retired. This change restores no subscriptions,
alert delivery workers, or unsolicited DMs.
