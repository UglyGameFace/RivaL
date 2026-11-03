# RivaL Intelligence: local-only inference

Effective November 2, 2026. RivaL Intelligence LFM3 on its own Discloud service is the **sole** AI inference provider.

## Deployment

- RIVAL_LOCAL_AI_TOKEN is the private shared credential for the separate LFM3 gateway.
- RIVAL_AI_PROVIDER=local is the default; disabled opts out. Old values such as gemini, openrouter and cloudflare are ignored for compatibility and never activate external inference. With a local token, old values still select the local LFM3 service.
- RIVAL_LLM_HYPOTHESES_ENABLED controls optional bounded, independently verified BTC/ETH/SOL/XRP feature proposals.
- Legacy provider API keys, daily budgets, provider cooldowns and background opt-ins no longer participate in runtime configuration. Remove old secrets from Discloud environments when convenient, but do not expose or commit them.

## Behavior

There is no artificial 18-per-day cap, paid-token balance or one-hour request cooldown. The Discord client serializes inference in-process; the private CPU service admits only one inference at a time and enforces request size, generation length and timeout limits.

Only the authenticated private gateway is contacted. A busy 429 or invalid response fails closed, never calling external AI. If the local model is temporarily unavailable, the bot may return a strictly uncertain deterministic summary of independently verified data, **not** a second model. Cache identities distinguish real LFM3 inference from data-only explanations.

Existing PostgreSQL quota rows, old provider-specific research and historic forecast receipts are preserved for audit. The new local provider never reserves external quota slots or re-labels old results. Mathematical predictions, source-native historical data admission, contract-oracle settlement and prospective after-cost evidence remain authoritative; an LLM explanation is not a calibrated profitable signal. Polymarket-specific local contract AI is not implemented and no unrelated exchange candle output is substituted.

## Acceptance

Ruff plus full PostgreSQL-backed pytest must pass; tests should prove more than 18 distinct local requests without quota writes, no external fallback, caching, evidence validation, local CPU admission, safe failures and private gateway authentication. After merge, require a green main build, deployed Discloud bot, authenticated standalone LFM3 health/research smoke and a new persisted hypothesis receipt before claiming full production operation.
