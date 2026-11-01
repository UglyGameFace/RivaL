"""Local LFM3 research first, optional existing free Cloudflare Worker as backup.

Inherits the already-authoritative user quota, cache and evidence assembly from
OpenRouterResearch. Neither model may alter scores, positions or promotions.
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx

from parlay_bot.perpetuals.cloudflare_research import CloudflareResearch
from parlay_bot.perpetuals.evidence_policy import enforce as enforce_grounding
from parlay_bot.perpetuals.gemini_research import ResearchUnavailable
from parlay_bot.perpetuals.local_llm_discovery import discover_private_llm
from parlay_bot.perpetuals.openrouter_research import OpenRouterResearch

_LOG = logging.getLogger(__name__)
LOCAL_MODEL = "LiquidAI/LFM3-1.7B-GGUF:Q4_K_M"
CLOUD_MODEL = "@cf/meta/llama-3.2-3b-instruct"
LOCAL_ENDPOINT = "http://rival-llm:8765/v1/research"
MAX_LOCAL_EVIDENCE_BYTES = 24_000


def validated(reply: object, expected: str) -> bool:
    return (
        isinstance(reply, dict)
        and reply.get("model") == expected
        and reply.get("uncalibrated") is True
        and reply.get("informational_only") is True
        and reply.get("direction") in ("up", "down", "uncertain")
        and reply.get("confidence") in ("low", "medium", "high")
        and isinstance(reply.get("thesis"), str)
        and 0 < len(reply["thesis"].strip()) <= 400
        and isinstance(reply.get("risk"), str)
        and 0 < len(reply["risk"].strip()) <= 250
    )


class LocalFirstResearch(OpenRouterResearch):
    """Only caller-chosen AI research, with one shared PostgreSQL quota slot."""

    def __init__(
        self, market, *, local_key: str | None,
        cloudflare_key: str | None = None, daily_max: int = 18,
        cooldown_seconds: int = 3600, client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            market, api_key=local_key, daily_max=daily_max,
            cooldown_seconds=cooldown_seconds,
            client=client or httpx.AsyncClient(
                timeout=httpx.Timeout(52), follow_redirects=False,
            ),
        )
        # super() sees the explicit client; this class closes its own client.
        self._owned_client = client is None
        self.model = "rival-local-cloudflare-shared-budget"
        self._peer_ip: str | None = None
        self._backup = (
            CloudflareResearch(
                market, api_key=cloudflare_key, daily_max=daily_max,
                cooldown_seconds=cooldown_seconds, client=self._client,
            )
            if cloudflare_key else None
        )

    async def list_advertised_models(self) -> tuple[str, ...]:
        if not self.available:
            raise ResearchUnavailable("Private local LLM token is not configured.")
        # Fixed only after operator deploys matching local model; not a
        # diagnostic claim that the app is currently running.
        return (LOCAL_MODEL, CLOUD_MODEL) if self._backup else (LOCAL_MODEL,)

    async def review_contract(self, evidence: dict) -> dict:
        raise ResearchUnavailable(
            "Local LLM cannot verify Polymarket oracle settlements. "
            "Contract-specific research requires authoritative contract evidence."
        )

    async def _complete(
        self, *, selected: str, evidence: dict, instruction: str,
        now, fallbacks: tuple[str, ...] = (),
    ) -> dict:
        if selected != LOCAL_MODEL or fallbacks not in ((), (CLOUD_MODEL,)):
            raise ResearchUnavailable("Unapproved local/backup AI model route")
        if len(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")) > MAX_LOCAL_EVIDENCE_BYTES:
            raise ResearchUnavailable("Verified evidence too large for local inference.")
        # The original OpenRouterResearch.ask() has already reserved ONE shared,
        # user-fair, persisted request for this entire local-then-backup attempt.
        try:
            headers = {"Authorization": f"Bearer {self._key}",
                       "Content-Type": "application/json"}
            payload = {"asset": evidence.get("asset"), "evidence": evidence}
            endpoint = (f"http://{self._peer_ip}:8765/v1/research"
                        if self._peer_ip else LOCAL_ENDPOINT)
            try:
                response = await self._client.post(
                    endpoint, headers=headers, json=payload, follow_redirects=False)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                # Retry *only* a connection that never completed, not a model
                # timeout or HTTP failure (which may already have issued research).
                # No paid API, static container IP, or Discloud owner credential.
                self._peer_ip = None
                peer = await asyncio.to_thread(discover_private_llm, self._key)
                if peer is None:
                    raise
                self._peer_ip = peer
                response = await self._client.post(
                    f"http://{peer}:8765/v1/research", headers=headers,
                    json=payload, follow_redirects=False)
            if response.status_code in (502, 503, 504):
                raise httpx.HTTPStatusError(
                    "local temporarily unavailable",
                    request=response.request, response=response,
                )
            response.raise_for_status()
            data = response.json()
            if not validated(data, LOCAL_MODEL):
                raise ValueError("invalid local model output")
            # Repeat the guard in RivaL: valid JSON from an outdated gateway
            # still cannot assert a future direction without actual proof.
            grounded = enforce_grounding(data, evidence, asset=evidence.get("asset"))
            return {
                "direction": grounded["direction"], "confidence": grounded["confidence"],
                "thesis": grounded["thesis"], "risk": grounded["risk"],
                "served_model": LOCAL_MODEL,
            }
        except httpx.HTTPStatusError as exc:
            # An authentication error, malformed client request, or BUSY is
            # not permission to spend a second provider's capacity.
            if exc.response.status_code not in (502, 503, 504):
                _LOG.warning("Local AI rejected request HTTP %s", exc.response.status_code)
                raise ResearchUnavailable(
                    f"Local AI rejected research (HTTP {exc.response.status_code})."
                ) from exc
            _LOG.warning("Local inference unavailable HTTP %s", exc.response.status_code)
        except httpx.RequestError:
            _LOG.warning("Private local AI network unavailable")
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            _LOG.warning("Local AI response invalid class=%s", type(exc).__name__)
            raise ResearchUnavailable(
                "Local model produced invalid research; not sending malformed data "
                "to any fallback."
            ) from exc

        if self._backup is None or fallbacks != (CLOUD_MODEL,):
            await asyncio.to_thread(
                self.market.pause_ai_requests, model=self.model, now=now, seconds=1800,
            )
            raise ResearchUnavailable(
                "Local AI unavailable; no Cloudflare backup configured. "
                "Mathematical collection and learning continue."
            )
        # Cloudflare's separate D1 24/day cap is also enforced by the Worker.
        # One backup attempt, never unbounded retries and never paid fallback.
        _LOG.warning("Using explicit Cloudflare research fallback")
        try:
            return await self._backup._complete(
                selected=CLOUD_MODEL, evidence=evidence, instruction=instruction,
                now=now, fallbacks=(),
            )
        except ResearchUnavailable:
            await asyncio.to_thread(
                self.market.pause_ai_requests, model=self.model, now=now, seconds=1800,
            )
            raise
