# RivaL Betting Genie: user-entered crypto price targets

## Two genuinely different questions

1. **Direction:** `/rival-target coin:BTC timeframe:15 minutes` leaves
   `target_price` empty. The existing prospective mathematical forecast
   gives **UP / DOWN / WAIT** using its real prior-issued model ledger.
   The displayed raw directional probability is **not a profit estimate**.
2. **Specific price:** `/rival-target coin:BTC timeframe:15 minutes
   target_price:75000` creates a private, persistent **USDT price-touch
   watch**. RivaL records a fresh, closed, venue-specific reference candle
   when you opt in. It will state **WATCH**, **WAIT** (stale/unavailable feed),
   **HIT** (verified later completed candle touch), or **EXPIRED** (no verified
   hit in the window). This is **not** a claim that a predictive model
   assigns a known probability to BTC reaching $75,000.

The price is **a market goal**, not your quantity of BTC, bankroll, order
amount, leveraged position or order instruction. The only initial supported
assets remain BTC/ETH/SOL, the only supported user-selected deadlines 5m/15m,
and the instrument is the registered exchange's USDT-quoted contract. Price
is an optional slash-command number. It must be finite, positive, at most
1e12, with at most eight decimal places. The latest verified closed candle
cannot equal the requested goal, to prevent immediate false HIT claims.

## Data integrity and target result rules

- The underlying *exchange*, *contract*, *USDT quote*, *reference close*,
  *start time*, *target price* and *5m or 15m deadline* are frozen at opt-in,
  stored in a new additive PostgreSQL `rival_price_goal_settings` table.
  Existing `rival_direction_targets`, subscriptions, and forecast history
  are preserved unchanged. The price-goal row is updated or deleted in
  the same DB transaction as its associated existing private Discord target.
- A goal is never activated using a disconnected feed or stale historical
  reference. It uses a live feed status and a recent completed 5-minute
  candle with true `first_observed_at`.
- Future price-touch evidence comes from **completed 5m exchange candles
  first observed after opt-in**, in that original venue+contract. A future
  candle completely after subscription can prove a hit if its `high_price`
  crosses an upward price threshold or its `low_price` crosses a downward
  threshold. If a candle was already open before subscription, its high/low
  could have happened **before** the user opted in, so only its final
  close can prove a threshold crossing.
- Price touches are recorded conservatively at the **completed bar's
  close timestamp**, not a fabricated instant of tick execution. Late-arriving
  valid historical bars can change EXPIRED to HIT on later verification.
  EXPIRED does **not** prove the price never touched the target intrabar,
  because incomplete or missing sub-bar observations cannot be excluded.
- The card is updated in the **same DM** around once per minute, using a
  deterministic digest. No unrequested new messages. Subscription,
  timeframe and source survive restart. `/rival-stop` removes both
  subscription and price-goal configuration and marks the old DM inactive.
- Neither a higher/lower target relative to the reference candle nor a
  raw UP/DOWN forecast is a statistically validated target-hit probability.
  No target-specific numeric `P(HIT)`, expected return or actual PnL is
  calculated without qualified historical labels, calibrated model and
  executable fee/liquidity data. No real-money execution occurs.
- The source uses completed candles, **not live order-book ticks or quotes**.
  In particular, 5m goal watches may have insufficient completed-bar
  evidence during their deadline. The UI says so rather than claiming
  immediate execution. Real-time tick streams, hit-rate model, and
  per-account trading receipts are future research blockers.

## Sport and league discovery

`/rival-sports` lists only fresh, active, future-fixture sportsbook prices
observed from the current configured provider. It displays league, sport,
number of fixtures and books, and the actual configured league filters.

It **does not** fabricate coverage for all listed worldwide sports.
The current SportsGameOdds league search is configured with
`RIVAL_CURRENT_LEAGUES` (NBA,NFL by default); add a supported provider
league ID to the deployment environment only after verifying its contract,
quota and legal availability. Neither seeing a live fixture nor bookmaker
prices proves a statistical edge or legal wagering availability.

## Release evidence

Full Ruff + pytest including live-candle race/freshness tests and
PostgreSQL 16 migration/restart checks must pass at the exact new head.
A subsequent green `main` CI triggers the existing official Discloud
deployment workflow. Its actual API success proves upload only. Actual
Discord login, command sync, market collection, and subscription behavior
still need host-level and Discord verification.
