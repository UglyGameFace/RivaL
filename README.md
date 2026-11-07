# RivaL

**Know the line. Build the slip.**

RivaL is a **Discord-first, read-only market research and prediction-evaluation bot**. It combines crypto-perpetual market observations, separate prediction-market contract research, sportsbook odds comparisons, and fictional practice accounts. Its aim is to learn from *verified* historical data and *genuinely later* outcomes, and to show **WAIT / NO CALL** when the evidence is not good enough.

**RivaL does not place trades or bets.** A working collector, trained model, successful backtest, or GitHub deployment is **not** proof of profitable predictions.

## Start in Discord

Run `/rival` to open the private, mobile-friendly Home menu.

| Area | What you can do | Important distinction |
| --- | --- | --- |
| **Crypto** | Select Crypto Perpetuals, Polymarket, Kalshi, or independent DraftKings-related research | An exchange candle forecast is not a prediction-market contract forecast. |
| **Sports** | Review available fixtures, eligible sportsbook quotes, and market-derived parlays | Coverage depends on configured providers, quota, freshness, and jurisdiction. |
| **Bot Performance** | Inspect actual forward predictions, settled results, model/source evidence, and reliability | Historical fits and practice results do not count as live prediction wins. |
| **Notifications** | Manage opted-in trackers, including a single updating Polymarket DM | Retired Auto-Hunt price-change spam is not a prediction service. |
| **Practice** | Try simulated trades and review imaginary results | No real funds or executable orders. |
| **Help** | See explanations and navigation | Technical diagnostics live behind specialist menus. |

The primary commands are `/rival`, `/rival-signal`, `/rival-stop`, and the optional `/rival-tools` group. Exact available commands depend on a successful Discord command sync on the deployed bot; merged code alone does not prove they are live.

### Market paths stay separate

- **Crypto perpetuals:** BTC, ETH, SOL, and XRP are configured for read-only five-minute observations across ten venues. Five-, fifteen-, and sixty-minute **forecast horizons** refer to future targets based on completed five-minute candles; a 60-minute forecast is **not** an hourly-candle forecast. A venue or asset with missing current evidence produces WAIT, not invented accuracy.
- **Polymarket:** exact listed Up/Down contracts, public book research, optional experimental contract-specific AI/model calls, and opted-in updating DM tracking. Contract quotes are **prices**, not calibrated win probabilities. Settlement requires the exact condition and an authoritative terminal receipt.
- **Kalshi:** separate public contract discovery and prospective research. No approved Kalshi-specific model or interchangeable Polymarket signal is assumed.
- **DraftKings:** sportsbook odds and futures belong under Sports. Independent crypto event-contract research must be labeled separately when a verified native contract or price is unavailable.
- **Sports parlays:** eligible same-book current lines are compared using de-vigged, line-matched market consensus. This is not an independently validated winning-pick model.

See [local-only Intelligence](docs/RIVAL_INTELLIGENCE_LOCAL_ONLY.md) and [continuous contract learning](docs/RIVAL_CONTINUOUS_LEARNING.md), [price goals and contract distinctions](docs/RIVAL_PRICE_TARGETS.md), and [actual profit-proof criteria](docs/RIVAL_PROFIT_PROOF.md).

## What is implemented

### Live collection and mathematical research

- A shared read-only five-minute perpetual collector, with source-specific freshness checks, bounded recovery, and persistent PostgreSQL observations.
- Ten configured venue identities: **Binance, Bitget, Deribit, dYdX, Gate, Hyperliquid, KuCoin, Lighter, OKX, and Phemex**. A configured adapter is **not** proof that every venue, instrument, or historical period is available.
- Frozen, prospective forecasts and later source-matched settlement. The incumbent learner and independent logistic, Gaussian Naive Bayes, kNN, and screened historical/boosted challenger research use model identity and provenance rather than borrowing another exchange's records.
- Source-balanced forward probability scoring, calibration and accuracy screens; disagreements or missing/late data remain visible rather than becoming artificial wins.
- Lightweight [autonomous price/volume spike observations](docs/RIVAL_SPIKE_ANALYTICS.md), with immutable source evidence and future-only *research* outcomes. Spike detections are descriptive, not trade calls.
- Paper quote/cost comparisons and conditional read-only model-family selection/rollback. Research qualification is deliberately strict; hypothetical spread, fees, slippage, and funding stress are **not authenticated real fills**.
- Optional, separately hosted private **RivaL Intelligence** LLM gateway (LFM3, the sole configured model) for bounded explanations of verified evidence. The mathematical decision and NO CALL rule remain authoritative; running an LLM does not train missing market history or establish predictive improvement.

For mathematical admission requirements see [source-specific challenger promotion](docs/RIVAL_FAMILY_COST_PROMOTION.md). The intended research proof includes **150 genuinely future outcomes across 21 UTC days**, at least **80% matched-quote coverage**, calibrated performance against baselines and an independently positive hypothetical after-cost comparison. These are *requirements*, not results already achieved.

### Historical collection: ambitious scope, incomplete proof

The source-exact historical coordinator inventories **10 venues × 4 assets × 16 investigated perpetual data families**. Its investigated families include 1m/5m/15m/1h/4h/1d candles, individual trades, funding, mark/index/premium, open interest, positioning, liquidations, level-2 books, and options/volatility. **Being listed in that inventory does not mean a collector exists or that the source publishes the history.**

