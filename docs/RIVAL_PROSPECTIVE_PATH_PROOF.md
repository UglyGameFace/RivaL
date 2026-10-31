# RivaL: prospective market-specific target/stop model

**State:** 15m/60m source-matched **paper research only**. Neither a
verified profitable signal, a real order, an authenticated fill, nor an
event-market contract resolution.

## End-to-end background lifecycle

The original five-minute forward-audit cycle is the authoritative issuer.
For eligible up-to-date BTC/ETH/SOL venue/contract/quote candles it can issue
a read-only LONG and SHORT research forecast, in the *same transaction* as
the original logistic forecast. It records an immutable issuance timestamp,
reference/target identity, five-minute volatility estimated from already
closed bars, volatility-scaled TP1 / TP2 / stop barriers, smoothed empirical
probabilities and 10/50/90% terminal signed-return estimates. The historical
training fingerprint, number of nonoverlapping episodes and matched
momentum cohort are fixed at issuance. Six-month Binance archive rows may
inform offline research, but may not be used to invent forward records for
other exchanges.

Each future outcome is separately settled from the **same exact venue,
contract and quote** using a complete chronological 5m path, after the
horizon's last candle is closed and first observed **after** the model issued
its probability. Missing bars, backfilled candle first observations before
issuance, malformed price/clock data, and late-unavailable source data
cannot improve the recorded score. Where TP and stop may have crossed
within the same OHLC bar, the record is **ambiguous**, excluded from accuracy
and never relabeled into a fictitious profitable path.

Source/side-specific, unique target-group Brier comparisons against a
contemporaneous source historical prior use RivaL's existing minimum
100-target, 7-UTC-day and day-block evidence screen. An unconditional
source prior itself is not proof of a tradable edge; **both TP1 and TP2**
must independently pass the forward research screen. Results are shown
compactly on Bot Performance instead of adding another confusing menu.

## What this does NOT authorize

The model is never an order or recommendation, and it has no special access
to the user-facing UP/DOWN gate. `after_cost_qualified` and
`live_approved` remain **false** regardless of its score. The absence of
same-source, same-time bid/ask, spread, fees, funding, slippage and matched
exit execution evidence cannot be filled with a guessed cost or another
exchange's bid/ask. Do not automatically promote this family. Existing
multi-venue signal promotion/rollback and the conservative NO CALL path
remain authoritative.

## Continue under Issue #151 and Issue #6

1. Research-compatible historical and live checkpoints **for the exact same
   venue and feature/target contract**, with immutable source provenance and
   no forced Binance cross-venue transfer.
2. Calibrated forward probability-of-return/path estimates; train on actual
   resolved outcomes for Polymarket/Kalshi/DKeX contracts, never futures
   price direction masquerading as YES/NO settlement.
3. Independently timestamped order-book quotes, same-venue realistic
   entry/exit, complete fee/funding/slippage and conservative after-cost
   paper decision gains for **each specific challenger family**.
4. Source-specific, individually audited promotion/rollback with a fail-safe
   default model and NO CALL for any missing gate or stale source.
5. Real hosted Discord canary and sufficient **future** distinct days,
   targets and changing regimes before any claim of improved skill.

The research table is additive and can remain as an audit ledger after a
code rollback. Tests use isolated SQLite/PostgreSQL and adversarial
missing, duplicate, backfilled and ambiguous paths. The live Discloud
application must retain its prior working forecast cycle if the optional
path research schema or inference fails.
