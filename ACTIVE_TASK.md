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

Implementation is in progress. Core storage, runtime wiring, deployment configuration, and
PostgreSQL integration tests are on the feature branch. Validation and final cleanup are not yet
complete.

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

Pending exact-head CI.

Required before completion:

- Ruff passes.
- Existing SQLite regression suite passes.
- New PostgreSQL integration tests pass against PostgreSQL 16.
- Archive tests pass with the shared store.
- Discord client construction tests cover both production PostgreSQL requirements and explicit
  SQLite test mode.
- Final diff contains no credentials, generated databases, conflict markers, or unrelated changes.
- Branch remains based only on the intended RivaL production baseline.

## Cleanup / conflicts

The affected storage area is being consolidated so production code does not keep competing SQLite
and PostgreSQL implementations for the same state.

Compatibility wrappers are allowed only where existing tests or local development need SQLite.
They must not become a second production authority.

## Blockers / risks

- A real Discloud PostgreSQL instance and its private connection URL are deployment configuration,
  not repository data.
- Production activation cannot be validated against the user's actual Discloud database until that
  database exists and its URL is supplied through deployment environment configuration.
- No legacy SQLite data migration should be attempted without first confirming that the deployed
  SQLite file contains data worth preserving.

## Backlog

No unrelated work is active.

## Next step

Run exact-head CI, inspect failures if any, clean the affected storage area, review the final diff,
then open the focused pull request only after validation evidence is green.
