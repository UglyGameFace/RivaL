# RivaL

**Know the line. Build the slip.**

RivaL is a provider-agnostic sports-odds intelligence and parlay-analysis project. It keeps sportsbook coverage, line comparison, historical movement, jurisdiction filtering, and parlay construction behind a simple Discord experience.


### Start in Discord

Type **`/rival`**. Everything ordinary users need is in one private menu:

- **Sports & Odds:** choose a state, compare game prices and build a fictional
  three-pick slip. Nothing is placed.
- **Predict Crypto:** select BTC, ETH or SOL, choose 5m/15m, then **Get Signal**.
  Expect UP/DOWN research watches only when the evidence is fresh and strong;
  otherwise **WAIT** is a correct answer. Follow in DM or set an optional price goal.
- **Practice Money:** fake buy/sell/close trades using current exchange quotes.
- **My Results:** personal fictional trade outcomes, plus a plain-English link
  to **Can I Trust Predictions?**
- **Advanced (optional):** **Are My Prices Fresh?**, **Is RivaL Improving?**,
  and the existing technical research dashboard.

Only **four top-level Discord commands** are registered:
`/rival` (the menu), `/rival-signal` (quick crypto lookup),
`/rival-stop` (cancel alerts), and `/rival-tools` (optional old/technical
shortcuts). All twenty existing power-user actions are grouped under
`/rival-tools <feature>` without deleting functionality or records.
Examples: `/rival-tools data`, `/rival-tools source-health`,
`/rival-tools multi-proof`, and `/rival-tools profit-proof`.
The group is optional and not part of beginner onboarding.

### What RivaL actually verifies

RivaL collects up to 10 public perpetual-exchange five-minute candle feeds
(BTC/ETH/SOL) and has two current best-book WebSocket integrations
(Binance USD-M, Hyperliquid). Region blocks and provider outages mean
configured is **not** the same as proven connected.
The ten-source combined model is a **shadow challenger** and does not
automatically replace the original model. Its forward predictions are scored
only against later observed market results and compared on identical target
times. Quote-based hypothetical returns include spread, simulated liquidity
and conservative/severe cost assumptions, never authenticated real fills.

For detail, see [profitability acceptance and source limitations](docs/RIVAL_PROFIT_PROOF.md).
No reliable profitable edge, guaranteed wager, or real-money execution is claimed.

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
Discloud PostgreSQL   Google Drive cold warehouse
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
- Discloud PostgreSQL hot storage for fixtures/current prices, line history, user state, refresh gates, market metadata, and archive bookkeeping.
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
- ZSTD Parquet cold archive for closed line states.
- SHA-256/MD5 archive integrity manifests.
- Resumable Drive staging -> verification -> history promotion.
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

## Simple RivaL Home (mobile and first-time users)

Start with `/rival`; use the buttons, no manual setup commands required.
The Home, Back, and Explain This buttons stay on every page.
Crypto signals, personal DM alerts, fictional practice, sports picks and
basic reliability checks are private, separately navigable flows.

Advanced is optional. It explains whether recent exchange candles are
available and whether actual future combined-model scores have improved.
It never equates historical backtests, fictional practice wins or
successful deployments with real-money profitability.

For developers and expert users, `/rival-tools` groups the old shortcuts.
The underlying quote, model, analysis, target, sports and database services
remain functional; only their location in Discord's slash-command menu changed.

## Discord commands

| Command | Who it's for | What it does |
| --- | --- | --- |
| `/rival` | Everyone | Opens the main private menu for sports, crypto, alerts, practice, results and Help |
| `/rival-signal` | Optional shortcut | Checks a coin/timeframe and explains the directional research result or WAIT |
| `/rival-stop` | Everyone | Immediately cancels the user's own private price/forecast alerts |
| `/rival-tools` | Experienced users | Optional group of twenty old specialist actions, not required for normal use |

