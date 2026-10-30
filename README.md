# RivaL

**Know the line. Build the slip.**

RivaL is a provider-agnostic sports-odds intelligence and parlay-analysis project. It keeps sportsbook coverage, line comparison, historical movement, jurisdiction filtering, and parlay construction behind a simple Discord experience.


### Faster Discord controls and historical training provenance

The Discord response and live-market scan are separate workloads. Button
handlers **acknowledge before external database/Discord requests**; the
private forecast page first shows a loading screen, then updates only if
that page is still current. Model-proof lookups have short timeouts and
unexpected view failures open a new usable private Home menu.

The WebSocket feeds still supply genuine market-book data and forward
cost research. Old momentum-signal DMs and duplicate Auto-Hunt delivery
workers were retired: market-price notices are **not prediction calls**.
RivaL silently studies verified listed Polymarket contracts without sending
DMs or consuming AI quota. A first contract observation is stored for
later exact terminal payout labeling. This is not a claim of better accuracy.

Historical training status: **/rival → My Results → Can I Trust Predictions?**
shows saved/reused live weights, actual learning cycles, Polymarket outcome
counts and the verified historical model inventory. The live v3 learner keeps
its weights across restarts and learns each newly matured observation once.

The six-month workflow now also trains the exact live 40-bar/10-feature
contract. Three regularized logistic fits and their logit ensemble compete on
purged chronological validation; only the selected method opens the final
holdout. Compatible passing weights are loaded from PostgreSQL as independent
persistent research challengers. Source-matched frozen forward predictions,
calibration and day-blocked paired improvement govern research selection and
rollback. Existing quote/cost gates still govern qualified trade calls. Legacy
six-weight artifacts remain separate; the importer understands their original
bundle without pretending those features match the current model.

The historical workflow runs monthly and when research code reaches main,
unless `RIVAL_HISTORY_MONTHLY_ENABLED=false`;
manual `workflow_dispatch` remains available. Drive is the cold archive: its
bounded six-hour verified import is outside prediction and Discord paths.
Historical results never become live accuracy by declaration.

**Live Polymarket:** selected 5m, 15m and verified hourly contracts have their own
experimental AI calls. Start Auto-Track keeps one DM updating until Stop,
including rollover. Quotes refresh about every 15 seconds independently of the
shared free AI quota (18 requests/day). A separate book-only learner uses exact
terminal Polymarket payout receipts and needs 100 settled contracts per source
and timeframe before issuing local experimental model calls. It never trains
on exchange candle labels or AI confidence. See
[continuous calls and learning](docs/RIVAL_CONTINUOUS_LEARNING.md) for measured
results and production acceptance limits.

### One notification center, no price-change DM spam

Open **/rival → Notifications**. Track a verified Polymarket market in
**one continuously edited private DM**, or deliberately follow a separate
exchange research forecast. **Stop All** and **/rival-stop** cancel all
personal tracking, including old Auto-Hunt subscriptions. Existing historical
delivery/audit rows and preferences are preserved for integrity and rollback,
but the retired standalone Auto-Hunt notices and price-reversal DM workers
do not run.

The **Unlimited** setting previously removed the daily cap independently
for multiple old tiers. It did **not** produce additional mathematical or AI
predictions. Background market observation now proceeds silently.
A contract AI/model **UP, DOWN or WAIT** may be displayed only if genuinely
issued for the exact verified Polymarket contract; current UP/DOWN share
asks remain market prices, never calibrated model probabilities.

### Start in Discord

Type **`/rival`**. The beginner menu shows:

- **Crypto:** current verified Polymarket contracts and a separate exchange
  research forecast path.
- **Sports:** available games, sportsbook prices, and research-only slips.
- **Notifications:** see whether personal trackers are on and stop all of them.
- **My Results:** verified forward outcomes and fake-money practice results.
- **Practice:** simulated, non-executable trades.
- **Help:** plain-language instructions.

Use **Home** or **Back** to navigate. Technical options live in
`/rival-tools`, not the beginner flow. No wagers or trade orders are placed.
The public commands remain `/rival`, `/rival-signal`, `/rival-stop`,
and the optional `/rival-tools` group.

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

### XRP and 60-minute forecast research

The read-only forward market catalog now includes **BTC, ETH, SOL and XRP**
and horizons **5, 15, 60 minutes**. The extra hour means a forecast for the
direction of the five-minute reference candle that *opens one hour after the
source candle*, **not** an hourly candle, a one-hour average or a Polymarket
Up/Down outcome. Four-asset data collection is the **default configuration**;
existing deployed environment overrides may still collect only three assets.
A venue must actually list the perpetual and produce fresh, causally
observed candles before the model can issue a new genuine prediction. Until
the new asset/horizon accumulates its own settled track record, it is
research-only and displays WAIT/UNVERIFIED instead of invented accuracy.

Existing private Auto-Hunt subscriptions retain their old BTC/ETH/SOL,
5m/15m notification coverage. New XRP/60m predictions can be inspected
from the main crypto signal screen, but expanding automatic notifications
requires a separately authorized opt-in, not an unannounced scope change.

### Automatic model qualification and alert delivery

Champion promotion now requires the paired short-window model tests **and**
21 distinct UTC days, at least 150 unique outcome windows, usable matched
quote coverage, strict calibration and positive conservative/severe
simulated-cost evidence. Each outcome window is counted once, not once per
exchange. Historical market backfill never becomes a newly issued forecast.
The qualification is a conservative **research** gate, not a guaranteed
return or a market-specific binary win probability.

The Auto-Hunt outbox stores claim, READY, SENDING, SENT and UNKNOWN state
transitions in existing SQLite/PostgreSQL tables through additive columns.
Only failures definitely **before** Discord message creation are eligible
for bounded retries; timeouts during or after sending remain UNKNOWN to
avoid unwanted duplicate notifications. Subscription ON and verified DM
delivery are separate. No new wallet or wagering permissions are requested.

### Research alerts and quoted prices

A severe-cost historical simulated trading mean is **not** an expected betting
edge. Auto-Hunt will not describe perpetual exchange quotes as actionable
Polymarket prices. Its qualified tier remains a **research watch** until a
verified market-specific event, calibrated win probability, executable contract
price and applicable fees establish a separate expected value.

Polymarket notices show the best displayed ask and an estimated order-book
average for **10 shares** if enough live quoted size exists. This depth
estimate excludes fees and latency, and it is not a guaranteed fill. Market
availability and legal eligibility vary by jurisdiction. Alert status means
*configured*, not that future DMs are guaranteed. The existing once-only
claim policy intentionally does not retry ambiguous sends.

XRP and one-hour forecasts are not shown yet: the active forward models are
still built around BTC/ETH/SOL and 5m/15m future five-minute reference candle
outcomes. A one-hour label alone would not create a validated one-hour model.

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
