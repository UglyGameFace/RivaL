# Preserve the free AI budget for explicit RivaL predictions

## October 30 production incident and root cause

The user only opened the market-selection screen but RivaL logged `Free AI budget or request spacing reached. Next request <t:1793491200:R>`. That retry stamp is **2026-11-01 00:00 UTC**, the UTC daily reset. It is consistent with RivaL's internally persisted 18-request-per-day reservation limit, **not confirmation of any OpenRouter account quota exhaustion**. The original warning conflated daily budget, minimum spacing, and provider-error pauses.

The live Polymarket board preview/15-second refresh called `PolymarketResearch.observe`, which previously spawned a *new experimental AI review* even when the user never asked for one. A second, four-hour scheduled exchange AI track could also use the same persisted quota when its legacy auto flag was on. Historical/math model research and settlement collection did not need these LLM reviews.

## Actual behavior change

- Background market picker, live Polymarket cards and tracked updates now use math-and-settlement research only. They no longer reserve AI requests by default.
- Deliberately pressing **View Contract Call** initiates an optional free AI review for a fresh verified contract; this still obeys existing free-only catalog, budget and 60-second request spacing. If the provider/account has no capacity, nothing is fabricated.
- Scheduled exchange AI reviews require BOTH the legacy `RIVAL_GEMINI_AUTO_ENABLED=true` AND the new `RIVAL_AI_BACKGROUND_OPT_IN=true`; the latter is off by default even on Discloud installations where the legacy value was explicitly true.
- On blocked requests, the precise RivaL local cause, today's reservation count, cap and UTC retry time are reported. This says nothing conclusive about provider account quota.
- No AI ledger reset, paid fallback, new notifications, trades, credentials, or model promotion. Live mathematical prediction, PolyBolt Chainlink TWAP reception and settlement learning remain independent.

## Acceptance

Run exact-head full Ruff, tests and PostgreSQL integration; verify merged-main CI, Discloud deployment, and confirm actual production AI quota counter does not move during passive board refreshes. Deliberate View Contract Call should consume at most the permitted explicit LLM request. Do not paste API secrets or publicly expose the persisted daily quota table.