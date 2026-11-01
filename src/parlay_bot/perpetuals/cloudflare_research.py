"""Standalone, Cloudflare-hosted research reviewer for 512 MB RivaL.

Reuses the established persisted AI quota, caching, and validation path.
No model weights run locally; this does not control quantitative predictions.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

import httpx

from parlay_bot.perpetuals.gemini_research import ResearchUnavailable
from parlay_bot.perpetuals.openrouter_research import OpenRouterResearch

_LOG = logging.getLogger(__name__)
_MODEL = "@cf/meta/llama-3.2-3b-instruct"
_WORKER_URL = "https://rival-ai-research.staytrippynlw.workers.dev/v1/research"
_MAX_EVIDENCE_BYTES = 24000


class CloudflareResearch(OpenRouterResearch):
    """Explicit opt-in, fixed-model Worker route. No paid provider fallback."""

    def __init__(self, market, *, api_key: str | None, daily_max: int = 18,
                 cooldown_seconds: int = 3600,
                 client: httpx.AsyncClient | None = None) -> None:
        super().__init__(
            market, api_key=api_key, daily_max=daily_max,
            cooldown_seconds=cooldown_seconds, client=client,
        )
        # Never share accounting slots with OpenRouter or the old Gemini path.
        self.model = "rival-cloudflare-research"

    async def list_advertised_models(self) -> tuple[str, ...]:
        if not self.available:
            raise ResearchUnavailable("Cloudflare AI token is not configured.")
        # Pinned model verified against this account's Workers AI catalog.
        # This is a deployment identity, not a fresh model-quality test.
        return (_MODEL,)

    async def review_contract(self, evidence: dict) -> dict:
        """Market contract research requires a separate oracle-aware prompt."""
        raise ResearchUnavailable(
            "Cloudflare contract research is not enabled. "
            "The independent exchange-candle reviewer cannot validate oracle settlements."
        )

    async def _complete(
        self, *, selected: str, evidence: dict, instruction: str,
        now: datetime, fallbacks: tuple[str, ...] = (),
    ) -> dict:
        # Override the existing provider transport, not its budget, cache, or UI.
        # Never fall through to OpenRouter/Gemini or any paid endpoint.
        if selected != _MODEL or fallbacks or not self._key:
            raise ResearchUnavailable("Unapproved Cloudflare AI route")
        encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > _MAX_EVIDENCE_BYTES:
            raise ResearchUnavailable(
                "RivaL's verified market summary exceeds the free AI request limit. "
                "No AI inference was made; live mathematical learning continues."
            )
        try:
            response = await self._client.post(
                _WORKER_URL,
                headers={"Authorization": f"Bearer {self._key}",
                         "Content-Type": "application/json"},
                json={"asset": evidence.get("asset"), "evidence": evidence},
                follow_redirects=False,
            )
            response.raise_for_status()
            result = response.json()
            if (
                not isinstance(result, dict)
                or result.get("model") != _MODEL
                or result.get("uncalibrated") is not True
                or result.get("informational_only") is not True
                or result.get("direction") not in ("up", "down", "uncertain")
                or result.get("confidence") not in ("low", "medium", "high")
                or not isinstance(result.get("thesis"), str)
                or not result["thesis"].strip()
                or not isinstance(result.get("risk"), str)
                or not result["risk"].strip()
                or len(result["thesis"]) > 400
                or len(result["risk"]) > 250
            ):
                raise ValueError("invalid Cloudflare AI response")
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (401, 403, 404, 429, 500, 502, 503, 504):
                await asyncio.to_thread(
                    self.market.pause_ai_requests, model=self.model, now=now,
                    seconds=3600 if code in (401, 403, 404, 429) else 1800,
                )
            _LOG.warning("Cloudflare research failed HTTP %d", code)
            raise ResearchUnavailable(
                f"Cloudflare research unavailable (HTTP {code}). "
                "No LLM opinion changed an authoritative forecast."
            ) from exc
        except (httpx.RequestError, ValueError, KeyError, TypeError) as exc:
            _LOG.warning("Cloudflare research invalid type=%s", type(exc).__name__)
            raise ResearchUnavailable(
                "Cloudflare research response is unavailable or invalid; "
                "no LLM opinion was used."
            ) from exc
        return {
            "direction": result["direction"], "confidence": result["confidence"],
            "thesis": result["thesis"].strip(), "risk": result["risk"].strip(),
            "served_model": _MODEL,
        }
