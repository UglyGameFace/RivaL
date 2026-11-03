# RivaL Intelligence: from LFM3 feature idea to genuine future evidence

This pipeline uses the existing private **LFM3 1.7B**, not Gemini,
OpenRouter or another provider. Nothing automatically places a trade.

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
