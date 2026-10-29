# RivaL: real-world profitability evidence protocol

## Why RivaL does not yet prove profit

The existing `perp_forward_audit` is a legitimate **pre-outcome forecast**
ledger for BTC/ETH/SOL on 5m and 15m horizons. Its original cost stress
estimates assume a price change from one completed candle close to another,
without live executable entry and exit quotations. A directionally correct
prediction may lose after fees, spread, funding and slippage. Neither the
current `rival_practice_accounts` fictional balance nor a bookmaker
no-vig consensus is verified real-money P&L.

**Do not fill this gap with backfilled candle prices.** That would create
profitable-looking results that could not have been traded at the claimed time.

## New continuously operating quote-based research

`/rival-profit-proof coin:BTC timeframe:5` reads the new
`rival_forward_quote_profit` ledger, which is separate from the user's
private practice bankroll.

- Once per scheduled shared market collection, reuse the existing
  Binance USD-M public, bounded `/fapi/v1/ticker/bookTicker` client.
  When region-blocked or rate-limited, it records **no fake quote**.
- Select the earliest prospectively issued direction forecast in each
  asset/horizon/target-close window. Only directional probabilities at
  most 0.45 or at least 0.55 qualify for *observation*; this is not
  an approved edge threshold.
- The exchange quote must be fresh (≤10 seconds), must be no later than
  90 seconds after the original forecast was issued, and must precede
  the outcome's target close time. Record the original forecast venue,
  model, timestamp, quoted symbol, timestamp, actual bid, ask, sizes,
  simulated side and fixed **25 USDT notional** BEFORE the target closes.
- Observe the **same exchange and instrument** at the future target-close
  window (within 90 seconds). A LONG uses entry ASK and exit BID;
  SHORT uses entry BID and exit ASK. Reject invalid or insufficient
  order-book depth. Persist the quote-derived gross return.
- If the exit quote is late or missing, close the observation as
  `MISSED_EXIT` without calculating a favorable or unfavorable
  price from later candles. Preserve lost data coverage in the report.
  Keep every observed paper loss and prevent duplicate positions across
  the often-correlated source exchanges.
- The report shows counts of eligible forecast time windows, recorded
  entries, genuinely observed exits, missing exits, and quote coverage.
  It applies costs **in addition to the observed bid/ask spread**.
  Assumed scenarios are: conservative = 8 bps round-trip fee,
  4 bps total extra slippage, 2 bps adverse funding; severe = 25 bps
  fees, 30 bps extra slippage, 25 bps funding stress.
  These are declared *assumptions*, not measured funding or fills.
- A shadow stress pass requires at least 150 independently tracked
  quote-paired windows, at least 21 UTC calendar days, at least 80%
  coverage of eligible windows, positive mean after both cost
  assumptions, positive daily-block lower stress margin under each
  and at least 80% positive UTC days. This conservative stress screen
  is a heuristic, not a universal 99% confidence interval, and does
  not prove expected future profitability.
- **Never** label a shadow stress pass as actual verified profit;
  no real orders are placed and actual fill receipts do not exist.
  Candidate models are not promoted to real-money trading based on
  hypothetical outcomes.

## What is needed for a real profit claim

1. Independent *prospectively issued* predictions and a predeclared,
   executable decision rule, including PASS/no-trade windows.
2. Read-only, authorized, user-account-specific trade/fill receipts
   linked to venue order IDs, fee tiers, funding debits, timestamps,
   quantity, partial fills and authenticated settled balances.
3. Settlement and realized gain/loss for **every** executed position,
   including losing fills and unmatched/cancelled orders.
4. Strict calendar-block evaluation across substantial independent
   sample sizes, out-of-sample periods and realistic adverse
   assumptions, with honest coverage and uncertainty.
5. Comparison to passive/baseline strategies, drawdown and leverage
   exposure, plus continuously running regression/drift checks.

No existing public market-data endpoint can authenticate an individual's
real fills. A user-controlled permitted read-only account integration
is required; **do not collect trading passwords or API keys in Discord**.
Never automatically wager or switch on leverage for a profitability test.

## Production acceptance

GitHub full tests + PostgreSQL16 integration must pass at the exact
merge SHA. Discloud **upload** is a separate gate from bot startup and
real exchange access. Run `/rival-health` and `/rival-profit-proof`
in Discord to confirm actual command sync and whether fresh quotes
have been ingested. A geoblocked quote provider means NOT PROVEN,
not a reason to use stale candle closes as a fallback.
