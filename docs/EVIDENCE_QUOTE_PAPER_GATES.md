# RivaL quantitative evidence upgrade (2026-10-29)

## What now runs, and what does not

### Executable-market reference quotes

**`/rival-quote`** reads the documented public Binance USD-M
`GET /fapi/v1/ticker/bookTicker` endpoint. It displays the **current top
bid and ask**, top-level quoted quantities, the real provider transaction
timestamp, spread and an **explicitly hypothetical** instantaneous round trip
after 4 bps each way and 2 bps exit slippage. It never silently substitutes
a 5m candle for an executable bid/ask. The quote is invalid unless both
exchange and receipt timestamps are at most 10 seconds old, bid < ask,
quantities are nonzero and the contract is exactly BTCUSDT/ETHUSDT/SOLUSDT.
Caches at most 3 seconds and rate-limits source failures; a 451 means this
venue is blocked, not that RivaL somehow has a different exchange quote.
There is no wallet, brokerage key or order submission in this client.

Provider: https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Symbol-Order-Book-Ticker

### Exact target empirical probability

**`/rival-target-proof`** audits the *owner's existing* private
`/rival-target` **exact USDT price**. It takes the original recorded
price, source, contract and opt-in timestamp, selects a maximum of 5,000
genuinely observed 5m candles on the same venue, uses a fixed rolling
volatility-normalized barrier and two fixed momentum cohorts, and
chronologically splits non-overlapping training targets and independent
holdout outcomes.

No fitted model probability appears unless all are true:
- Future candles really arrived after each reference and within bounded
  observation delay; backfilled hindsight is not a prospective forecast.
- At least 160 training labels, 70 untouched holdout labels, 7 independent
  UTC days, 35 examples in both fixed momentum cohorts.
- Brier improvement of at least 0.005 over the training-only baseline
  on the untouched holdout.
- At least 75% of holdout days individually benefit.

If any check fails, Discord says **NOT QUALIFIED** and refuses to invent
P(HIT). Passing would report a **research-only empirical probability**,
not a guaranteed target fill, real trade edge, live profitability,
certified calibration or permission to deploy weights automatically.
Do not fit on untouched holdout results and then report that holdout
again as fresh validation. No historical model version is promoted by
this command. Current autonomous shadow governance stays conservative.

### Real provider-entitled sports coverage

**`/rival-leagues`** reads SportsGameOdds `/v2/leagues` with the user's
configured API key and caches for one hour. Shows actual enabled,
account-entitled sport IDs and league names, not all 67+ advertised
leagues. This is a coverage registry, not a claimed odds feed or
sport-specific statistical predictor for every listed competition.

`/rival-sports` separately shows only fresh, actually ingested events
and bookmaker prices. Existing `RIVAL_CURRENT_LEAGUES` and provider
quotas remain authoritative for active collection; no suddenly
unbounded provider scanning. SGO documents that plan entitlements
control league availability. Without a valid key, don't assert access.

Provider: https://sportsgameodds.com/docs/data-types/leagues

### Quote-based paper accounting

**`/rival-paper-open`**, **`/rival-paper-close`**, and
**`/rival-paper-results`** let users make separate private
experimental **LONG/SHORT** paper positions and show **all** after-cost
results including losses. Entries use the verified best ask (long) or
best bid (short); exits reverse the side of the book. A position needs
enough top-of-book displayed quantity at both timestamps and a later
verified exchange quote to close. The persisted position is idempotent
per Discord interaction ID, unique open slot per user+asset, ≤3 open
coins, ≤250 USDT per position. All calculations include estimated
4 bps taker fees per side plus 2 bps per side hypothetical slippage.

**These are NOT authenticated actual fills**, and actual funding or
liquidation cannot be verified. Do not combine paper P&L with real
trade statistics or call a profitable paper ledger a proven edge.
No automated wagering, no real-money exchange trading, no access to
other users' positions. Existing DB/forecasts/Drive archives untouched.

### Model rollout and rollback gates

Existing RivaL `governance.py`, `shadow_monitor.py`,
`calibration_audit.py`, and `cost_stress.py` remain the authority:
chronological prospective forecast/settlement, heldout Brier screening,
reliability checks and **explicitly hypothetical** cost stress. This
release **does not** auto-promote weights or modify the main inference
model. An independently qualified challenger, actual trade-cost
evidence, explicit release authorization, and a tested reversible
deployment mechanism are still required before enabling promotion or
real-money orders. Merely increasing a model's accuracy from about
46% doesn't prove it profitable.

## Production acceptance

- Ruff, full pytest and Postgres16 on the exact PR SHA and merged SHA.
- Official Discloud upload success on that same verified `main` SHA.
- Live Discloud process restart and Discord command sync checked
  separately; provider regional access/permissions can fail in production
  even when all tests pass.
- User-issued real `/rival-quote`, `/rival-leagues`,
  `/rival-target-proof`, and read-only paper commands must be verified
  with actual timestamped market observations before claiming live service.
- No model may be advertised as having proven profitability without
  authenticated exchange order/fill, fee, funding and slippage receipts.

**This is a substantial end-to-end evidence improvement, NOT proof that
all sports, every exchange, and every requested predictive model are
live, independently validated or consistently profitable.**
