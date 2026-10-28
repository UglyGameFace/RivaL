# RivaL Active Task Record

## Active task / outcome

Convert RivaL's existing sports-only Discord parlay application into an **automatically evaluated and retrained, read-only crypto perpetual-futures prediction bot**, using verified no-cost public market-data APIs and the existing authoritative Discloud PostgreSQL runtime.

Source request: GitHub issue #6. Start from `main@fb28a85523891c8251d8c2e5814459817f4c931c` after verified completion of PostgreSQL cutover.

## Scope

- Inspect current `/rival` entry point, sports collector, provider adapters, normalization, persistence, jurisdiction/onboarding, parlay UI, archive/runtime, tests, and deployment dependencies before replacing behavior.
- Confirm supported public derivatives data endpoints, limitations, legal/geographical access restrictions, rate limits, venue symbol and contract identity, funding, open interest, and historical retention. Do not assume source interchangeability or combine venue-specific prices as if identical.
- Keep PostgreSQL authoritative and Drive as optional, separately authenticated cold storage. Preserve existing user data, legacy compatibility until explicitly migrated, and safe rollback.
- Build point-in-time candles/feature/label datasets, horizon-specific probability predictions, walk-forward evaluation, calibration, outcome settlement, autonomous model-training triggers, model version/rollback, quality/drift monitoring, and visible user-facing metrics.
- Preserve Discord private application-command workflows while replacing sports-specific screens with perp prediction screens. Do not enable actual orders, exchange credentials, leverage, or unattended trading.
- Free-first, Android-manageable, Discloud-friendly, low-resource and rate-limit-safe. Do not claim predictive edge without tested out-of-sample evidence.

## Status

**Active: foundational prediction/feedback slice merged; further conversion work remains.** PR #9 merged at `78dbbc18d0dfc0c31902e727fe9736a5834498e7`. Exact-head pull-request CI passed Ruff and 75 tests (including PostgreSQL 16 integration). The sports app remains the live production behavior; the new modules are isolated, not scheduled, not fed by exchange APIs, and not exposed in Discord. No claims of trained predictions or demonstrated profitability.

## October 28, 2026: bounded market-data ingestion slice

- Active task remains the entire perpetuals conversion, not a separate feature.
- First official no-key source: Hyperliquid POST Info (metaAndAssetCtxs and candleSnapshot).
- Isolated market adapter, immutable shared-store candle/context tables, owner-invoked
  public probe and explicitly opt-in PostgreSQL ingestion, mock-HTTP regression cases,
  plus PostgreSQL 16 compatibility test. No automatic polling, Discord changes,
  sportsbook code edits, trade execution, or exchange credentials.
- Data provenance: source/contract/USDC quote, interval, UTC candle boundaries,
  observed-at timestamp, base-asset volume/OI, mark/funding snapshot.
- Fail closed on 403/429, schema discrepancies, future/forming bars and changed
  historical candles. Limits: 10 assets, 200 bars/request, no source retries.
- Live regional access from Discloud and supervised backfill are **not yet verified**;
  test fixtures cannot establish that. Avoid model-training claims.
- Immediate next step: run exact-head Ruff/pytest with PostgreSQL 16, inspect the
  entire PR diff, merge only when green, and validate the no-key probe from
  Discloud before automatic ingestion or forecasts.

## Findings / authoritative execution path

`main.py` -> `parlay_bot.discord_app.bot.run_discord_bot` ->
`RivalDiscordClient` -> shared `RelationalHotStore` / `PostgresHotStore` ->
`LocationOnboardingService`, `CurrentBoardCollector`,
`ParlayBuilder`, and `ColdArchiveRuntime` -> `/rival` and views.

As of 2026-10-27, Kraken's public Futures charts documentation offers no-key chart candles; Hyperliquid's public Info endpoint offers candle snapshots and asset contexts (funding/open interest); Bybit V5 offers no-key historical klines, funding and open-interest data but exchange availability varies by location. Provider selection requires endpoint and regional-validation checks before integration. Historical retention, websocket consistency, and missing fields must be handled explicitly.

## Changes / validation / cleanup / conflicts

- Added the first isolated conversion code on `feat/rival-perps-forecast-ledger`: source-provenance-aware probability fusion (`perpetuals/fusion.py`) and an idempotent, transactional forecast/outcome ledger (`perpetuals/ledger.py`) using the existing relational database. Added local SQLite unit tests and PostgreSQL integration coverage plus `docs/PERPETUALS_LEARNING.md`. The runtime entry point is intentionally unchanged.
- Existing PostgreSQL/Discord runtime on `main` remains unchanged.
- Backlog unrelated requests; retain one active implementation task, including verification and cleanup.
- The previous PostgreSQL cutover has passed GitHub CI and a user-observed Discloud restart/persistence test, recorded below.
- No live exchange access or model quality can be claimed from docs alone.

