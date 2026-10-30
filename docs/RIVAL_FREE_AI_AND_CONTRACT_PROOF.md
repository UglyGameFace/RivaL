# Free-only AI and historical evidence boundaries (October 30, 2026)

## Discloud configuration

- The existing owner-provided `OPEN_ROUTER_API_KEY` is recognized. The standard `OPENROUTER_API_KEY` is an alternative. The bot selects **one** AI reviewer: catalog-verified free OpenRouter coding model when a matching key is present; otherwise the existing Gemini reviewer (if configured).
- An AI reviewer is **not** a model-training service or an authoritative betting signal. Its independent, self-reported directional assessment is only descriptive research over the verified 5m/15m/1h/4h/24h exchange summaries already stored in PostgreSQL. Polymarket Up/Down shares are market purchase prices, not predictions.
- OpenRouter picks from a curated list of code-analysis models after checking `GET /api/v1/models`: `cohere/north-mini-code:free`, `poolside/laguna-s-2.1:free`, `qwen/qwen3-coder:free` and `mistralai/devstral-2512:free`. These are candidates, **not** a claim that every ID is still available or objectively the best at market prediction.
- Any model not returned by the catalog, missing the chat-completions endpoint, expired, not text-output capable, or advertising a nonzero token/request price is rejected. Nothing ever silently falls back to paid models. A coding model being unavailable is reported rather than substituted with random general models.
- All free models share **one** persisted daily request cap of **18**, with a default 60-minute interval, never one budget per fallback. The provider's account-wide rate limits may be stricter, and a 402 or 429 pauses requests. Failed calls still count when reserved.
- OpenRouter's October 2026 published free tier starts at 50 requests per day and 20 requests per minute; limits can change. Poolside Laguna S 2.1's free endpoint is scheduled to expire October 31, so RivaL must verify availability rather than assume it.
- Never log or reveal API keys, store them in the repository, expose upstream payload errors to end users, or mix independent currencies/venues without corresponding source records.

## Historical training and what remains

- **Already present**: offline monthly historical-research workflow, private Google Drive historical/model artifact readouts, causal live `walk_forward` model, ten-venue shadow challenger and strict forward/after-cost promotion gates.
- Existing live mathematical issuance processes up to 1,000 five-minute observations per stream and learns from causally observed labels. Backfilled six-month data are not authentic *live* issuance history. The private archive remains separate from the hot PostgreSQL store. More rows do not confer predictive accuracy.
- **New read-only offline validator**: `polymarket_contract_audit.audit_contract_predictions` requires an exact, unique 5m/15m event identity, on-window Chainlink TWAP open/close, real prediction timestamps **before** the contract opened, oracle observation after settlement, quote observed before the trade, paired candidate and baseline Brier scores, at least 150 unique markets across 21 UTC days, at least 80% contemporaneous quote coverage, and positive net share return after costs.
- The actual Polymarket rule for 5m/15m is **end TWAP >= start TWAP resolves Up**, including exact ties. A future five-minute *exchange candle* is not a 15-minute Polymarket contract and cannot be given that label. Offline retrospective weights can't simply be promoted to the live feature contract.
- The audit only evaluates supplied evidence; it **cannot authenticate** that the supplied Chainlink records, model issuance times, prices or Polymarket resolutions came from their claimed source. Its strongest result is `research_only`. Never automatically promote on a CSV, a backtest, self-reported LLM confidence or raw exchange-price history.
- **Remaining production work**: collect independently verified Polymarket contract and Chainlink TWAP outcome receipts; join with *actually issued* immutable candidate predictions and contemporaneous order-book quotes; run source-verified chronological train/validation/test splits, match exact feature/scoring contracts, compare same-event baseline with cost and stress scenarios, then gather sufficient forward evidence for a promotion decision. This repository update does not manufacture missing oracle labels, does not run historical six-month training on Discloud, and does not approve real wagers.

## Release checks

- GitHub CI tests catalog/paid-fallback rejection, AI budget/caching/invalid responses, isolated provider outage, and the exact Polymarket identity, tie, chronology, quote coverage and matched Brier/cost gates.
- GitHub merge is **not** Discloud redeployment. Verify the bot actually restarts on the merged SHA, loads the provided key, receives a genuinely free model list, and successfully generates a separate AI research response before calling it live.
