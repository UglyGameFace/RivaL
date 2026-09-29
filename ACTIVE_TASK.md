# Active Task Record

## Project

RivaL

## Repository boundary

RivaL development belongs only in `UglyGameFace/RivaL`.

Do not write RivaL code, workflows, task records, branches, or pull requests to
`UglyGameFace/sports-parlay-bot`.

## Completed milestones

- Provider-neutral odds foundation and quota-aware OddsPapi integration.
- Historical OddsPapi access with ETags and state compaction.
- Canonical odds normalization and SQLite hot storage.
- ZIP/state onboarding with ZIP discarded after state resolution.
- Verified sportsbook filtering.
- Private Discord `/rival` dashboard.
- Market-consensus parlay engine V1:
  - no-vig probabilities
  - same-book executable slips
  - risk modes
  - one-leg-per-fixture correlation guard
  - freshness/start-time filtering
  - Build Parlay / Make Safer / Boost Payout
- RivaL-only Google Drive cold-warehouse folder tree and hard access boundary.

Parlay engine V1 was merged as commit
`e44534d43ee8b81499a73df7bb11e80f4d80598c`.
The validated feature tree and squash-merge tree were identical.

## Google Drive boundary

The authorized connected Google account contains one dedicated root:

`RivaL Data Warehouse/`

with:

- `history/`
- `manifests/`
- `models/`
- `predictions/`
- `backups/`
- `reports/`
- `staging/`

Real account details and real folder IDs are private runtime configuration and are not committed.
RivaL may operate only inside this root. See `docs/GOOGLE_DRIVE_BOUNDARY.md`.

## Active task / outcome

Add SportsGameOdds as RivaL's quota-efficient **current/today odds feed**, while keeping
OddsPapi for historical/backfill and provider-account functions.

Branch: `feat/sportsgameodds-current-feed`

### Implemented on branch

- SportsGameOdds V2 client using `x-api-key` header authentication.
- Usage/quota parser and configurable monthly entity reserve.
- Event-limit trimming so a request cannot intentionally consume the reserve.
- Rate-limit handling and event-request pacing.
- Current event filters:
  - selected leagues
  - selected books
  - odds available
  - not started
  - not cancelled
  - alternate lines excluded for the V1 current feed
- Canonical SportsGameOdds event/odds normalization.
- American-to-decimal price conversion.
- Team/event/player metadata extraction.
- Player-prop identity and line preservation.
- Spread/total line normalization.
- Canonical paired market identity using opposing odds.
- Actual sportsbook deeplink preservation when supplied.
- Extended hot-store schema for market names, outcome names, line values, line-group values, and deeplinks.
- Safe in-place SQLite column migration for existing RivaL databases.
- Line movement included in the meaningful-state fingerprint.
- Persistent runtime-state table for collector cadence.
- Shared current-board collector with an async lock.
- Refresh cadence persists across bot restarts.
- Default current MVP scope:
  - NBA
  - NFL
  - DraftKings
  - FanDuel
  - one page / up to 25 events per refresh
  - 10-minute shared refresh interval
- Discord Build Parlay and risk rebuilds call refresh-if-due before using cache.
- Provider failures fall back only to cache; the builder still refuses stale prices.
- Parlay consensus is line-aware.
- At least two reference probability samples are required for a consensus leg.
- Different spread/total lines are not compared as if they were the same wager.

## Current data architecture

```text
SportsGameOdds
 current/today board
       |
       v
shared 10-minute refresh gate
       |
       v
canonical normalization
       |
       v
SQLite hot store
       |
       +------> Discord /rival Build Parlay
       |
       v
market-consensus engine

OddsPapi
 historical/backfill
       |
       v
history/archive pipeline
       |
       v
Google Drive cold warehouse
```

A Discord button press does not equal an API request. The current board is shared across users.

## Data-integrity rules

- API keys never appear in request query strings or Git.
- Current odds are rejected when stale.
- Already-started fixtures are rejected.
- Alternate lines are excluded from the initial live collector.
- Spread consensus compares the same absolute spread only.
- Total consensus compares the same total only.
- A returned parlay remains one-book only.
- Same-game multiple legs remain disabled until correlation is explicitly modeled.
- RivaL does not represent market consensus as guaranteed outcomes or a trained prediction model.

## Validation status

Feature head `0fa5ed1d0a1dd5cd787ccdbe4b20a6b743ed03c9` passed GitHub Actions push CI:

- install passed on Python 3.11
- Ruff passed
- pytest: **48 passed, 1 warning**

Exact-head PR merge-ref CI and final security/diff review are still required before merge.

## Next step

Open draft PR #2, require green merge-ref CI, scan the complete diff for secrets/unrelated
changes, then squash-merge only if the validated tree is clean.
