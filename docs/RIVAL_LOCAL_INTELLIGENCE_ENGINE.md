# RivaL private local intelligence engine

## Current production handoff

- **GitHub:** The RivaL Intelligence Python entrypoint, separate Discloud ZIP, private gateway, and Cloudflare backup are implemented. The published standalone ZIP passed local packaging and GitHub clean-download checks.
- **Discloud:** The user's November 1 production logs proved Python `main.py` runs correctly, then stops with `ValueError: Missing RIVAL_LLM_SHARED_TOKEN`. That is a **missing private runtime credential**, not a remaining `discloud.config` parsing error.
- **Unverified:** LFM3 GGUF download, actual live model load, CPU speed, peak RAM, the private RivaL-to-LLM HTTP connection, and Cloudflare fallback in Discloud production. Do not mark these complete without observed evidence.
- **Isolation:** The original RivaL app remains at 512 MB with independent market scanning, predictive models, training, NO CALL and model-promotion controls. Do not connect the local LLM until it passes live tests.

## Private app settings

The separate Discloud application uses `services/rival-llm/` and this minimal `discloud.config`:

```ini
NAME=RivaL-Intelligence
TYPE=bot
MAIN=main.py
RAM=2450
VERSION=3.11
VLAN=true
HOSTNAME=rival-llm
AUTORESTART=true
```

No custom build or start command is needed: Discloud detects Python 3.11 using the root `main.py` and `requirements.txt`. The package includes a llama.cpp v0.8.0 CPU binary with required native libraries and no model weights. Do not mix this ZIP into the existing RivaL bot or any SniperPlug or Dank Shield project.

### Resolve missing `RIVAL_LLM_SHARED_TOKEN` (one-time)

**Preferred:** Configure a private runtime environment variable for the **RivaL-Intelligence** Discloud app:

```text
Name: RIVAL_LLM_SHARED_TOKEN
Value: <fresh token generated locally using secrets.token_urlsafe(48)>
```

Generate the value on a trusted device: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Do not commit, screenshot, or paste the value into a GitHub issue, PR, source code or logs. Restart the separate app after changing the secret.

**Alternative for hosts without an environment-variable editor:** On a version including the private `.env` loader, place a **private, untracked file named `.env`** at the application root (alongside `main.py`), with one line:

```dotenv
RIVAL_LLM_SHARED_TOKEN=<same freshly generated token>
```

The optional standard-library loader reads only this key, refuses symlinks, restricts file permissions to 0600, rejects malformed/duplicate values, and does not override Discloud-provided environment variables. Upload only to the private Discloud application, **never to GitHub, chat, or a distributable ZIP**. The deployed app must include the updated loader before the file alternative is effective.

A valid token is required even for local access because private VLAN access is not sufficient authentication. If missing, the application fails closed, and `AUTORESTART` may repeatedly restart it. Do not weaken this protection by hardcoding a shared default.

## Expected startup and validation

1. The startup process validates its token and local CPU executable.
2. It fetches exactly one official allowlisted GGUF (default LFM3-1.7B Q4_K_M), verifies advertised SHA256 and byte count, and stores it under `runtime/models/`. The roughly 1.1 GB model file is **disk**, not total runtime RAM. Do not rely on ZIP size to assess disk requirements.
3. It launches `llama-server` bound **only to loopback port 8766**, and the authenticated research gateway on private VLAN port 8765 (`http://rival-llm:8765`). The optional `GET /health` response is a readiness check, not independent production uptime proof.
4. From RivaL's private network, verify authorized `GET /v1/status`, then one `POST /v1/research` with read-only market evidence for BTC, ETH, SOL, or XRP. Never issue real trades or alter model governance.
5. Only after successful readiness and acceptable RAM/CPU levels, set the original bot's **separate** environment:
   ```dotenv
   RIVAL_AI_PROVIDER=local
   RIVAL_LOCAL_AI_TOKEN=<same private local token>
   RIVAL_CLOUDFLARE_AI_TOKEN=<existing separate Cloudflare secret if desired>
   RIVAL_OPENROUTER_DAILY_MAX=18
   RIVAL_GEMINI_AUTO_ENABLED=false
   RIVAL_AI_BACKGROUND_OPT_IN=false
   ```
   All unconfigured or unavailable local responses must leave existing mathematical learning and NO CALL behavior unchanged. `RIVAL_AI_PROVIDER=disabled` is the fast rollback.

## Resource, performance and security rules

- The configured memory allocation is 2,450 MB, *not* a verified measured RSS. One CPU model at a time, two threads, one concurrent generation, 4096 context, max 300 output tokens.
- The gateway now renders the active model chat template and checks actual token count before inference; it reserves 300 generation tokens plus a 96-token safety margin. Oversized evidence returns HTTP 422 `context_budget_exceeded` without model generation or Cloudflare fallback. No partial data is silently discarded. Source-specific compact retrieval is a separate improvement; an HTTP 422 means the current evidence cannot safely fit, not that its market conclusion is negative.
- The benchmark includes positive and negative historical trend examples in addition to abstention scenarios, and reports direction-correct rate separately from policy safety. These are *synthetic explanation checks*, never evidence of future trading accuracy. The local model must be tested on host before claiming benchmark improvements.
- Allowed research assets are BTC/ETH/SOL/XRP. Unsupported assets, missing evidence and unauthenticated requests are rejected without inference.
- Model-produced directional notes and low/medium/high confidence are **uncalibrated** and informational only. Never treat this as independently proved forecast accuracy or trading edge.
- Cloudflare's existing `rival-ai-research` Worker is a separate optional backup with its own D1 daily limit. Its inference allowance is shared across the Cloudflare account and paid settings require separate confirmation.
- The exact `RIVAL-LLM-UPLOAD-THIS-ZIP` artifact is generated in `.github/workflows/local-llm-discloud-package.yml` with an independent clean-download smoke test. Never embed `.env`, GGUF weights, source build tree or nested ZIPs.
- To compare LFM3-1.7B against Qwen3.5-2B Q4, benchmark sequentially on Ruby using `services/rival-llm/benchmark.py` and `benchmark_cases.json`. Compare evidence fidelity, refusals, latency, RAM and CPU. This is **research-quality measurement**, not future market performance or model promotion evidence.

## References

- [Discloud Python deployment](https://docs.discloud.com/en/how-to-host-using/dashboard)
- [Discloud private environment guidance](https://docs.discloud.com/en/faq/general-questions/.env-file)
- [LFM3 official GGUF](https://huggingface.co/LiquidAI/LFM3-1.7B-GGUF)
- [Qwen3.5-2B GGUF](https://huggingface.co/gguf/Qwen3.5-2B-GGUF/tree/main)
- [llama.cpp](https://github.com/ggml-org/llama.cpp)
