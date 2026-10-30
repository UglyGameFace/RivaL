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
