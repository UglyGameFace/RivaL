# Four independent RivaL prediction engines: evidence and release status

Updated 2026-10-30. This page is a **production verification record**, not an
assertion that the bot beats market prices. Current task branch:
`fix/rival-kalshi-executable-book-evidence-20261030`.
Scope: read-only forecasts and paper evidence. Real trades remain out of scope.

## Market model status, without venue substitution

| User-selected market | Genuine current foundation | Missing proof or independent access |
| --- | --- | --- |
| Polymarket | Gamma-listed contract identity, CLOB order books, contract-specific experimental forecasts, separate forward observation and research | Actual verified matching-oracle resolution, immutable forward holdouts, stable calibrated advantage over **that exact contract's quoted book** after fees/slippage, observed Discord live acceptance |
| Crypto perpetuals | Multi-exchange observations, venue-specific forward and outcome ledgers, challengers and guarded promotion | Consistent fresh, out-of-sample, after-cost performance across supported asset/horizon/venue pairs, not just a historical positive candle-direction rate |
| Kalshi | Public REST series/markets and order-book receipts; frozen per-contract first quote, series-only shadow learner and official settled YES/NO labels | At least 150 untouched and genuinely **book-backed** challenger outcomes spanning 21+ days in the same series; calibration, comparative skill, execution fees, liquidity and prospective after-cost evidence |
| DraftKings Predictions (DKeX) | Locally imported SHA-checked daily market and settlement CSV research; external official web link | Independently authenticated source provenance; an authorized, documented live contract API with bid/ask and size; stable IDs, exact rules, official outcomes; independent historical and *future* model evaluation. **No live API is claimed connected** |

The DraftKings Predictions marketing website or a third-party page is **not**
by itself an authorized exchange-data API; do not reverse-engineer a private app
endpoint or mislabel delayed reports as executable quotes.

## This change: Kalshi evidence integrity

Kalshi's official unauthenticated `GET /markets/{ticker}/orderbook` exposes
`orderbook_fp.yes_dollars` and `no_dollars`: **bids**, not direct asks.
For a standard $1 binary contract:

- Top executable YES ask = 1 minus highest NO bid; available YES size is the
  size at that NO bid.
- Top executable NO ask = 1 minus highest YES bid; available NO size is the
  size at that YES bid.
- Empty/malformed/zero-depth/stale/unreachable books => no new book-backed
  learned observation. Market-summary ask fields are indicative only.
- Even book-derived top asks are only point-in-time quotes, **not guaranteed
  fills at a desired size** and not calibrated winning probabilities.
- Exact immutable first recorded book-backed quote is stored in the existing
  forward ledger. New positive-size book evidence is separately recorded in
  `rival_kalshi_book_evidence`; already existing records are *not*
  backfilled with invented quote provenance.
- Score only forward challenger outcomes tied to that persisted book evidence.
  Older observations can remain in audit totals but **do not count toward a
  book-validated performance claim**. If old unverified settlements trained
  a series' weights, that series cannot pass a research eligibility gate.
- No model is automatically approved or traded.

This change is additive; no existing audit records, model state or trading
preferences are deleted. DDL runs through the existing SQLite/PostgreSQL
initialization path. Rollback means code revert; do not delete provenance or
settlement history.

## Definition of a verified improvement

1. **Input:** Independently verified venue IDs, rules, source timestamps,
   quoted depth and exact real settlement oracle. No exchange-close-price
   substitution for another provider's event.
2. **Causality:** Persist model version and prediction before target closure
   and before any settlement information exists. Freeze and de-duplicate.
3. **Baseline:** Compare against same-venue/time executable quote, not a
   midpoint from elsewhere. Show matched baseline Brier/log loss, calibration
   and daily-block uncertainty, separately by contract series/horizon.
4. **Economics:** Fee, spread, slippage, funding (perpetuals) and fill-size
   limits measured using realistic quotes. Unknown costs => **no edge verdict**.
5. **Promotion:** Independent forward sample adequate in size and duration,
   positive conservative lower confidence bound, no leakage, reliability,
   regime and post-cost checks. Passes can promote research *only* through
   explicit existing gates; new unsupported providers remain disabled.
6. **Operations:** CI on exact PR head, main, accepted release, and hosted
   Discord button/source/DM/settlement checks. A successful deployment action
   is not proof of live profitability.

## Task record

- Objective: deliver all four truly source-matched engines, then establish
  or reject market-beating claims on independently resolved future observations.
- Existing ownership: GitHub issue #138; exchange-perpetual proof in #6.
  Unrelated OpenRouter reliability PR #136 stays separate.
- Root cause corrected here: Kalshi summary prices lacked independently
  checked displayed/executable counterparty book depth.
- Implementation: public order-book parsing, price/depth isolation,
  fail-closed learner, no-cost UI transparency, additive immutable provenance,
  forward score filtering and regression fixtures.
- Tests: pending exact-head Ruff + full SQLite/PostgreSQL pytest.
- Release: main and hosted verification pending until merged CI succeeds.
- Remaining blockers: real future Kalshi/Polymarket/perp performance and
  independently permitted live DraftKings Predictions data access.
- Next: inspect CI failures; merge only exact validated code; verify runtime,
  publish per-market genuine prospective statistics; do not manufacture edge.