## Blockers / risks

- Exchange/public API availability from the actual Discloud region is not yet verified.
- A statistically useful training dataset must first be collected/backfilled, with no future leakage. Data scarcity may require a conservative baseline and a shadow model.
- Google Drive OAuth refresh/token lifecycle and archive uploads remain **separate unverified functionality**. Leave archiving off until authenticated validation; do not confuse Drive readiness with the PostgreSQL cutover.
- Trading access and legal eligibility vary by region; read-only analytics must not circumvent access restrictions.

## Next step

The first isolated ledger/fusion slice was merged after successful exact-head CI and focused diff review. Next, verify which permitted public perpetuals market-data endpoints are reachable from Discloud; implement bounded, normalized, independently traced read-only ingestion and point-in-time features into the existing PostgreSQL store, then connect genuine forecasts/settlements to the tested ledger. Integrate model training and Discord UI only after evaluation gates. Do not claim live predictions or switch the active task prematurely.

---

# Completed prior task: Discloud PostgreSQL production cutover

## Closure evidence (2026-10-27)

- PR #5 merged the shared PostgreSQL implementation, with PostgreSQL integration regression tests passing.
- PR #7 added a secret-safe startup backend log; exact-head push/PR GitHub Actions CI passed, as did the merged `main@fb28a85523891c8251d8c2e5814459817f4c931c` run.
- Owner-supplied Discloud startup logs at **22:58 local** show `RivaL operational storage initialized (backend=postgres)`, one command synchronized to guild `1514374173517152418`, and a Discord Gateway connection.
- A freshly invoked `/rival` at **23:01 local**, after that process restart, directly displayed the previously saved Connecticut (CT) selection without reentry. This verifies persistence across the deployment restart through the currently configured PostgreSQL runtime.
- The `403 Missing Access` from 22:41 was resolved by installing the Discord app in the server, not by altering the guild ID.
- No production DB URL or credentials were exposed. Cold archive uploads, SportsGameOdds live refresh and perpetuals models are *not* implied verified by these checks.

Prior task scope, findings, original test and compatibility results are archived below as historical evidence. References to outstanding production verification there describe the **pre-closure snapshot**, not current blockers.

# Historical PostgreSQL Cutover Record (pre-closure snapshot)

## Project

RivaL

## Repository boundary

RivaL development belongs only in `UglyGameFace/RivaL`.

Do not write RivaL code, workflows, task records, branches, or pull requests to
`UglyGameFace/sports-parlay-bot`.

## Active task / outcome

Replace RivaL's production SQLite hot/state database with a Discloud-managed PostgreSQL database
without changing the user-facing odds, jurisdiction, parlay, or archive behavior.

Branch: `feat/discloud-postgres-storage`

Production baseline: `main@46986f7aada639b649a01e976c082dcaa2e82d9a`

## Scope

PostgreSQL must become the single authoritative production store for:

- fixtures and current sportsbook prices;
- meaningful line-state history;
- saved state/jurisdiction selections;
- shared SportsGameOdds refresh gating;
- OddsPapi market/outcome catalog metadata;
- Google Drive cold-archive batch bookkeeping and source-row archive markers.

SQLite is retained only as an explicit local/test compatibility backend.

## Status

Implementation is complete on the feature branch and the runtime/storage changes are validated in
CI. Production activation still requires the real Discloud PostgreSQL instance and its private
connection URL to be supplied through deployment configuration.

## Findings / root cause

RivaL previously had several direct SQLite owners rather than one production database boundary:
`SQLiteHotStore`, `MarketCatalogStore`, and `SQLiteArchiveQueue` each opened the local database
independently. The Discord runtime also constructed them separately.

A simple connection-string replacement would therefore have left part of the application on local
SQLite.

SportsGameOdds also uses string provider identifiers such as `BASKETBALL` and `NBA`. SQLite
accepted those values in columns declared `INTEGER` because of its dynamic typing. PostgreSQL would
reject that schema, so provider ID columns must be text-compatible.

## Execution path

```text
Discord /rival
    |
    v
RivalDiscordClient
    |
    +--> shared RelationalHotStore
           |
           +--> fixtures/current odds/line history
           +--> jurisdiction state
           +--> refresh runtime state
           +--> market catalog
           +--> cold archive queue/batch state
    |
    v
Discloud PostgreSQL
```

Closed historical line states can still flow from PostgreSQL through the existing verified Parquet
and Google Drive archive path. Drive remains cold storage, not the operational database.

## Changes on branch

