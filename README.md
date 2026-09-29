# RivaL

**Know the line. Build the slip.**

RivaL is a provider-agnostic sports-odds intelligence and parlay-analysis project. It is being built so the complicated parts — sportsbook coverage, line comparison, historical movement, probability modeling, and jurisdiction filtering — stay behind a simple user experience.

## Current architecture

```text
Odds providers
     |
     v
provider adapters
     |
     v
canonical normalization
     |
 +---+----------------+
 |                    |
 v                    v
SQLite hot store    historical state stream
 |                    |
 v                    v
current board       compact archive partitions
 |                    |
 +---------+----------+
           |
           v
     future model layer
           |
           v
 Discord first / other clients later
```

Jurisdiction data filters which sportsbooks a user can act on. Odds are stored once per real market selection rather than duplicated for every state. A ZIP can be used to determine the state during onboarding, but RivaL persists only the state code and discards the ZIP.

## Implemented

- OddsPapi v4 account entitlement parsing.
- Quota-aware current fixture and odds access.
- Historical odds access with ETag support.
- Provider-specific cooldown enforcement.
- Configurable billable-request reserve.
- Canonical sportsbook selection identity.
- Current OddsPapi board normalization.
- SQLite hot storage for fixtures and current prices.
- Meaningful-state line history with repeated-snapshot compaction.
- Protection against stale provider updates overwriting newer current prices.
- Jurisdiction-ready sportsbook availability model.
- ZIP or state onboarding with state-only persistence.
- Verified sportsbook filtering for the user's selected jurisdiction.
- Private Discord `/rival` dashboard with ZIP/state onboarding.
- Python 3.11 CI with Ruff and pytest.

## Discord surface

RivaL currently exposes one application command:

```text
/rival
```

It opens an ephemeral dashboard. New users can enter a ZIP or state; returning users see their saved jurisdiction and verified sportsbook options. The Discord client does not request message-content intent.

Set `DISCORD_TOKEN` in secret storage. `RIVAL_DEV_GUILD_ID` is optional and can be used for immediate development-guild command sync; omit it for global sync.

## Secrets and generated data

Never commit API keys, Discord tokens, OAuth credentials, downloaded odds payloads, databases, or archive files.

Copy `.env.example` to a local `.env` and inject real credentials through the deployment platform's secret manager. The default hot database path is `data/rival.sqlite`, which is ignored by Git.

## Development

```bash
python -m pip install -e ".[dev]"
ruff check .
pytest -q
```

See `ACTIVE_TASK.md` for the exact task, validation evidence, blockers, and next step.
