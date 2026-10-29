# RivaL: verified sports models, forward-only shadow promotion, and funded practice

## What was actually added

- `/rival-practice`: per-user **fictional** funded account, default
  `10,000` USDT, optionally 100-100,000 before first paper trade.
  Open trade notional is reserved; close releases notional, books the realized
  quote-based simulated P&L after modeled fees/slippage. Bankroll cannot be
  inflated after a trade to erase an unfavorable practice record. Maximum
  paper position remains 250 USDT, at most three different coins concurrently,
  and new entries stop once negative realized daily paper losses reach 5%
  of the starting balance. Persisted with an additive PostgreSQL table.
  No real deposit, account access, trading API or auto-order functionality.
- Per-user strategy feedback groups a minimum five **settled** practice
  decisions by coin and LONG/SHORT, including net returns and all losses.
  These are selection-biased outcomes of voluntary practice decisions and
  therefore **must not** be counted as out-of-sample model successes.
  A successful fake balance does NOT trigger a production model change.
- `/rival-sport-proof`: proof-oriented Poisson **soccer regulation-time
  win/draw/away** model and Gaussian **basketball home/away** model.
  At least 130 finalized same-league games, 50 chronological forecasts
  across at least 14 dates and Brier improvement over pre-observed
  league base rates are required for `HISTORICAL_SHADOW` evidence.
  It cannot claim profitability without real market price and after-cost
  independent prospective evidence.
- The sportsbook collector attempts to store *genuine pregame* probability
  records when past result history qualifies. Predictions are stored
  immutably before kickoff with model ID, team/league identities, baseline,
  timestamps; their source-verified, finalized results settle them later.
  Neither future game outcomes nor revised late odds can create retroactive
  predictions. The score-check loop only polls previously ingested soccer
  or basketball fixtures, every six hours, in bounded batches of eight
  behind the existing provider quota reserve.
- Soccer 1X2 model requires `results.reg.{home,away}.points` to prevent
  extra-time/shootout bias. Basketball uses validated final game points.
  Missing or contradictory scores are refused, not repaired by inference.
  The provider contract distinguishes `eventID` singular from `eventIDs`
  plural; results are still verified against requested IDs.
- Research-only automatic champion gate: at least 100 **prospectively
  issued and independently settled** unique events over 14 UTC days,
  with mean Brier improvement of at least 0.01 over frozen baseline.
  Failed/insufficient evidence means `NOT_QUALIFIED`. If forward
  gain deteriorates to zero or below, the champion is automatically
  **revoked**. Champion state is additive and restart-safe.
  This *never* changes live inference weights or enables betting execution.
  Actual live model-weight promotion requires a verified artifact,
  calibration/drift screens and a tested rollback. There are no auto-trades.
- `/rival-health`: actual bot-ready/sync timestamps and count, worker
  presence and sportsbook refresh freshness. A Discloud upload SUCCESS
  is not a runtime login or command-interaction proof. Owner can now
  verify actual command sync by running the command in Discord.

## External blockers that cannot be faked

1. New sportsbook models must receive **real authorized finalized match
   records** and later prospective games; their qualification is initially
   NOT_QUALIFIED until sufficient games exist.
2. A truly profitable expected-value claim requires independent executed
   prices, actual settlement rules, and after-cost evaluation on fresh
   prospective entries. Neither market consensus nor practice winnings
   establish this.
3. Authenticated real-money profit tracking requires a permitted, read-only,
   per-user exchange/broker account connection and trade/funding/fee
   receipts. No account is currently connected. Never use a global
   shared exchange key across all Discord users, or pretend practice
   balances are authenticated money.
4. Production acceptance needs actual Discloud startup logs and Discord
   `/rival-health` interaction, plus provider data counts. CI and a
   successful deploy ZIP do not themselves prove those externally.

## Source docs

- SGO GET /events: https://sportsgameodds.com/docs/endpoints/getEvents
- SGO sport periods: https://sportsgameodds.com/docs/data-types/periods
- SGO bet settlement: https://sportsgameodds.com/blog/how-to-settle-bets-with-an-odds-api

## Rollback

All schema additions use `CREATE TABLE IF NOT EXISTS`, preserving
existing forward forecasts, venue candles, historical archives, quote-based
paper entries and Discloud host credentials. The new worker can be
disabled by the existing SportsGameOdds collector configuration without
deleting previously verified records. Revert release code to the prior
tested `main` revision; do not delete the new history tables for rollback.
