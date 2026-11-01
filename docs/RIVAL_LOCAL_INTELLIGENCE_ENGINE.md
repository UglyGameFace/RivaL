# RivaL private local intelligence engine

## Current production handoff

- **GitHub:** The RivaL Intelligence Python entrypoint, separate Discloud ZIP, private gateway, and Cloudflare backup are implemented. The published standalone ZIP passed local packaging and GitHub clean-download checks.
- **Discloud:** Earlier November 1 logs stopped on `ValueError: Missing RIVAL_LLM_SHARED_TOKEN`. **Later logs at 11:16 showed `local_gateway_start model=lfm3` and `llama_server: initializing`, which means that run got past credential validation, GGUF verification and subprocess startup.** The CPU/GPU notice is expected for CPU-only hosting. Startup does not prove the model reached ready state or completed an authenticated research request.
- **Unverified:** Complete backend readiness, authenticated model response, CPU speed, peak RAM, the private RivaL-to-LLM HTTP connection, and Cloudflare fallback in Discloud production. The latest logs establish startup and a verified model file path, not end-to-end acceptance. Do not mark these complete without observed evidence.
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

No custom build or start command is needed: Discloud detects Python 3.11 using the root `main.py` and `requirements.txt`. The updated package builds the pinned llama.cpp b12024 CPU binary with required native libraries and no model weights. Do not mix this ZIP into the existing RivaL bot or any SniperPlug or Dank Shield project.

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

## Production inference incident, November 1, 2026

Discloud independent diagnostics confirmed the standalone LFM3 gateway reaches readiness and the main RivaL process authenticates to it over the current eth0 container address. The documented VLAN aliases still do not resolve even after restarting **only** RivaL-Intelligence. The eth1 route timed out; do **not** hardcode the observed eth0 IP as a permanent endpoint, as container addresses can change. Production model choice remains unchanged until stable discovery is established.

The earlier 503 on actual research was a verified `TimeoutError`: 185 prompt tokens required about 9.95 seconds, followed by 300 completion tokens in 30.23 seconds, which exceeded the 40-second upstream deadline. A 180-token direct probe took 28.49 seconds but returned an empty assistant `content` and `finish_reason=length`. This is not successful research. The LFM3 model defaults to thinking, and upstream llama.cpp fixed JSON-grammar interactions with thinking suppression in #31374/#31380. The standalone package therefore pins b12024, sets the server reasoning budget to zero, passes `enable_thinking=false` to requests, uses a 220-token completion budget and rejects truncated or empty answers. These are implementation changes, **not validated production recovery** until the updated artifact is deployed and an authenticated real reply succeeds.

## Private service discovery fallback

When Discloud's configured VLAN hostnames are missing from embedded Docker DNS, the RivaL process attempts the documented `rival-llm` hostname first, then performs **bounded, HMAC-SHA256 authenticated UDP discovery** over its own attached account VLAN interfaces only if an HTTP connection could not be established. The LLM gateway answers UDP port 29785 only for requests signed with the existing `RIVAL_LLM_SHARED_TOKEN`. The client verifies a fresh random nonce, signed reply, and local-subnet membership, then continues using bearer-authenticated HTTP port 8765. UDP discovery never triggers inference, opens a public site, or sends credentials. Cached peer addresses are discarded after a connect error and rediscovered, accommodating Discloud's changing interface order and IPs. Any failure closes safely and keeps existing quotas, optional Cloudflare fallback and NO CALL unchanged. This app-side fallback does not fix Discloud's broken DNS itself; Issue #210 remains open for host-side registration.
