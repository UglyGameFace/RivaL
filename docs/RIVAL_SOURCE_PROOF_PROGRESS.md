# RivaL: exact-source forward-proof progress

In Discord open **Detailed Research → Results → Is any model proven better?**
and choose a coin and an exchange. The report reads the latest *persisted*
family-governance verdict for that exact market, instrument and timeframe.
It does not train models, run the expensive evaluator, fetch external market
data, or make trading decisions on a button press.

## What the report verifies

- At least **150 independently settled, future-issued outcomes** from at least
  **21 UTC days**, for the same exchange, contract and model family.
- Candidate Brier accuracy beats the contemporaneous incumbent and the
  independent base-rate/coin-flip screens; calibration must have passed.
- At least 150 eligible windows and matched bid/ask exits, at least 50 paper
  trades, and at least **80%** matched quote coverage.
- Conservative and severe *hypothetical* after-cost paper results passed the
  family evaluator's day-block profitability and paired-gain lower bounds.
- No corrupt evidence and a current, fresh (48 hours or less) paper-governance
  decision. Expired approvals lose their display qualification.

RivaL cannot acquire 21 calendar days of genuine forward results instantly
by importing historical candles. Historical archives can train challengers,
but only *later*, independently observed outcomes test their forecasting
advantage. Model families, exchanges, horizons and distinct instruments are
never pooled to make an unqualified candidate appear to pass.

## Limits and safety

A **paper-qualified** family means only a shadow model passed strict
*observational* checks. Fees, slippage and funding used for stress testing
remain explicit assumptions. No authenticated actual fills, funding debits,
payouts or trade-account history have been verified, so **real profitability
is unproven**. The existing promotion gate remains the sole authority for
read-only watch selection. The report never permits real orders.

Missing tables, old evaluations, malformed counters, bad quote denominators,
missing models or contradictory evidence produce a conservative unavailable
or held result. They never become zero-loss wins.

Existing menu functions and market data collection continue unchanged;
this adds a lightweight read-only proof view plus source/asset selection and
regression tests. Follow up after deploy by inspecting the record timestamps,
live 5m/15m outcomes and source-specific evaluation counts in the connected
Discloud PostgreSQL database. Source-specific paper proof is not a substitute
for independent actual-execution records.
