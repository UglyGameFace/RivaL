# RivaL local intelligence engine: Ruby deployment and evidence gates

**Status (2026-11-01):** Code + independent Cloudflare backup Worker exist. Private Discloud LLM is **not deployed**: no Discloud control API connector, host disk/memory and first real inference remain unverified. RivaL's current 512 MB app is untouched; default AI provider remains OpenRouter.

## Architecture and separation

- Existing RivaL app: \`RAM=512\`, \`VLAN=true\`, \`HOSTNAME=rival-bot\`.
- New **separate** Discloud application: [services/rival-llm](../services/rival-llm), \`RAM=2450\`, \`VLAN=true\`, \`HOSTNAME=rival-llm\`, \`TYPE=bot\`. No public website or external port. The private gateway listens on TCP 8765; llama.cpp listens ONLY on loopback 8766.
- Service has **no** database credentials, exchange keys, Polymarket keys, Discord token, trading authority, or ability to alter model weights. Only the RivaL bot accesses already-assembled read-only evidence.
- Primary: official Liquid AI LFM3-1.7B Q4_K_M (~1.1GB weight file, total RAM depends on CPU/runtime/context). One parallel request, 2 threads, 4096 context, 300 max output tokens, CPU only.
- Optional comparison mode: Qwen3.5-2B Q4_K_M selected by \`RIVAL_LLM_PROFILE=qwen2b\`, never both memory-heavy models concurrently.
- Existing isolated Cloudflare Worker is **backup only** for local timeouts/transport failure/502/503/504. No fallback for local authentication errors, local overload (429), or malformed output.
- Existing SQL stored budget: 18 explicit user-requested local-first research requests/day with 3600-second cooldown (including those using backup), per-user fairness and cache, plus the standalone Cloudflare D1 cap of 24 attempted requests/day. Other Cloudflare account apps share the underlying Workers AI daily free allocation. Do **not** enable unattended LLM calls unless separately justified.
- No change to mathematical learning, native history, quote-scoring, model-family promotion, or NO CALL. This AI is an *unverified research explainer* only.

## Deployment artifact

GitHub Actions [local-llm-discloud-package.yml](../.github/workflows/local-llm-discloud-package.yml) builds \`llama-server\` from llama.cpp v0.8.0 (LFM3 support) and packages **only** this separate app's Python sources, config, CPU executable and shared libraries. It never downloads or packages GGUF model weights or private tokens.

The archive is \`rival-llm-discloud.zip\`. It must be deployed as **its own new Discloud application**, not uploaded as an update to the existing RivaL application. If GitHub prebuilds the executable successfully, Discloud's \`build.sh\` detects and reuses it; otherwise a local C++ compiler, git, cmake and sufficient build resources are required. Do not replace or modify any other app.

At first start, the gateway fetches only the allowlisted official quantized GGUF over TLS into \`runtime/models/\`, checks the exact Hugging Face LFS SHA256 and byte size, and atomically publishes it. It checks the SHA again at every restart. A corrupt model, unavailable API, missing executable, weak/missing token, or unavailable backend **fails closed** rather than using an unknown model. First-run disk space and download volume are not included in the ~2GB CPU memory estimate; verify Discloud persistent storage and network availability.

An \`RIVAL_LLM_SHARED_TOKEN\` of at least 32 random characters must be set in **both** Discloud applications using private environment settings, never committed. Use a secure random generator (e.g. \`python -c 'import secrets; print(secrets.token_urlsafe(48))'\`) on a trusted device and transfer privately.

New local model app settings:

\`\`\`dotenv
RIVAL_LLM_SHARED_TOKEN=<private long random token>
RIVAL_LLM_PROFILE=lfm3
RIVAL_LLM_PORT=8765
# Optional reproducible official source pin after verifying Hugging Face LFS metadata:
# RIVAL_MODEL_REVISION=<pinned revision>
# RIVAL_MODEL_SHA256=<64-character published LFS hash>
\`\`\`

Existing **RivaL** Discloud app settings, only **after** the local service and tests pass:

\`\`\`dotenv
RIVAL_AI_PROVIDER=local
RIVAL_LOCAL_AI_TOKEN=<same private local token>
RIVAL_CLOUDFLARE_AI_TOKEN=<existing separate Cloudflare secret if backup desired>
RIVAL_OPENROUTER_DAILY_MAX=18
RIVAL_OPENROUTER_COOLDOWN_SECONDS=3600
RIVAL_GEMINI_AUTO_ENABLED=false
RIVAL_AI_BACKGROUND_OPT_IN=false
\`\`\`

Leave \`RIVAL_AI_PROVIDER=openrouter\` until the new service is responding. Disabling local AI is immediate via \`RIVAL_AI_PROVIDER=disabled\`; this does not disable source collection or mathematical learning. Cloudflare-only mode remains \`RIVAL_AI_PROVIDER=cloudflare\`.

## Acceptance, no made-up performance

1. GitHub PR test: Ruff and PostgreSQL/SQLite tests green on the exact PR head. Independent package job produces a verified binary ZIP and the separate app boots with only a private secret.
2. Deployment receipt: Discloud confirms full 2450 MB allocation, storage for GGUF and backup capacity for other projects; \`rival-llm:8765/health\` is \`ready\`. Verify bind to loopback on 8766 and no public route.
3. Authorized private API call: RivaL's existing request budget saves only validated research, uses a single token, and does not modify model predictions. Test local failure with Cloudflare fallback once, without duplicate quota or public secrets.
4. Benchmark **sequentially** with LFM3 and Qwen on the same CPU/concurrency/context and the synthetic [benchmark cases](../services/rival-llm/benchmark_cases.json). Keep reports with model ID, context, latency median/max, structured validity, safety, evidence fidelity, RSS and allocation headroom. Human review and future forward validation are needed before any performance claim.
5. No automatic switching of forecasting models or training on model-generated pseudo-labels. Fine-tuning is optional, conducted off the Ruby production host only after an auditable dataset of genuine source-labelled examples is available and independent held-out evaluations show improvement.

### Operator-side benchmark (local service only)

\`\`\`bash
cd services/rival-llm
# Run first on LFM3:
python benchmark.py --endpoint http://rival-llm:8765 --output lfm3.json
# Restart only the isolated LLM service with RIVAL_LLM_PROFILE=qwen2b:
python benchmark.py --endpoint http://rival-llm:8765 --output qwen2b.json
# Compare the two completed reports:
python benchmark.py --compare-with lfm3.json --output qwen2b.json
\`\`\`

Benchmark scores measure *research response behavior*, not the probability of a profitable position. No threshold relaxations or after-cost edge are claimed by these results.

## Licenses and sources

- LFM3 weights: [Liquid AI GGUF](https://huggingface.co/LiquidAI/LFM3-1.7B-GGUF), LFM Open License v1.0; license notices must be retained. Under the published terms, commercial licensing becomes necessary above the stated business revenue threshold.
- Qwen comparison weights: [GGUF Qwen3.5-2B](https://huggingface.co/gguf/Qwen3.5-2B-GGUF), original model subject to its associated license.
- llama.cpp: [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp), v0.8.0 or newer for LFM3.
- Private VLAN: [Discloud VLAN docs](https://docs.discloud.com/en/configurations/discloud.config/vlan).

## Reconciliation / ownership

This extends the existing [RivaL Cloudflare backup PR #193](https://github.com/UglyGameFace/RivaL/pull/193) instead of introducing another competing AI branch. The independent Gate archive [PR #192](https://github.com/UglyGameFace/RivaL/pull/192) remains unaffected. The separate Discloud runtime is not automatically deployable with the connected GitHub/Cloudflare tools; avoid claiming a live local model or a production-approved research advantage until verified.
