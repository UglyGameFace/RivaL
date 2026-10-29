# RivaL opportunity-first operating guide

The intended output is a **current, eligible price opportunity with measurable
market-estimated edge** or an explicit **PASS**, not a long introduction to
technical indicators. Profit is not guaranteed. Price comparison alone is not
an independently validated winning strategy.

## Owner-facing Discord commands

- `/rival` — if the bettor's state has been saved, show the best fresh,
  jurisdiction-eligible **single sportsbook price** with at least 2% positive
  cross-book no-vig *estimated* return. If none qualify, say PASS. If the user's
  state is unknown, offer the existing saved-state setup first.
- `/rival-picks` — recheck the provider's current board (respecting quota),
  show a single screened outcome, its sportsbook, displayed odds, selection,
  any line, price estimate and reference count. Never invent an event.
- `/rival-opportunities` — one private compact sports + BTC live research
  watch screen; asset selector switches to ETH and SOL. A live watch requires
  a real prospective forecast already written before its target plus a
  recently collected venue/candle. No forecast or stale data -> PASS.
- `/rival-research` — now opens on the watch, not a 16-page history lesson;
  the same controls expose Math & History, Gemini and actual Forward Results.
- `/rival-data` — advanced live source/collection health remains available.

The crypto screen deliberately does **not** convert an unqualified raw
probability into "BUY NOW", a fake stop loss, a fictional executable mid-price
or a guaranteed profitable entry. Prices represent the last validated closed
candle, not a limit order book quote. `Result proof` is based on actual
prospective forecast outcomes and displays insufficient evidence when that
is the case. The older 42 historical candidate evaluations produced **zero
qualifying new live models** and are never counted as a realized trading edge.

## Automatically enabled when prerequisites really exist

- SportsGameOdds current odds: scheduled background refresh every
  `RIVAL_CURRENT_REFRESH_SECONDS` (default 600), with the existing
  `SPORTSGAMEODDS_API_KEY` and provider reserve/expiry protections. Without
  that key, the bot cannot invent sportsbook prices.
- Crypto: existing multi-venue PostgreSQL read-only collector, live
  prospective shadow predictions, automatic settling, Gemini research
  (when its own key exists), history recovery and quality audit continue with
  their existing automatically enabled settings.
- Google Drive: `RIVAL_ARCHIVE_AUTOSTART_WHEN_CONFIGURED=true` by default.
  Once *all* root/history/manifests/staging folder IDs and independent
  bot-owned Google OAuth values exist, start both the sports cold archive
  and older perpetual candle copy safely. Partial credentials never
  trigger archive startup. Owners can opt out by setting this flag false.
  Actual Google Drive API permission, upload and SHA-256 restore must still
  be observed before claiming any production delivery.
- Historical data: the separate monthly official Binance source collection
  workflow has been enabled on the 8th of each month, using bounded network
  and workload settings. If private GitHub Actions Drive credentials are
  missing, publication is not verified and only short-retention Actions
  artifacts are produced. Football-Data's previously unavailable endpoint
  cannot prevent verified crypto data from being archived once authenticated.

## Not secretly authorized

No real-money trading account, API key, exchange margin settings, bankroll
size, fee schedule, sport wagering account or regulatory eligibility was
connected and checked here. No order placement or cash betting is enabled.
Any future execution capability must be a separate authenticated, opt-in
feature with per-account spend limits, exchange rule checks, kill switch,
fee/funding/slippage-aware paper validation, idempotent orders and explicit
owner approval. Merely having historical data or high raw accuracy is not
a substitute.

**What actually matters:** fresh source quotes, honest price comparisons,
prospective calibrated probabilities, net expectancy after real costs,
observable drawdown and repeatable positive independent outcomes. If that
last evidence is absent, the profitable decision can be not to bet.

## Pick a target, see UP or DOWN

Use **`/rival-target`**. Choose **BTC, ETH or SOL** and **5 minutes or
15 minutes**. RivaL DMs a single private status card:

- **📈 UP**: latest valid pre-outcome directional model watch points upward.
- **📉 DOWN**: latest valid pre-outcome model watch points downward.
- **⏳ WAIT**: no current valid prediction, insufficient current feed or tied
  probability. No invented directional assertion.

The card includes the most recently observed closed-candle price, source and
raw model probability where available. Its time window is explicit. These are
**uncalibrated research predictions, not approved profitable trades**. A candle
close is not an executable quote, and historical data alone is not live proof.

**Changes automatically:** the Discord bot rereads the actual PostgreSQL
prospective forecast ledger about every 60 seconds and edits the **same**
private DM only when the forecast, raw odds, recent price or status changes.
This can show a switch from UP to DOWN, or WAIT when a prior prediction
expires or a venue goes stale. It never sends another message for every
candlestick. The one active target per user survives Discord-bot restarts and
can be changed by running the target command again. The default Opportunities
screen also has a **Follow 5m UP/DOWN** button targeting the currently
selected coin.

**`/rival-stop`** cancels and marks the old DM **Target stopped**, so an old
green or red card cannot be mistaken for an active forecast. If the user
blocks DMs, registration fails with a clear error and no target is silently
created. If the user deletes the monitored message or revokes permissions,
the worker stops the inaccessible subscription rather than sending replacement
spam. The bot stores only Discord user/channel/message IDs, selected asset,
horizon and last display digest in the existing relational database.
One monitored target per user, up to 100 active targets per bot instance.
Nothing is sent unless the user opted in.

Only 5m and 15m intervals are offered because those are the actual supported
prospective model horizons today. Additional horizons, target-price hit odds
or trading automation require independently tested forecasting models.
