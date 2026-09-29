# RivaL

**Know the line. Build the slip.**

RivaL is a provider-agnostic sports-odds intelligence and parlay-analysis project. It keeps sportsbook coverage, line comparison, historical movement, jurisdiction filtering, and parlay construction behind a simple Discord experience.

## Current architecture

```text
SportsGameOdds         OddsPapi
 current/today       history/backfill
      |                    |
      +---------+----------+
                v
         provider adapters
          |
          v
 canonical normalization
          |
   +------+-------------------+
   |                          |
   v                          v
SQLite hot store      Google Drive cold warehouse
   |                  history / models / backups
   |                          |
   +------------+-------------+
                |
                v
       market-consensus layer
                |
                v
        parlay construction
                |
                v
          Discord /rival
```

Odds are stored once per real sportsbook selection rather than duplicated per state. Jurisdiction metadata filters which books a user can actually act on.

## Implemented

- SportsGameOdds V2 current/today odds and player-prop feed.
- Shared quota-aware current-board refresh instead of per-user polling.
- OddsPapi v4 account entitlement parsing.
- Quota-aware OddsPapi fixtures, odds, historical odds, and market-catalog access.
- Provider-specific cooldown enforcement and billable-request reserve.
- Canonical sportsbook selection identity.
- Current OddsPapi board normalization.
- SQLite hot storage for fixtures/current prices.
- Meaningful-state line history with duplicate-state compaction.
- Stale provider-state protection.
- ZIP/state onboarding with state-only persistence.
- Verified sportsbook filtering for the user's selected jurisdiction.
- Private Discord `/rival` dashboard.
- Market/outcome label catalog.
- No-vig implied-probability normalization.
- Multi-book median market-consensus probability.
- Same-book parlay construction.
- Line-aware consensus so different spreads/totals are never silently combined.
- Lower / Balanced / Aggressive / Longshot risk modes.
- One-leg-per-fixture correlation guard.
- Freshness and started-fixture guards.
- Discord `Build Parlay`, `Make Safer`, and `Boost Payout` controls.
- Dedicated Google Drive `RivaL Data Warehouse` boundary.
- Python 3.11 CI with Ruff and pytest.

## Parlay V1

RivaL's first parlay engine is deliberately **market-derived**, not presented as a trained prediction model.

For each market, RivaL removes the sportsbook's vig from the available outcome prices, compares the same logical outcome across eligible books, de-duplicates identical probability vectors, and uses the median no-vig probability as its market-consensus estimate.

A returned parlay:

- uses one sportsbook only;
- uses active main lines only;
- ignores stale prices;
- ignores already-started fixtures;
- uses at most one leg per fixture until a proper same-game correlation model exists.

The Discord result explicitly labels these values as market-consensus estimates rather than guaranteed outcomes or model predictions.

## Discord surface

RivaL exposes one application command:

```text
/rival
```

New users can enter a ZIP or state. If a ZIP is used, only the resolved two-letter state is persisted; the ZIP itself is discarded. Returning users see their saved jurisdiction, verified sportsbook options, and the parlay builder. Build/rebuild actions refresh the shared SportsGameOdds cache only when its configured interval has expired, then reuse that cache across users.

The Discord client does not request message-content intent.

Set `DISCORD_TOKEN` in secret storage. `RIVAL_DEV_GUILD_ID` is optional for development-guild command sync.

## Google Drive

Historical archives and future model artifacts live under one dedicated Drive root:

```text
RivaL Data Warehouse/
├── history/
├── manifests/
├── models/
├── predictions/
├── backups/
├── reports/
└── staging/
```

RivaL is forbidden from enumerating or modifying Drive content outside that root. Real Drive account details and folder IDs remain private runtime configuration and are not committed.

See `docs/GOOGLE_DRIVE_BOUNDARY.md`.

## Secrets and generated data

Never commit API keys, Discord tokens, OAuth credentials, real Drive folder IDs, downloaded odds payloads, databases, or archive files.

Copy `.env.example` to a local `.env` and inject real credentials through deployment secret/config storage. The default hot database path is `data/rival.sqlite`, which is ignored by Git.

## Development

```bash
python -m pip install -e ".[dev]"
ruff check .
pytest -q
```

See `ACTIVE_TASK.md` for the exact active task, validation evidence, and next milestone.
