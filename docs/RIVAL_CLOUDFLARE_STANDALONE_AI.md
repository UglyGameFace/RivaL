# Standalone RivaL Cloudflare AI reviewer (2026-11-01)

## Deployment and isolation

- Cloudflare Worker: `rival-ai-research` at `https://rival-ai-research.staytrippynlw.workers.dev`.
- Cloudflare D1 budget database: `rival-ai-usage-ledger`, schema in `infra/cloudflare-rival-ai/schema.sql`.
- Worker source, deployment configuration and data schema are versioned under `infra/cloudflare-rival-ai/`.
- No SniperPlug routes, Pages projects, bindings, credentials, or website files are involved.
- The model is Cloudflare-hosted `@cf/meta/llama-3.2-3b-instruct`. The previously discussed Qwen3.5 2B is **not** available in the connected Workers AI catalog.
- Workers AI is account-wide, not an exclusive RivaL allowance. The Worker limits itself to **24 attempted calls per UTC day** with a maximum 24,000-character evidence payload and 320 output tokens. D1 transactionally reserves the attempted call *before* inference. D1 database failure denies requests.
- For any future redeploy using Wrangler, preserve the dedicated secret with `wrangler secret put RIVAL_AI_SHARED_TOKEN` in the `infra/cloudflare-rival-ai` directory before allowing traffic. An omitted or blank secret intentionally denies all inference requests.
- Requests are authenticated with a dedicated bearer secret in the Worker environment. Never commit, print, or log it. Only `GET /health` is public; `GET /v1/status` and `POST /v1/research` require authorization. No browser CORS policy grants third-party access.
- Cloudflare's Workers AI free allowance is 10,000 neurons per day *shared across the account*, reset 00:00 UTC. **The 24-call cap is an additional guard, not a guarantee of $0 if this account uses Workers Paid or other AI traffic consumes the free allowance first.** Check the account's billing plan and daily usage before enabling calls.

## RivaL activation on Discloud

RivaL remains in its original 512 MB allocation. After this PR is merged and deployed, configure its existing Discloud application environment (not SniperPlug's):

```dotenv
RIVAL_AI_PROVIDER=cloudflare
RIVAL_CLOUDFLARE_AI_TOKEN=<dedicated Worker bearer secret>
RIVAL_OPENROUTER_DAILY_MAX=18
RIVAL_OPENROUTER_COOLDOWN_SECONDS=3600
RIVAL_GEMINI_AUTO_ENABLED=false
RIVAL_AI_BACKGROUND_OPT_IN=false
```

A separate human-authorized production secret update is necessary. Existing OpenRouter and Gemini settings remain untouched, and the default provider remains `openrouter` until explicitly selected. The dedicated Worker token must **not** be added to repository files, GitHub PRs, issues, or logs.

The RivaL adapter inherits its existing persisted per-user and shared safety quota, cached evidence and uncalibrated-result validation. It never changes the independent forecast, NO CALL, price targets, model promotion, or any real trade. Experimental Polymarket contract review is deliberately disabled for this Worker because exchange-candle commentary is not settlement-oracle evidence.

## Acceptance / rollback

- Verify the Worker deployment has `AI`, `DB` and `RIVAL_AI_SHARED_TOKEN` bindings and its own workers.dev subdomain.
- Confirm unauthenticated `/v1/research` returns 401 and authorized `/v1/status` reports D1 usage; test one *explicit* inference only once account billing and daily quota are confirmed.
- Confirm no secret in logs and one request is charged in the D1 ledger. Confirm 429 when the cap is reached and no fallback to other AI providers.
- Confirm one explicit RivaL research call, cache reuse, Discord response and same 512 MB Discloud memory limit. Do not call this production-ready until verified on the host.
- If errors occur, set `RIVAL_AI_PROVIDER=disabled` (or revert to the previous provider) without changing market collection or deterministic learning. This does **not** delete the separate Cloudflare Worker or database.

No hosted LLM is inherently better at actual trading forecasts. Model commentary is non-authoritative research and its confidence is uncalibrated.
