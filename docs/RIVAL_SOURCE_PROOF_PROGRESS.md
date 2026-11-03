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

## Opt-in tracking of the next X future predictions

Choose **Detailed Research → Results → Track next predictions**. The
**Choose exchange** control takes you to the exact-source proof page, where
you select the source and return. The last-selected source is preserved in
the private menu. Start a cohort of the **next 25, 50, 100, 150, 300 or
500** predictions for **5m, 15m or 60m**, provided that source already has
an actual forward-forecast stream.

A new session is persisted in PostgreSQL (and works with the SQLite test
store), keyed by the Discord owner's ID, asset, source and timeframe.
The session freezes the existing instrument, model version, start timestamp
and planned sample count. Only the **first X issued strictly after the start**
are eligible. The score always includes pending, invalid and losing records
rather than selecting only successful outcomes. Old sessions cannot be reset
while incomplete. Subsequent sessions are permitted only after a previous
session has fully settled with verified model provenance.

The progress screen shows issued, settled, correct direction, Brier loss versus
the contemporaneous basic forecast, and missing evidence. When all X
precommitted predictions have settled, it also compares the earlier and
later halves by their *relative Brier improvement over the contemporaneous
baseline*. A positive later-half comparison is descriptive, not a causal
proof that training made a model better. It cannot auto-promote a model.

This cohort feature does **not** replace the separate 150-future-outcome,
21-calendar-day, 80%-quote-coverage and positive stressed-paper-after-cost
qualification gates. It never constructs simulated fills from historical
candles or labels simulated profits as authenticated real-money gains.
