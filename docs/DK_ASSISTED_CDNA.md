# RivaL DraftKings Assisted Mode and CDNA public data boundary

Updated 2026-10-31. Owner objective: research DraftKings Predictions crypto
contracts inside the existing RivaL menu, reuse RivaL's independent exchange
mathematics/free AI, and do not place bets.

## Installed workflow

`/rival` → Crypto → DraftKings Predictions → choose BTC/ETH/SOL/XRP and
independent research horizon. Buttons:

1. **Find My Coin** automatically searches public Crypto.com Predictions events
   for the selected BTC/ETH/SOL/XRP coin on demand and lists up to two
   symbol-verified candidate contracts, with fresh quoted prices if available. An event search result is NOT an automatic DraftKings contract ID.
2. **Check CDNA Quote** reads one exact contract price from
   `GET https://data-api.crypto.com/api/v1/predictions/contracts/{symbol}/price`.
   Displays original bid, ask, source update age, market status and freshness.
3. **Independent AI** requests one free-only AI assessment from RivaL's
   existing exchange-data engine for the selected coin, then opens the result
   in the research panel. The AI engine chooses its supported horizons,
   which may differ from the selected DraftKings research window. RivaL does not send CDNA fields to
   OpenRouter, Gemini, any model or any statistical-learning pipeline.
4. **Open DraftKings Predictions** opens the external official website so the
   user can verify the actual DraftKings contract, settlement rules, price,
   fees and expiry. RivaL cannot place orders on DraftKings.

Provider calls occur only after an explicit owner action, following a Discord
application-owner check, in a private interaction. No automatic polling, paid
API keys, order authorization, persistence, Drive export, DM alert forwarding
or AI processing of the CDNA response. Source-symbol matching does **not**
establish an equivalence to a DraftKings listing. A price is not a forecast.

## Rights and guardrails

Official docs: https://data.crypto.com/docs
Official EULA: https://data.crypto.com/static/eula.pdf?v=eula_v3

The anonymous API permits personal noncommercial read-only access, with
100 requests/minute and 50,000/day per IP documented as of October 2026.
**Personal use does not grant permission to process CDNA market data in any
AI or statistical-learning technology.** EULA section 1(b)(x) prohibits that,
even if the application has only one user. The docs require an MDLA for
model training, commercial use, or redistribution. Source license and specific
permissions should be checked before any new feature that feeds, exports,
screenshots, scores or learns from provider data.

CDNA public read-only client intentionally has no access to a store, market
learner, prediction engine, history importer, or LLM. This cannot be bypassed
by setting an undocumented flag. To use CDNA as a model input, obtain an
appropriate license/permission, audit the granted rights, and implement a
separately reviewed, validated ingestion path.

- Do not advertise public Crypto.com prices as executable DraftKings quotes.
  Without matching exchange, product, payoff, oracle, symbol, close time,
  contract rules and current venue bid/ask and fees, no profit prediction.
- Backend request timeout 3.5 seconds; only direct GETs to documented public
  URL and bounded symbols. No arbitrary URLs or secrets.
- 12-second same-symbol/query cooldown; 5-minute global pause on 401/403/429;
  read-only responses are not saved. Quoted fresh at <=90 seconds and only
  active/open/trading market statuses.
- Owner check fails closed for team-owned applications with no matching
  application owner. This restriction is deliberate, not a networking error.

## Runtime verification checklist

- CI: Python 3.11, Ruff, full pytest and PostgreSQL suite on PR head.
- Main release: exact hash, Discloud deployment acceptance.
- Owner-only Discord canary: find BTC, inspect exact symbol, show timestamp
  and source status, switch coins, ask independent AI; verify no background
  provider calls, data uploads, training rows or AI prompts incorporate CDNA.
- Without legitimate DraftKings quotes and exact rules, output remains
  assisted research, NOT a verified contract-specific UP/DOWN call.
- Opening the original #138 venue-specific outcome-validation issue does not
  by itself prove four market engines are fully functional or profitable.

## Continuity

Task branch: `feat/rival-dk-assisted-cdna-personal-data-20261031`.
Existing unrelated OpenRouter free-model PR #136 is left unchanged.
Issue #138 remains open for full source-matched contract/outcome/fee proof.
Objective is owner-controlled read-only research and AI via *independent*
sources, with no automatic bets. Never claim CDNA data trained RivaL.
