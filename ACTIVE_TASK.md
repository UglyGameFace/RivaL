# Active Task Record

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
