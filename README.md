# RivaL

**Know the line. Build the slip.**

RivaL is a provider-agnostic sports-odds intelligence and parlay-analysis project. It keeps sportsbook coverage, line comparison, historical movement, jurisdiction filtering, and parlay construction behind a simple Discord experience.


### Prospective quote-based profitability proof

- `/rival-profit-proof coin:BTC timeframe:5` reports the actual
  **forward-stamped, later-verified executable bid/ask observations** RivaL
  collected around pre-outcome predictions. Also tracks missed/late exits,
  quote coverage, losing trades, and conservative/severe fee, funding,
  and slippage assumptions. It is NOT a retrospective candle-close backtest.
- Observations are collected automatically during the existing 5m
  research cycle (up to BTC/ETH/SOL, single shared snapshot per asset),
  stored durably in PostgreSQL, and resumed idempotently after restart.
  Regional or quota restrictions fail closed without fabricated quotes.
- **Quote-based paper results are not authenticated brokerage fills.**
  Even passing stringent day-block after-cost tests cannot establish
  verified *actual* profitability without independent account receipts.
  See [profitability acceptance and source limitations](docs/RIVAL_PROFIT_PROOF.md).

### Funded practice simulator and verified sports models

- `/rival-practice` provides a **fictional, privately isolated funded balance**:
  10,000 USDT by default, configurable from 100 to 100,000 before your first
  practice position. Nothing is deposited or paid out.
- `/rival-paper-open`, `/rival-paper-close`, and `/rival-paper-results`
  now reserve notional from the fictional bankroll, release it at close,
  account for all quoted bid/ask losses and **assumed** fees/slippage, and
  enforce a maximum 5% of starting funds in realized daily practice losses.
  The account persists across restarts and cannot erase a losing track record
  by increasing its starting balance after a trade. Practice results are
  **not** independently verified actual fills or machine-learning labels.
- `/rival-sport-proof` shows sport-specific **soccer Poisson** or
  **basketball score-margin Gaussian** research with chronological outcome
  evaluation, only from provider-finalized games with stable team IDs.
  A low evidence count returns **NOT QUALIFIED**.
- Autonomous sports result auditing checks only previously discovered matches
  and enforces the provider's quota reserve. A pregame model prediction must
  be saved before kickoff; the actual final outcome is stored and settled
  later, never backfilled as a fake earlier prediction.
- Qualified independent *prospective* sports results can automatically earn
  or lose a **research-only** champion status. This never promotes unqualified
  code to real-money wagering or changes live inference weights.
- `/rival-health` exposes Discord command sync, process readiness, worker
  states and last persisted sportsbook refresh. A code upload or running
  worker is **not** the same as a verified external data feed.

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

Open `/rival`. It now opens a **private, button-driven home**, not
the technical sportsbook screen:

- **Sports & Odds**: Pick your state or ZIP once, compare fresh prices,
  and build a three-pick same-book parlay; adjust for lower risk or higher
  payout. A PASS is explained and no wager is placed.
- **Predict Crypto**: Choose Bitcoin (BTC), Ethereum (ETH), or Solana
  (SOL), choose 5 or 15 minutes, then tap See Forecast, Follow in DM,
  Set Price Target, See Buy/Sell Prices, or Stop My Alerts.
- **Practice Money**: Start with 10,000 **fictional** USDT or choose a
  different starting amount before your first trade. Use the ordinary
  Practice Buy/Practice Sell/Close Fake Trade buttons for a fixed
  25 USDT fake position subject to actual fresh quote and bankroll checks.
  **No deposit, no exchange account, no real trades.**
- **My Results**: Your individual practice balance, wins, losses and
  estimated after-cost fake P&L; the bot's independently scored market
  predictions and explicit unproven actual-profit status stay separate.
- **Help Me Start**: Beginner instructions with no quantitative jargon.
- **Advanced**: Historical data, models, trade-cost evaluation and
  original detailed commands are still present, not deleted.

**Home, Back and Explain This** are accessible throughout the beginner
menu. All controls are owner-checked and private, and the submenu edits
the current screen instead of sending an endless stream of new messages.
The advanced research button opens the already-existing research UI in
a separate private panel so the original home stays available. New
`/rival` screens require successful live Discord slash sync; a passing
test or Discloud upload alone cannot prove a user interaction worked.

Original `/rival-picks`, `/rival-research`, `/rival-data`,
`/rival-target`, and all other power-user commands remain available.
The simplified UI uses those authoritative services; it does not
replace prediction or storage infrastructure.

## Discord surface

RivaL is **opportunity-first**, and provides private, read-only decisions without
pretending there is a guaranteed profitable trade:

- `/rival`: private, guided beginner Home with Sports, Crypto, Practice, Results, Help and Advanced buttons.
- `/rival-picks`: direct power-user jurisdiction-eligible sports odds comparison, or PASS.
- `/rival-opportunities`: sportsbook opportunity plus BTC/ETH/SOL model watches.
- `/rival-target`: choose BTC/ETH/SOL and a 5m/15m timeframe. Leave the
  optional **target_price** empty for the existing private UP/DOWN/WAIT forward
  forecast; enter an exact **USDT target price** for a separate completed-candle
  **WATCH / HIT / EXPIRED / WAIT** price-touch card. This does not pretend
  model direction equals the probability of hitting your exact price.
  Both modes edit the same private DM on new verified evidence.
- `/rival-sports`: discover actual supported sports/leagues from fresh,
  unstarted sportsbook fixtures in the configured provider feed. No fictional
  league coverage or unsupported sport-specific model predictions.
- `/rival-leagues`: show the sports and leagues the current
  SportsGameOdds API key really has access to, with one-hour metadata caching.
- `/rival-quote`: verified timestamped BTC/ETH/SOL futures best bid/ask,
  spread, top-book quantity and explicitly assumed round-trip costs.
  If Binance USD-M denies access or quotes are stale, returns **unavailable**.
- `/rival-target-proof`: show the independently heldout research evidence
  for your saved exact-price goal. Outputs **NOT QUALIFIED** instead of
  inventing a price-hit probability when model gates fail.
- `/rival-paper-open`, `/rival-paper-close`, `/rival-paper-results`:
  quote-to-quote paper positions with per-user limits and after-cost
  hypothetical P&L. Absolutely no account orders or actual fills.
- `/rival-stop`: cancel and clearly mark the old target card inactive.
- `/rival-research`: simple opportunity summary; historical math, Gemini
  and verified forward scores remain available behind advanced sections.
- `/rival-data`: the detailed collector dashboard.

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
