# Advance next-target forecasts

The existing collector now runs a bounded native-candle advance pass before
optional spike analysis and heavyweight incumbent/challenger research. It uses
the existing causal feature extractor and persistent online logistic learner,
not a second data collector, LLM, or external prediction service.

## Two distinct prediction contracts

| Head | Target opening relative to last input candle opening | UP label | Persistence |
| --- | --- | --- | --- |
| Existing incumbent | Selected horizon (5, 15, 60 minutes) | Target close above the input reference close | Existing ledger and checkpoint contract |
| Advance head | Selected horizon plus 5 minutes | Target candle close above its own opening | `rival_advance_forecasts`, distinct checkpoint target contract |

Every target is a **five-minute candle**. The selected horizon advances its
opening; it is not the candle's duration. An advance target opens when the
corresponding incumbent target closes. For example, inputs through the
09:55–10:00 candle can issue a 5m-head advance for 10:05–10:10 before 10:05.
The 10:05 opening is unknown at issuance and is used only later as an outcome
label. It never enters the feature vector or fitted weights early.

The advance head is `advance-candle-logistic-lead5-v1`; its checkpoint target
contract is `next-5m-candle-close-above-own-open-lead5-v1`. Existing weights,
forecast probabilities, promotion controls, XMarket fits, and quote-cost proof
are preserved. This new raw probability is **not calibrated accuracy or a
qualified trade setup**. It cannot supply TP/SL, change trading thresholds,
promote itself, or issue orders.

## Chronology and failure behavior

- Only the latest completed, source-exact native candle is eligible. Source
  gaps, zero-trade reference bars and stale input clocks decline issuance.
- Training reuses causal 40-bar features and only labels first observed
  strictly before fitting. A batch backfill cannot become live evidence.
- Each venue/instrument/quote/horizon/target/model has one immutable original
  probability, input/weight hashes and issuance clock. Retraining and restart
  do not replace it. Current and next forecasts remain separate rows.
- The production clock is sampled after fitting, inside the write transaction.
  A second clock observes completion of the forecast commit. Both must precede
  the target opening before a reader can show it as a valid advance. A late
  commit is recorded as excluded; missing commit proof cannot be backfilled.
- Source failures are isolated. The pass has a 45-second work budget and uses
  the existing guarded thread runner so a timed-out thread cannot be started
  again concurrently. No new perpetual network polling worker is created.
- Readiness attempts preserve actual reason codes. Absence, stale data,
  insufficient causal history, exhausted budget and missed deadlines remain
  explicit pending states. An unavailable receipt never means zero forecasts.
- Settlement requires the exact native target candle, a first observation
  after close and issuance, and valid timestamps/prices. No-trade outcomes are
  excluded. Brier scores compare against the advance head's own causal past
  rate; none are pooled into incumbent/XMarket qualification evidence.

## Discord and market boundaries

The existing opt-in channel card names each advance's model, native source,
direction, raw confidence, issuance and target interval. It reads the exact
adjacent target of the displayed incumbent when available. At rollover it
retains the prior advance's unchanged probability, while the next target gets
its own forecast. The full ledger retains all previous windows.

An indexed 15-second check detects advance commits while optional research is
still running. The worker also wakes the card service after its priority pass.
Only a successful Discord send/edit records delivery of the original rows
actually included in that card. Repeated edits preserve the first accepted
delivery. The read-only host receipt reports real commit lead time and
issuance-to-delivery latency without exposing channel/user identifiers. Discord
API acceptance is not independent observation on a user's device.

Polymarket uses its own exact contract, published price to beat, frozen forecast
and resolution policy. A discovered next listing without the required price to
beat is explicitly pending. Kalshi keeps its own listed YES/NO contract rules
and expiry; an exchange advance is never substituted for either venue.

## Acceptance and remaining limitations

Tests cover both SQLite and PostgreSQL fixtures: native labels, no lookahead,
backfill exclusion, checkpoint isolation, idempotence/restart, exact-source
settlement, no trades, delayed commit, rollover, missing schema and real API
acceptance versus failed delivery. Loop regressions verify priority ordering
and that an advance timeout preserves incumbent work and the heartbeat.

Deployment acceptance still requires exact PR and main CI, native Discloud
source parity, and fresh authenticated host receipts. Consecutive production
windows must demonstrate pre-start commits and pre-start accepted deliveries.
Existing slow collection or an overlong prior whole cycle can still miss an
opportunity; stage and readiness receipts must establish that cause before a
further scheduling change. Missing source data can never guarantee a call.

The head starts with no independent prospective score history. Adequate future
outcomes, probability calibration, native executable quote/cost proof and the
existing qualification gates remain required before claiming reliable or
profitable predictions. Issues #6, #151 and #299 stay open.
