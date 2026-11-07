# RivaL Intelligence: local-only inference

Effective November 2, 2026. RivaL Intelligence LFM3 on its own Discloud service is the **sole** AI inference provider.

## Deployment

- RIVAL_LOCAL_AI_TOKEN is the private shared credential for the separate LFM3 gateway.
- RIVAL_AI_PROVIDER=local is the default; disabled opts out. Old values such as gemini, openrouter and cloudflare are ignored for compatibility and never activate external inference. With a local token, old values still select the local LFM3 service.
- RIVAL_LLM_HYPOTHESES_ENABLED controls optional bounded, independently verified BTC/ETH/SOL/XRP feature proposals.
- Legacy provider API keys and provider cooldowns no longer participate in runtime configuration. Remove old secrets from Discloud environments when convenient, but do not expose or commit them. The local LFM3 budget and background opt-in DO participate (batch 2, 2026-11-06): 18 requests/day, 60 s minimum spacing, 3 per Discord requester per day (`rival_llm_daily_max`), and background LLM loops run only with `rival_llm_background_opt_in=true`.

## Behavior

Local LFM3 inference is capped at 18 requests/day with 60 s minimum spacing and 3 per Discord requester per day, enforced from the persisted quota tables before any inference runs; a cap hit returns the reason and retry time instead of running inference. There is no paid-token balance or one-hour request cooldown. The Discord client serializes inference in-process; the private CPU service admits only one inference at a time and enforces request size, generation length and timeout limits.

Only the authenticated private gateway is contacted. A busy 429 or invalid response fails closed, never calling external AI. If the local model is temporarily unavailable, the bot may return a strictly uncertain deterministic summary of independently verified data, **not** a second model. Cache identities distinguish real LFM3 inference from data-only explanations.

Existing PostgreSQL quota rows, old provider-specific research and historic forecast receipts are preserved for audit. The local provider reserves from the persisted quota tables on every LLM call (cached evidence summaries cost no quota) and never re-labels old results. Mathematical predictions, source-native historical data admission, contract-oracle settlement and prospective after-cost evidence remain authoritative; an LLM explanation is not a calibrated profitable signal. Polymarket-specific local contract AI is not implemented and no unrelated exchange candle output is substituted.


## Task-specific local inference profiles

The normal `/v1/research` endpoint stays brief at **140 generation / 4096 total context tokens**, while `/v1/hypothesis` remains **136 / 4096**. Neither uses any paid-token accounting. Both modes count against the 18/day local budget.

An explicitly requested `/v1/extended-research` call uses **384 generation / 6144 total context tokens**. It summarizes the same source-bound evidence with additional 4h observations and bounded historical bar counts, and proposes a short list of *unproven research checks*. Its suggestions cannot change trading decisions, forecast confidence, source qualification, model weights, or promotion gates. Both modes use the real deployed tokenizer to reject overfull prompts; no source evidence is silently truncated. The gateway still handles only one inference at a time.

**Production opt-in:** `RIVAL_LLM_CONTEXT_TOKENS=4096` (default) boots the existing lightweight llama.cpp configuration and rejects extended calls with HTTP 422. Set `RIVAL_LLM_CONTEXT_TOKENS=6144` **only after measuring memory headroom and latency on the actual Discloud host**. This controls the *real* `--ctx-size` of the backend and the gateway's matching extended-research admission. Only 4096 and 6144 are valid to prevent arbitrary RAM expansion. No external AI provider is used. Extended calls allow up to 90 seconds of CPU inference (plus transport overhead), so they are deliberately separate from low-latency Discord interactions.

**Benchmark each profile** using the existing synthetic, non-trading cases against a private deployed service. The benchmark measures response validity, evidence fidelity, future-direction abstention, failure rate, and p50/p95/max request latency. It cannot prove model accuracy or estimate peak RAM from network timings; measure Discloud RSS/peak process memory during the run before enabling extended mode.

```bash
cd services/rival-llm
python benchmark.py --profile research --repetitions 3 --output /tmp/rival-brief.json
# After explicitly enabling 6144 tokens on a test/canary host:
python benchmark.py --profile extended --repetitions 3 --output /tmp/rival-extended.json
python benchmark.py --compare-with /tmp/rival-brief.json --output /tmp/rival-extended.json
```

Do not enable extended research by default for Discord, or auto-promote this profile/model based on synthetic benchmark scores. First require no response-schema regressions, no safety/grounding regressions, acceptable host memory headroom and latency, plus successful authenticated LFM3 canary requests. Existing production `/v1/research` callers require no changes. Actual Discloud benchmark and deployment validation have not been performed by this code change.

## Acceptance

Ruff plus full PostgreSQL-backed pytest must pass; tests should prove more than 18 distinct local requests without quota writes, no external fallback, caching, evidence validation, local CPU admission, safe failures and private gateway authentication. After merge, require a green main build, deployed Discloud bot, authenticated standalone LFM3 health/research smoke and a new persisted hypothesis receipt before claiming full production operation.