No real wagers or orders are submitted. Existing alert preferences,
historical results and practice balances survive the command migration.
Discord updates global command menus after the bot successfully syncs;
a GitHub green check or zip upload alone doesn't prove the running Discord
command list has changed.

[Verified quotes, heldout proof, source entitlements and paper accounting](docs/EVIDENCE_QUOTE_PAPER_GATES.md)
and [price-target evidence and provider coverage](docs/RIVAL_PRICE_TARGETS.md)
document the exact source/time-window rules.

The status is never a placed order or promise of profitable returns. The
model's 15m horizon predicts the *future five-minute target candle*, not
an entire 15-minute target candle; both horizons expire at that target
candle's actual close. A stale feed or unavailable prediction reads WAIT.

First-time sports users enter their ZIP or state; only the resolved two-letter
state is persisted. Price comparisons always use the shared provider cache,
and are rejected when stale. No message-content intent is requested.

Set `DISCORD_TOKEN` in Discloud secrets. `RIVAL_DEV_GUILD_ID` optionally
limits slash-command sync to a development guild. Deployment from passing
GitHub `main` CI can use the **official Discloud Action** when the owner has
configured the repository secrets `DISCLOUD_TOKEN` and `DISCLOUD_APP_ID`.
See [Discloud auto-deploy](docs/DISCLOUD_AUTODEPLOY.md) for setup and
**required live/restart validation**. A GitHub merge alone is not a live deployment.

## Perpetuals warehouse

The existing Google Drive archive originally covered **sports line changes only**.
An independent, opt-in copy-only warehouse now supports immutable 5-minute
perpetual candles, preserving venue, contract, actual observation timestamps
and historical provenance. It uses the same private warehouse, ZSTD Parquet,
verified Drive history/manifest workflow, and the existing hourly archive loop.
The last 14 days stay hot by default; **all** hot rows remain present until
a separate, tested restore/read-path and retention migration exist.
It is **not live** until the bot's separate Google OAuth configuration is
verified and `RIVAL_ARCHIVE_ENABLED` plus `RIVAL_PERP_ARCHIVE_ENABLED`
are deliberately enabled on the deployed host. See
`docs/GOOGLE_DRIVE_BOUNDARY.md`.

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

Closed line states are written as explicit-schema ZSTD Parquet, uploaded to `staging/`, verified by size and MD5, promoted to `history/`, then paired with a JSON manifest. Source PostgreSQL rows are marked archived only after both remote objects are verified. The latest mutable state of every market selection is never archive-eligible.

See `docs/GOOGLE_DRIVE_BOUNDARY.md`.

## Secrets and generated data

Never commit API keys, Discord tokens, OAuth credentials, real Drive folder IDs, downloaded odds payloads, databases, or archive files.

Production storage defaults to PostgreSQL. Set `RIVAL_DATABASE_URL` to the private PostgreSQL connection URL supplied for RivaL's Discloud database and keep `RIVAL_STORAGE_BACKEND=postgres`. The bot fails startup if PostgreSQL is selected without a database URL rather than silently writing to a local file.

SQLite remains available only as an explicit local/test compatibility backend with `RIVAL_STORAGE_BACKEND=sqlite` and `RIVAL_DB_PATH=data/rival.sqlite`. Generated local databases remain ignored by Git.

For the Discloud deployment steps and the database boundary, see `docs/DISCLOUD_POSTGRES.md`.

## Development

```bash
python -m pip install -e ".[dev]"
ruff check .
pytest -q
```

See `ACTIVE_TASK.md` for the exact active task, validation evidence, and next milestone.

## Verified free historical research intake

A separate, bounded, checksum-verified research importer supports Binance USD-M
monthly futures archives (multiple horizons) and historical football results/
bookmaker odds, plus descriptive no-vig pricing benchmarks. It does not change
live predictions or place trades. Run locally on a network-enabled research
host and preserve artifacts inside the authorized RivaL warehouse after
verified promotion. See [the importer guide](docs/VERIFIED_FREE_HISTORY.md).
