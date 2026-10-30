# RivaL live-response audit: October 30, 2026

## Objective
Keep Discord menus responsive while PostgreSQL, perpetual-market collection, Auto-Hunt and six-month history recovery run together. Simplify beginner menus without replacing the real signal, alert, sports or paper-ledger services.

## Grounded incident evidence
- A collector's startup `PerpetualMarketStore.initialize()` failed with PostgreSQL's `pg_class_relname_nsp_index` duplicate index error on `idx_perp_market_candles_observed`. Each instance had its own thread lock; concurrent `CREATE INDEX IF NOT EXISTS` is not a database-wide mutex.
- A PostgreSQL connection was held for 66.41 seconds while recovery inserted hundreds of candles. Ingestion issued one SQL INSERT per candle within a single transaction and ran nine venue recovery jobs per cycle, competing with live signal reads.
- Auto-Hunt qualified checks timed out at their 7-second budgets while blocked on `recent_watches`. This is consistent with pool contention; the hosted service must verify the improvement before calling it fixed.
- Auto-Hunt daily cap messages are intended: 12 sent per user per tier per UTC day. The SportsGameOdds 250-entity reserve is also intentional, and is not a database failure.
- Some Polymarket requests returned ConnectTimeout or invalid-response exceptions; provider availability is independent of PostgreSQL fixes.
- New prospective forecasts remain gated by real causal labels and target expiry. More historical rows cannot retroactively create forward predictions.

## Code changes
- PostgreSQL `executescript` takes a transaction-scoped advisory lock across worker instances and processes before executing DDL.
- Historical candle writes are grouped into at most 100 rows per INSERT statement, with `ON CONFLICT DO NOTHING RETURNING`. The existing transaction, original immutable stored bar, conflict accounting and actual observation timestamps are preserved.
- Historical recovery alternates at most two providers per scheduled cycle. Durable cursors resume every provider in turn without dropping progress.
- Existing `GuidedHome` is restyled with four direct sections and simpler Auto-Hunt explanations. The existing domain-specific views, commands and safe deferrals remain authoritative.
- Signal colours are reserved for verified qualified UP/DOWN watches; forward accuracy is shown only with its actual settled sample, and XRP quotes keep meaningful precision.
- Added regression coverage and resolved preexisting repository lint failures preventing CI.

## Verification and release gate
- GitHub PR: #112, branch `fix/rival-live-responsiveness-and-guided-ui-20261030`.
- CI: pending verification on final branch head.
- Still required: PostgreSQL concurrency and pool-pressure tests, Discloud deployment and Discord click-through, before declaring production recovery.
- Do not promote a new model, erase historical evidence, bypass the 250-entity reserve or claim profitability from these fixes.

## Post-deployment observations to collect
- No duplicate index error on restart or simultaneous initialization.
- Pool checkout wait and held-connection p95 under live + backfill load.
- Successful /rival interactions, no Discord 3s acknowledgment failures, and signal timeout counts.
- Every recovery cursor advances over time; no misclassified historical bars.
- Polymarket provider timeouts distinguished from local DB issues.

## October 30 post-#112 Discloud follow-up (15:28:47 -03 / 18:28:47 UTC)
- Bot restarted, registered 4 guild commands, started two WebSocket markets and nine perpetual venue collectors, and reached READY.
- Auto-Hunt BTC/15m qualified decision timed out while gathering venue-specific historical accuracy; this remains a separate measurable latency problem.
- An Auto-Hunt subscriber's individual research delivery failed with psycopg `ProgrammingError` immediately after `forecasts=3`; an inspection of its suppression query found a literal SQL `LIKE 'qualified|%'` combined with parameters.
- psycopg treats `%` as placeholder syntax even in bound SQL text. The shared database adapter failed to escape literal `%` before inserting `%s` placeholders. The patch escapes percent literals at the adapter boundary, with a PostgreSQL 16 integration test for the exact query and modulo operator.
- `AutonomousResearch.initialize()` reran schema DDL during normal research work, allowing CREATE INDEX/INSERT lock inversions; it is now idempotent and its schemas are initialized before live workers are launched.
- Delivery failure logging now includes the stored exception traceback, not just the exception class.
- Earlier logs showed autovacuum ANALYZE cancellation, repeated client connection resets and a PostgreSQL database named `dankshield`. Those events may be from a different deployment epoch or shared database; this patch does not rename, purge or migrate production data.
- PR #113 code tests ran successfully against SQLite and disposable PostgreSQL; hosted Discloud latency, boot SHA, alert delivery and provider reachability remain unverified until after redeployment.

## October 30 source-grounded timeout and pool attribution follow-up (PR #115)
- Runtime report found one PostgreSQL checkout held for **86.79 seconds**. It is *not* evidence of a leak by itself; the original pool log recorded duration and thread name but no owner, query timing, commit duration or operation phase.
- PR #115 adds safe checkout attribution to application caller, aggregate SQL operation durations, commit time and oldest in-flight operation on pool exhaustion, without logging SQL text, parameters or credential-bearing DSNs. Observations after deploying that version are needed to tell long SQL from slow commits or work holding an otherwise idle connection.
- The 7-second qualified Auto-Hunt outer deadline previously nested independent venue quote requests and historical evidence lookups. PR #115 passes a **single 6.5-second monotonic deadline** through promotion, forecast, venue proof, multi proof and source-matched book lookup with the existing seven-second overall safety guard.
- Stage and overall timeouts get distinct log messages, and missing or timed-out proof/books remain fail-closed. Cancellation of an `asyncio.to_thread` wrapper does not terminate underlying PostgreSQL queries; a production query bottleneck still requires investigation after release.
- **Not covered:** nine-venue six-month XRP history completion, provider quota reserve, ability to control Discord privileged intents, optional voice dependencies, model accuracy improvement, actual Discord DM delivery or Discloud deployment. Previously merged PR #114 groups immutable candle conflict warnings and adds Live Mode and configurable daily caps.
- This status reflects source code and uploaded incident logs, not confirmed post-deployment runtime success.
