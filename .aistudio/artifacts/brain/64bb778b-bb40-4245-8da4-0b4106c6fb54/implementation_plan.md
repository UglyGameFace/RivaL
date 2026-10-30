# Fix PostgreSQL Pool Reuse, Candle Ingest/Discovery Hold Times & Auto-Hunt Slot Timeouts

Resolves the Discloud production errors where `Auto-Hunt qualified slot` times out at 7 seconds (`signals.py` lines 138 and 291) and `PostgresDatabase` logs long-held connections (`6.59s`–`29.1s`) and pool exhaustion (`max=8 checked_out=2`).

### User Review & Critical Decisions

> [!IMPORTANT]
> Both log traces point to three compounding bottlenecks in database connection lifecycle, candle table queries, and background quote qualification. All fixes preserve existing safety gates, read-only behavior, and the 8-connection PostgreSQL cap.

- **PostgreSQL Connection Reuse & Borrower Tracking**: Prevent `psycopg` v3's native context manager from closing pooled connections on `with` exit, and pop borrower diagnostics before returning connections to the idle queue.
- **Fast Candle Discovery & Batch Ingestion**: Bound shadow stream discovery in SQL to the active 20-minute observation window and pre-fetch existing candle ranges once per batch instead of issuing per-bar `SELECT` round-trips.
- **Short-Circuit Unqualified Background Signal Checks**: In `decision_only=True` background screening, only query venue proofs and live order books for venues whose forecast meets the directional watch threshold.

---

### 1. Overview & Core Concept

- **What It Does**: Eliminates the 7-second `TimeoutError` failures across `BTC/15m`, `ETH/5m`, `ETH/15m`, `SOL/5m`, and `SOL/15m` in Auto-Hunt and prevents `ShadowLearningMonitor` and `PerpetualMarketStore` from saturating the PostgreSQL connection pool.
- **Target Audience / Persona**: RivaL Discord bot operator and opted-in alert subscribers running on Discloud with external PostgreSQL storage.
- **Key Value**: Sub-second database checkouts via true connection pooling, 95%+ fewer SQL round-trips during 5-minute candle collection and history recovery, and zero wasted REST quote calls on weak/non-watch background forecasts.

---

### 2. User Experience & Operational Behavior

- **Key Operational Flows**:
  1. **Database Checkout & Return**: Every `with store.connect()` commits or rolls back the transaction and returns the open `psycopg` connection to the idle queue without closing the underlying socket.
  2. **5-Minute Collector & History Recovery**: Each batch pre-reads existing candles in one range query over the primary key, inserting only new bars and checking conflicts in memory.
  3. **Shadow Stream Discovery**: Queries only candles observed in the last 20 minutes via an index instead of scanning all 6 months of historical bars.
  4. **Auto-Hunt Qualified Screening**: Evaluates all `(asset, horizon)` slots without hitting external OKX/Bitget REST endpoints or venue-proof queries when no forecast in that slot is a directional watch.

---

### 3. Key Product Decisions & Trade-Offs

- **Decision 1: Manage `psycopg` Transaction Commit/Rollback Directly in `_Connection.__exit__`**
  - *Chosen Approach*: For PostgreSQL connections, commit on clean exit and roll back on exception without invoking `psycopg.Connection.__exit__`, and remove the borrower tracking entry before placing the connection back into `_idle`.
  - *Why*: In `psycopg` v3, `Connection.__exit__` automatically calls `self.close()` whenever `_pool` is unset. As a result, `_checkin` saw `raw.closed == True` after every `with` block, discarding every connection and forcing every query to pay a full remote TCP+TLS+auth handshake while concurrent handshakes exhausted `max_size=8` with `checked_out=2`.
  - *Alternatives Considered*: Increasing `max_size` beyond 8 — rejected because it violates hosted PostgreSQL connection limits and does not fix connection closing on context exit.

- **Decision 2: Indexed 20-Minute Window for Eligible Stream Discovery & Single-Select Batch Ingest**
  - *Chosen Approach*: Add an index on `(interval_minutes, first_observed_at)` and push the existing `now - timedelta(minutes=20)` freshness bound into the SQL `WHERE` clause of `_eligible_streams`; in `ingest()`, fetch existing bars for the batch range in one query before inserting new bars.
  - *Why*: Scanning all historical rows in `perp_market_candles` for `GROUP BY venue, contract, quote` held a connection for `6.59s` at startup, and executing 95–1,000 per-candle `INSERT` + `SELECT` statements over the network held connections for up to `29.1s`.
  - *Alternatives Considered*: Caching stream lists in memory — rejected so database state remains the single source of truth across restarts.

- **Decision 3: Short-Circuit Non-Watch Venues in Background `decision_only` Signal Screening**
  - *Chosen Approach*: When `decision_only=True`, filter candidate venues to those whose forecast is a `"directional watch"` (`>= 0.56` or `<= 0.44`), verify `_venue_proof` (`model_beats_baseline is True`), and only then request live order books; also use per-`(venue, asset)` locks in `PublicBestQuoteClient.get_venue` so concurrent assets do not serialize behind a single venue lock.
  - *Why*: Background Auto-Hunt only emits a `QUALIFIED WATCH` when a venue has a directional watch, beats baseline, and has a valid live book. Fetching OKX/Bitget REST tickers for weak (`0.51`–`0.55`) forecasts caused 7-second timeouts at `signals.py:291`.
  - *Alternatives Considered*: Raising the 7-second timeout — rejected because stalling the Auto-Hunt cycle delays alert delivery and causes quotes to age past validity.

---

### 4. Technical Architecture & Data Strategy *(Technical Reference)*

```
┌──────────────────────────────────────────────────────────────────────────┐
│                        Auto-Hunt Background Cycle                        │
│  1. Read live / multi watches per asset                                  │
│  2. decision_only=True: filter to directional watches (>=56% / <=44%)    │
│  3. Check venue 7d baseline proof -> fetch live book only if qualified   │
└───────────────────┬──────────────────────────────────┬───────────────────┘
                    │                                  │
                    ▼                                  ▼
┌───────────────────────────────────────┐  ┌───────────────────────────────┐
│      PostgreSQL Connection Pool       │  │    PublicBestQuoteClient      │
│  • Commit/rollback without closing    │  │  • Per-(venue, asset) locks   │
│    underlying psycopg socket          │  │  • Shared venue block timers  │
│  • Pop _borrowers before _idle put    │  │  • 3s TTL quote cache         │
└───────────────────┬───────────────────┘  └───────────────────────────────┘
                    │
                    ▼
┌───────────────────────────────────────┐
│     PerpetualMarketStore & Shadow     │
│  • Range SELECT once per ingest batch │
│  • Indexed 20m window on              │
│    (interval_minutes,first_observed)  │
└───────────────────────────────────────┘
```

- **Data Model & State**:
  - Adds non-destructive index `idx_perp_market_candles_observed` on `perp_market_candles (interval_minutes, first_observed_at)`.
  - Preserves all existing tables, rows, and 8-connection pool ceiling.
- **Interactive Component & State Mapping**:
  - User-facing `/rival` and `/rival-signal` embeds (`decision_only=False`) continue to render full fallback books, track records, and shadow reports unchanged.
  - Background `decision_only=True` evaluations return immediately when no venue has a directional watch or baseline-beating proof.
