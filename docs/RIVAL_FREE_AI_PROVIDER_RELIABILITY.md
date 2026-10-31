# Free AI provider reliability after October 30 upstream 503 incident

## Incident

OpenRouter's upstream request log for the RivaL API key showed many HTTP **503** responses from Recursive's Flint model. 503 is upstream service unavailability, not proof of exhausted account quota. Failed free-tier attempts can still consume the account's daily request allowance. RivaL's own historical 18/day local budget may also have been consumed by previously unattended reviews; PR #135 already stopped automatic LLM spending by default.

## Current behavior

- Retire the broken Recursive Flint model from both primary and fallback selections. Pick the highest-priority **currently catalog-verified** zero-price text chat coding/reasoning model from a small curated allowlist: OpenAI gpt-oss-120b, Poolside Laguna S 2.1, NVIDIA Nemotron 3.5 Lightning, or Cohere North Mini Code. The actual availability changes, and obsolete/expired models are automatically rejected.
- Never use a paid or unverified model. All candidate IDs must end in `:free`, and the official catalog must advertise zero prompt, output and request charge with text in/out and an unexpired model.
- Each explicit AI request uses **one** OpenRouter API call with at most two approved free model IDs in the official `models` fallback array. The provider may try the second free model only if the first fails. It may count multiple upstream attempts toward the OpenRouter free cap; fallback is deliberately bounded to control request usage.
- Validate the actual `model` returned in the completion and save its identity to the existing audit ledger, even if the request used a fallback. Reject any unadvertised, stale, paid or mismatched model response.
- After 5xx provider/service failures, pause further **RivaL** free reviewer requests for 30 minutes using the existing persistent quota circuit breaker. 401/402/403/404/429 continue to pause one hour. Network transport errors also pause for 30 minutes. Do not endlessly retry or claim that a 503 means user quota exhaustion.
- Keep free AI **optional and user initiated** by default, as fixed in PR #135. The mathematical exchange, Kalshi and Polymarket models, verified settlement checks and PolyBolt Chainlink oracle ingestion continue without remote LLM requests. An AI essay is never evidence of calibrated trading probabilities or after-cost gains.

## Why not Hugging Face as an unlimited replacement?

Hugging Face Inference Providers currently gives a **small monthly hosted inference credit** (roughly $0.10 to free users, subject to change), not unrestricted ongoing inference. Paid compute is required after credits are exhausted. Free hosted compute and provider throttling can change; no public serverless provider offers an unconditional production-grade unlimited service.

Self-hosting downloaded open-weight models eliminates *external per-call API quotas*, but requires sufficient CPU/GPU memory, a viable model license, monitoring and local compute costs. That is not the same as a zero-cost, unlimited hosted service on a small Discloud container. RivaL does not silently download model weights or add that infrastructure here.

## Validation

CI should pass Ruff, tests and PostgreSQL integration before merge. Regression tests check model catalog filtering, priority, use of at most two free-only fallbacks, correct actual model attribution, provider 503 pause and paid-model rejection. Main CI and matching Discloud deployment are separate gates. Genuine production stability requires looking at sanitized provider logs after explicit calls and ensuring unsolicited request count stays zero; existing daily spent request history is not reset.