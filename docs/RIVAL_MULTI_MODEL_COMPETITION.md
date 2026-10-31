# RivaL: verified multi-model competition and NO CALL

## Running architecture

RivaL retains its persistent source-matched logistic learner and already
existing source-fusion promotion machinery. The forward worker also trains two
**separate, non-promoted research families** every cycle from matured
point-in-time live examples: diagonal Gaussian naive Bayes and k-nearest
neighbors (k=21). No remote AI service, heavyweight numerical runtime,
exchange calls or Discord notification worker is added. Inference is
deterministic, bounded to 480 completed training samples per
venue/instrument/horizon, and uses the original ten-feature contract.

The new `rival_model_shadow_forecasts` ledger stores model family/version,
source identity, exact reference and target times, original probability,
incumbent and causal base-rate probabilities, sample count and a fingerprint
of training labels known at issuance. It is inserted inside the existing
authoritative forecast transaction, using a savepoint to isolate optional
model errors. Existing forecasts/checkpoints are never rewritten.

## What counts as a genuine result

Only the original forward auditor's **independently settled, same-source
and same-time** result is permitted to settle a shadow prediction. An
unobserved future candle, a bulk backfill, an old unverified record, or
a different exchange's outcome cannot settle a challenger.

The Bot Performance learning panel reports each alternative model's unique
time-target count, number of days and research-proof status. It compares
prospectively recorded Brier loss to the **contemporaneous** online incumbent,
past base rate and 50/50, using the existing seven-day, 100-unique-target,
daily consistency and conservative day-block stress gates. Different
exchanges targeting the same UTC interval count as one correlated event.
A missing or conflicting outcome contributes no performance credit.

**Research-qualified does not mean live-approved, profitable or safe to
trade.** New families remain shadows. No automatic challenger promotion,
real order, venue crosswalk, contract inference or API spending is added.
Future promotion must also pass source-matched quote availability,
calibration and credible prospective net-after-cost gates, using the
existing production model promotion and rollback pathways. In particular,
there is no migration of Binance-only historical weights onto other
exchanges with incompatible feature/source identities.

## NO CALL policy

The manual exchange signal screen displays **NO CALL** where baseline
improvement, minimum day-block evidence, a fresh quote, or directional
strength is absent. Only the evidence-qualified same-source
research-watch path can display a directional WATCH.

The separately subscribed direction-target card now also fails closed to
WAIT / NO CALL without matching *model-version-specific* multi-day
forward accuracy proof. An unproven 60% probability cannot create a green
UP/DOWN WATCH. Existing subscriptions continue updating one message with no
new unsolicited notifications.

## Release verification

- Run Ruff and the complete project pytest suite against SQLite and the
  isolated PostgreSQL service before merge.
- Test poison future labels, cross-venue attempts, duplicates, original
  source outcomes, missing settlement, and no-proof NO CALL behavior.
- Review exact main commit and CI; Discloud upload acceptance is not a
  runtime Discord interaction or a proven edge.
- Inspect hosted collector/forecast cycles and model-panel counts over
  genuinely later days; only then consider additional model families
  such as gradient-boosted trees trained offline from the verified archive.

Rollback is a code revert. The additive shadow audit ledger is retained
without retroactive promotion credit. Open Issue #6 tracks hosted skill,
performance and independently verified net benefit.
