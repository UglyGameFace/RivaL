# RivaL Intelligence: from LFM3 feature idea to genuine future evidence

This pipeline uses the existing private **LFM3 1.7B**, not Gemini,
OpenRouter or another provider. Nothing automatically places a trade.

## Bounded combinatorial experiments (2026-11-03)

RivaL Intelligence can now propose `feature_subset_v2` with one canonical
2-to-8-indicator selection drawn from the **existing ten identical live/historical
features**, in addition to the four original named masks. Feature indices map
to recent return, cumulative return, volatility, candle range, relative volume,
RSI, MACD, Bollinger, ATR and volume regime, in that fixed order.

A proposal's index list is checked by the private LFM3 gateway, main-bot client,
persistent PostgreSQL record, source-native relay and independent offline
trainer. The immutable source/proposal SHA includes indices; existing
four-mask records retain their exact original hashes. Noncanonical indices,
duplicates, fabricated features, generated probabilities and direct weight
writes are rejected. **Only one** subset is proposed per six-hour asset slot,
not a hindsight search across combinations. Ordinary logistic and boosted
specialists still compete separately. No feature-subset result can open the
sealed holdout or enter the model import folder before its mandatory
chronological/retrospective screens.

This broadens the hypothesis-design space but **does not yet make LFM3 itself
continually retrain its neural weights**. New experiments are reviewed and
selected by measured results. Later model-family or indicator expansion must
respect exact point-in-time data availability, chronological holdouts,
competitor models and future/after-cost qualification.

## 1. A real authenticated proposal

- The separate Discloud LFM3 service can choose **one** of four fixed feature
  ablations for BTC, ETH, SOL or XRP, using source evidence already collected.
- The main bot stores the fixed choice, asset, original-evidence SHA256, LFM3
  identity, UTC slot, source venues and proposal SHA256 in PostgreSQL.
- Failed requests may retry within the same six-hour slot at most twice,
  after 20 minutes each. One saved proposal is immutable and deduplicated.
  The retries never create a second independent outcome.
- The read-only research relay distinguishes missing/unconfigured/failed
  attempts from a genuinely stored proposal and never exports credentials.
  No source or valid inference means **no proposal**, not an invented success.

## 2. Predeclared mathematical challenge

- Restore **source-native**, checksum-verified consecutive historical months.
  Require exactly matching exchange, instrument, quote and horizon.
- Compare the LFM3-selected masked logistic model against the ordinary
  full-feature logistic and the smoothed training prior on the **same purged
  chronological validation**. Only training labels update coefficients.
- Only masks clearing Brier and calibration screens **and** beating the
  ordinary logistic by >0.005 Brier on validation may inspect the final
  holdout. If they fail validation, that final test remains unopened.
- After validation screening, require >0.005 Brier improvement over ordinary
  logistic on the same holdout, as well as the existing importer thresholds
  for validation/holdout count, Brier gain, calibration and source months.
  The model spec preserves the exact LFM3 proposal SHA and original sources.
- Only passed immutable model artifacts reach the existing Drive model folder.
  All other ideas stay research reports, not candidates, and never replace
  the incumbent's weights. Previously used historical months are
  **exploratory**, regardless of their retrospective scores.

## 3. Actual forward and after-cost proof

- Eligible, imported logistic challengers use the **existing** source-matched
  persistent shadow system. Freeze each probability *before* its genuinely
  future market outcome. No hindsight, source pooling or duplicate target
  windows may inflate skill.
- Compare against existing logistic, simple prior and coin-flip baselines.
  Require calibration and day-block consistency; no historical-only win
  automatically promotes a model.
- Require >=150 unique future targets over >=21 UTC days, >=80% matched
  source-exact fresh quote coverage, sufficient paper trades and positive
  severe and conservative spread/fee/slippage/funding lower-bound tests.
  Re-evaluate and demote on subsequent failure. Use **NO CALL** otherwise.
- Neither a private LFM3 smoke test nor successful training proves a
  tradable edge. Real profit still requires authenticated fill evidence,
  not fabricated paper balances.

### Historical logistic challengers in real source-matched forward proof

The existing screened historical logistic candidates now enter the incumbent's
immutable **future** shadow scoring transaction with their exact model-identity
prefix and hash of the actual weights used for that forecast. Prospective
source/venue quotes then enter the same family cost evidence system as boosted
models. The 150 distinct future targets, 21 UTC days, 80% book-quote coverage,
day-block calibration and both cost stresses are unchanged. A missing,
superseded, revoked or source-incompatible original artifact cannot remain
eligible for use. These are **paper-only** qualifications.

Historical seed counts on the main incumbent continue to read zero until
that incumbent genuinely uses a verified seed. Historical challenger predictions
are not retroactively represented as incumbent predictions. No real fills or orders are implied. A genuinely future-qualified model can
replace only the matching **read-only** venue/horizon watch under its own
version string, with exact artifact revalidation at watch time; losing, expired
or missing-artifact candidates fall back to the unmodified incumbent. Existing
calibration, 21-day and after-cost requirements continue to decide eligibility.
No artifact is qualified by this change alone.

## 4. Operational acceptance

- PR CI: Ruff, all tests with PostgreSQL, source-native and strict
  negative/leakage/duplicate tests.
- Main deployment: original source SHA, bot and separate LFM3 Discloud
  releases, matching authenticated inference and persisted slot receipts.
- Source-native workflow: show proposals received, experiments evaluated,
  screened model artifacts, importer reasons and verified Drive readback.
- Future-only audit: show exact venue/coin/horizon, dates, unique targets,
  baseline Brier, candidate Brier, matched quotes, stress P&L and gates.
  A 0-proposal or 0-approved result remains an unresolved finding.

Related open issues: [#6](https://github.com/UglyGameFace/RivaL/issues/6),
[#151](https://github.com/UglyGameFace/RivaL/issues/151),
[#188](https://github.com/UglyGameFace/RivaL/issues/188) and
[#268](https://github.com/UglyGameFace/RivaL/issues/268).
