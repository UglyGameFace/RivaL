# Kalshi autonomous mathematical evidence

This change extends the merged PR #131 Kalshi shadow research; it does not activate new orders, paid AI models, automatic user alerts or a Kalshi betting signal.

## What is now autonomous

The bot rotates BTC, ETH, SOL and XRP every two minutes, bounded by the existing four-series and four-contract public Kalshi discovery limits. Only the first valid source quote per exact ticker is frozen. The background worker uses the same in-memory browser as the user, has no additional Discord interactions, cannot request private Kalshi account access, and makes no calls to GPT/Gemini.

Settlements are checked in a bounded queue. Unfinalized markets no longer monopolize the first two slots: the time of the last unsuccessful attempt is persisted, a three-minute cooldown applies, and previously unseen pending markets get checked first, even across restarts. A genuine terminal YES/NO and a non-future settlement timestamp are mandatory before training. Other settlement values and incomplete market records remain unlearned.

## Mathematical validation

The shared `research/probability_evidence.py` scores immutable, prospective **paired** candidate probabilities against the same contract's saved market-implied benchmark. It reports Brier score, logarithmic loss, expected calibration error, count of independent exact contract IDs, active UTC issuance days, and a descriptive daily paired Brier improvement bound. Duplicate contracts, nonfinite probabilities, naive clocks and settlements preceding prediction time are rejected.

Crucially, **approval evidence is per Kalshi market series**, not a misleading pooled BTC 5m+15m+1h score. A series needs at least 150 independently settled candidate predictions across 21 UTC issuance dates, Brier improvement >=0.005, ECE <=0.10, daily gain lower bound >0 and over 60% positive days before the *research* gate can pass.

Even a passing research gate leaves `approved_model=False` and `verified_betting_advantage=False`. Kalshi's real executable fees and spread, market-specific settlement rules, and live forecast performance still need demonstration before any approved betting model. The existing online logistic v1 checkpoint and all prior observations remain backward-compatible and immutable. No use of hidden future labels or re-scoring a contract after its result.

The daily bound is an approximate day-block diagnostic, not a formal independent-results statistical guarantee. Multiple markets can share the same underlying price shocks; more outcomes do not guarantee independent samples. Calibration and day-level comparison improve discipline, not magical accuracy.

## Validation and open limitations

- CI and PostgreSQL tests, exact-commit main checks and hosted Discloud release are separate gates.
- Verify real public Kalshi API connectivity, source-specific market discovery, actual settlement receipts, persistence across restart and no Discord DM spam. The public contract may not have a `settlement_ts` immediately even after close; it remains pending, not guessed.
- Provider-specific price-to-beat, volatility and oracle context are NOT implemented in the Kalshi v1 features yet. A real oracle source and a versioned new model are needed before claiming those inputs drive Kalshi probability.
- DraftKings Predictions remains unconnected to live contract prices. DKeX daily reports are archival research only, not DraftKings app executable quotes.
- Separate Polymarket source-aligned oracle data and six-month model promotion remain gated by genuine forward evidence, not by this Kalshi improvement.