- The target is each instrument's **actual, independently verified listing date through the present**, not a fabricated 2021 start for contracts that listed later.
- Scheduled GitHub workflows and bounded Discloud recovery acquire source-specific history where supported. The coordinator routes only known source/family combinations and reports missing routes separately.
- Binance official archive intake includes original ZIP/checksum-backed candles and additional source families such as mark, index, premium, funding, and daily open-interest/positioning where the source provides them. Other exchanges have differing, often partial source paths. Sample trade/book days do **not** certify complete historical tapes.
- The private Google Drive warehouse retains original data, integrity manifests and research artifacts. An archive filename, a successful API call, or a local digest **alone** is not proof of full UTC coverage or remote original-byte SHA-256 readback.
- Historical candidate models are evaluated with chronological holdouts and explicit feature/venue/contract/quote matching. Backfilled candles cannot be passed off as forecasts issued before the outcome.

**Current status (November 2, 2026):** the collection and evidence infrastructure exists, but **complete 2021-to-present, all-family, all-ten-venue history is not verified**. Native listing dates, missing collector routes, original archive completeness, exact Google Drive readbacks, live model imports, and future-only superiority remain active work. No 2021 data belonging to one exchange can certify another exchange.

Follow [Issue #188](https://github.com/UglyGameFace/RivaL/issues/188) for historical completeness and [`ACTIVE_TASK.md`](ACTIVE_TASK.md) for evolving implementation evidence. Avoid interpreting old fixed counts in an issue or log as the latest verified production state.

## How the pieces fit together

```text
Official public exchange data     Sports & event-market providers
               |                             |
        venue/source adapters         contract/odds adapters
               |                             |
               +----------+------------------+
                          |
                  provenance + freshness
                          |
                 Discloud PostgreSQL
                (hot state and ledgers)
                          |
          +---------------+----------------+
          |                                |
   future-only models,             verified archive/
    quote comparisons              research workflows
          |                                |
   results + NO CALL                 private Google Drive
          |                         (history / manifests /
          |                          models / reports)
          +---------------+----------------+
                          |
                 Discord /rival UI
                          |
              private local LFM3
               (evidence summaries)
```

The Discloud bot uses **PostgreSQL** for production state. SQLite is a deliberate local/test alternative. The independent LLM service is not an alternate market database, an order router, or a substitute for mathematical verification.

## Setup and development

**Requirements:** Python 3.11+, a Discord bot token, and either a reachable PostgreSQL database or explicit local SQLite configuration. Provider APIs, Google Drive and optional AI integrations have their own credentials and quotas.

```bash
python -m pip install -e ".[dev]"
# Configure environment variables before starting:
# DISCORD_TOKEN=...
# RIVAL_STORAGE_BACKEND=postgres
# RIVAL_DATABASE_URL=postgresql://...
python main.py

# Validate the local code:
ruff check .
pytest -q
```

For an isolated **local/test** run, use `RIVAL_STORAGE_BACKEND=sqlite` and `RIVAL_DB_PATH=data/rival.sqlite` instead of a production database. Sports background polling is opt-in (`RIVAL_SPORTS_BACKGROUND_ENABLED`), and supplying an API key alone does not enable it. The standard perpetual observation loop is enabled by default, but actual connections still depend on venue availability.

Optional configuration groups:

| Integration | Configuration and operational note |
| --- | --- |
| **Discord / Discloud** | `DISCORD_TOKEN`; optional `RIVAL_DEV_GUILD_ID`. The existing deployment is configured in `discloud.config` with `main.py` and Python 3.11. |
| **Database** | `RIVAL_STORAGE_BACKEND=postgres`, `RIVAL_DATABASE_URL`. Missing production DB configuration fails startup, not silently redirected to SQLite. |
| **Sports odds** | `RIVAL_SPORTS_BACKGROUND_ENABLED=true` plus the relevant SportsGameOdds/OddsPapi credentials. Verify quotas and configured league/book coverage. |
| **Google Drive** | Scoped `RIVAL_DRIVE_*_FOLDER_ID` and `RIVAL_DRIVE_OAUTH_*` secrets. Bot cold archival requires the relevant archive toggles; research Actions may use their own authorized secrets. |
| **AI / Intelligence** | Only `RIVAL_LOCAL_AI_TOKEN` for the standalone private LFM3 gateway. `RIVAL_AI_PROVIDER=local` is the default; `disabled` opts out. External Gemini/OpenRouter/Cloudflare inference is retired, and old provider keys have no effect. No daily token budget applies; CPU inference is serialized. |
| **Polymarket** | Optional CLOB read-only credentials for relevant feeds; do not add trading-wallet keys. |

Never commit tokens, database URLs, OAuth refresh tokens, private Drive folder IDs, generated market archives, or user data. RivaL is restricted to its dedicated `RivaL Data Warehouse` tree; it must not browse unrelated Drive files.

## Build, deployment, and proof

GitHub CI runs Ruff and pytest, including PostgreSQL-backed checks. The official Discloud release workflow is tied to a **successful `main` CI** when the repository has `DISCLOUD_TOKEN` and `DISCLOUD_APP_ID` configured. A successful upload **does not verify** Discord login, market-feed freshness, Google Drive archival, or live prediction quality.

After release, verify host startup and database persistence, Discord command sync, source freshness, original archive receipts, and genuine later forecast settlements separately. Do not equate test fixtures or a completed Action with production acceptance.

Further documentation:

- [Discloud production PostgreSQL](docs/DISCLOUD_POSTGRES.md) and [CI → Discloud deployment](docs/DISCLOUD_AUTODEPLOY.md)
- [Google Drive security boundary and archive format](docs/GOOGLE_DRIVE_BOUNDARY.md)
- [Verified free historical research intake](docs/VERIFIED_FREE_HISTORY.md)
- [Forward-only prediction and hypothetical profit verification](docs/RIVAL_PROFIT_PROOF.md)
- [Current work, blockers and evidence](ACTIVE_TASK.md)

**Bottom line:** RivaL researches, measures, remembers, and abstains. It does not promise a winning bet or execute one.
