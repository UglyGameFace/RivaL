# RivaL forecast training lineage

Every newly issued forward exchange research forecast records an immutable model
lineage row in `rival_forward_model_lineage`, in the same transaction as the
forecast and regime fingerprints. It reports the exact input feature and model
weight hashes, the number of causal online training updates, and whether the
chosen model actually incorporated a **previously screened compatible historical
artifact**.

This is **not** a historical candle count and is **not** a forward accuracy
measurement. The old six-month research archives remain separate from live
inference. A historical seed appears only when the strict offline screen and
independent forward challenger-selection gates permit it. The October 30
six-month historical rerun admitted **zero of nine** candidates. Do not bypass
those gates or treat the historical archive as if it had been loaded in every
prediction.

In Discord, open **Results → Training actually used** for a concise, source-specific answer about the current mathematical models. Old forecasts without new lineage remain UNKNOWN. No claimed historical seed means the current code has not proven it consumed a screened six-month model.\n\nUse `ForwardAccuracyMonitor.learning_lineage_summary(asset="BTC",
horizon_minutes=5)` to inspect the latest 100 real forecasts issued within
seven days. Results distinguish forecasts with known evidence, old forecasts
with unknown lineage, and genuinely historically seeded forecasts. A zero
seeded count is an accurate result, not a pipeline failure.

## Production acceptance

1. After the release, confirm exact hosted commit and one newly issued BTC
   forecast. Check the corresponding lineage row has the same scope, issue
   time, feature SHA and weight SHA as its `perp_forward_regimes` row.
2. Verify a restart reuses checkpoint weights and increments online learning
   only for newly matured labels. If a forecast is a duplicate, its lineage
   must not be rewritten.
3. Keep `historical_seed_sha256 = NULL` until a matching, past-complete,
   offline candidate genuinely wins independent forward evaluation.
4. Check forward directional accuracy, paired Brier improvement versus
   baseline, calibration, actual contract settlement source and after-cost
   trade evidence **separately**. Training volume or provenance is not
   demonstrated prediction skill.
5. Expose this evidence in a beginner-friendly accuracy view only when it
   can be read cheaply without blocking Discord's interaction response.

No trading, permission, model promotion, notification or historical dataset
changes are introduced by this lineage milestone.