- Added a shared relational database adapter with SQLite and PostgreSQL implementations.
- Added PostgreSQL hot-store support using psycopg.
- Made production storage default to `postgres`.
- Added `RIVAL_DATABASE_URL` and an explicit `RIVAL_STORAGE_BACKEND`.
- Made missing PostgreSQL configuration fail clearly instead of falling back to SQLite.
- Moved market catalog access onto the active relational store.
- Moved cold-archive queue bookkeeping onto the active relational store.
- Generalized collector, onboarding, and parlay-builder storage typing.
- Preserved SQLite compatibility for local tests and existing SQLite-focused regression tests.
- Added the PostgreSQL runtime dependency to both Python packaging and Discloud requirements.
- Enabled Discloud VLAN configuration for the bot.
- Added a PostgreSQL 16 service to CI.
- Added PostgreSQL integration coverage for SportsGameOdds IDs, current/history writes, runtime
  state, jurisdiction state, market catalog data, and archive completion ordering.

## Compatibility review

The public Discord surface remains `/rival`. Existing market-consensus rules, sportsbook
eligibility logic, refresh cadence, one-leg-per-fixture policy, Drive archive verification, and ZIP
privacy behavior are intentionally unchanged.

No automatic SQLite-to-PostgreSQL data copy is performed by runtime startup. Production must not
silently import or discard an unknown legacy database. If a real legacy database needs migration,
that is a controlled deployment operation using an inspected source file.

## Validation / results

Validated implementation head:
`897da83566f9684d1d52166e38f1c57e8a395d38`

GitHub Actions CI run #161 passed on the PR merge tree:

- PostgreSQL 16 service initialized and became healthy.
- Runtime dependencies, including psycopg, installed successfully.
- Ruff: passed.
- pytest: **65 passed, 1 dependency deprecation warning**.
- PostgreSQL integration tests cover SportsGameOdds string IDs, current/history writes, runtime
  state, jurisdiction state, market metadata, archive transaction ordering, and an end-to-end
  three-leg parlay build through PostgreSQL.
- Existing SQLite compatibility/regression tests still pass.
- Archive tests pass with the shared relational store.
- Discord client construction tests cover both production PostgreSQL requirements and explicit
  SQLite test mode.

Final diff inspection:

- branch is ahead of the intended production baseline and not behind it;
- no conflict markers;
- no private-key material or Discord-token-like values;
- no real database URL or credential committed;
- no generated database/archive artifacts;
- no unrelated project files;
- SQLite references in production code are restricted to the explicit local/test compatibility
  path.

## Cleanup / conflicts

The affected storage area is consolidated behind the relational store. The stale catalog filesystem
assumption and redundant PostgreSQL archive wrapper found during validation were removed.

The SQLite archive wrapper remains only for existing local/test compatibility. Production runtime
constructs one authoritative store and passes it to hot odds, market catalog, jurisdiction, refresh
state, parlay queries, and archive bookkeeping.

## Blockers / risks

- A real Discloud PostgreSQL instance and its private connection URL are deployment configuration,
  not repository data.
- Production activation cannot be validated against the user's actual Discloud database until that
  database exists and its URL is supplied through deployment environment configuration.
- No legacy SQLite data migration should be attempted without first confirming that the deployed
  SQLite file contains data worth preserving.

## Backlog

The requested self-learning crypto perpetual-futures conversion is recorded in GitHub issue #6.
Do not begin it until this cutover is verified or the owner explicitly force-switches tasks.

## October 27, 2026 post-merge production observations

- PR #5 was merged into `main` on October 6, and the merge-head GitHub CI passed.
- Discloud logs provided by the owner show that RivaL synced one Discord command to guild
  `1514374173517152418` and connected to Discord Gateway after the bot was installed.
- The owner displayed the private `/rival` dashboard with Connecticut selected.
  Code inspection confirms this is backed by `LocationOnboardingService` and the selected
  relational store; it proves a runtime write/read path, not restart persistence.
- Discloud backend selection, connection URL provenance, and restart persistence are not yet
  independently verified. A visible Discord connection alone is insufficient evidence.
- This focused follow-up adds a non-secret startup log recording the actual instantiated
  database backend after successful initialization. It does not alter storage, authentication,
  migrations, command semantics, or archive behavior.

## Next step

Once the startup-log PR is validated and deployed, inspect the Discloud log for
`RivaL operational storage initialized (backend=postgres)`.
Then restart the bot and invoke `/rival` again using the same Discord user to verify the
saved Connecticut location remains present without re-entering it. Record safe evidence only;
never copy database URLs, passwords, Discord tokens, or OAuth refresh tokens into logs.
The PostgreSQL cutover remains active until those production checks pass.
