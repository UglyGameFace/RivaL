"""Optional Gemini research track, separate from deterministic forecast scoring.

Uses ALL currently healthy, distinct, recent RivaL venue summaries from
PostgreSQL, not direct exchange scraping. The cache and quota are also in
PostgreSQL, so button spam and bot restarts do not reset the usage budget.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from parlay_bot.perpetuals.analysis_evidence import build_evidence
from parlay_bot.perpetuals.market_store import PerpetualMarketStore

_LOG = logging.getLogger(__name__)
_MODEL = re.compile(r"^[a-z0-9][a-z0-9.\-]{2,63}$")


class ResearchUnavailable(ValueError):
    """A fail-closed answer. No pretending this is a validated trade."""


@dataclass(frozen=True)
class AiResearch:
    asset: str
    model: str
    generated_at: datetime
    direction: str
    confidence: str
    thesis: str
    risk: str
    source_count: int
    evidence_sha256: str
    from_cache: bool = False


class GeminiResearch:
    def __init__(
        self, market: PerpetualMarketStore, *, api_key: str | None,
        model: str = "gemini-2.5-flash", daily_max: int = 24,
        cooldown_seconds: int = 900,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not _MODEL.fullmatch(model) or not 1 <= daily_max <= 96:
            raise ValueError("invalid Gemini configuration")
        if not 60 <= cooldown_seconds <= 86400:
            raise ValueError("invalid Gemini cooldown")
        self.market = market
        self._key = api_key
        self.model = model
        self.daily_max = daily_max
        self.cooldown_seconds = cooldown_seconds
        self._client = client or httpx.AsyncClient(timeout=25, follow_redirects=False)
        self._owned_client = client is None
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return bool(self._key)

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    async def ask(self, *, asset: str, venues: tuple[str, ...]) -> AiResearch:
        """Only called by an explicit Discord button; zero background LLM calls."""
        if not self._key:
            raise ResearchUnavailable(
                "Gemini key not available to the RivaL process on Discloud."
            )
        async with self._lock:
            evidence, digest = await asyncio.to_thread(
                build_evidence, self.market, asset=asset, venues=venues
            )
            cached = await asyncio.to_thread(
                self.market.cached_ai_research, asset=asset,
                model=self.model, evidence_sha256=digest,
            )
            if cached:
                return AiResearch(
                    asset=asset, model=self.model,
                    generated_at=datetime.fromisoformat(cached["issued_at"]),
                    direction=cached["direction"], confidence=cached["confidence"],
                    thesis=cached["thesis"], risk=cached["risk"],
                    source_count=int(cached["source_count"]),
                    evidence_sha256=digest, from_cache=True,
                )
            now = datetime.now(UTC)
            permitted = await asyncio.to_thread(
                self.market.reserve_ai_request,
                model=self.model, now=now,
                daily_max=self.daily_max,
                cooldown_seconds=self.cooldown_seconds,
            )
            if not permitted:
                raise ResearchUnavailable(
                    "Gemini request budget/cooldown reached. "
                    "Existing market collection continues normally."
                )
            prompt = (
                "You are an INDEPENDENT research model for perpetual futures. "
                "Read every healthy exchange summary supplied. Compare 15m/1h/"
                "4h/24h returns, volatility, disagreement and missing windows. "
                "Do not treat USD, USDC and USDT quotes as perfectly fungible. "
                "These are independently sampled 5-minute candles, not a trained "
                "model or a guaranteed betting edge. Do not invent absent fields, "
                "funding, macro events or full six-month histories. "
                "Return strict JSON: direction (up/down/uncertain), "
                "confidence (low/medium/high, uncalibrated), thesis (under "
                "400 characters), risk (under 250 characters). Prefer uncertain "
                "when data or disagreement warrants it. Prediction horizon 15m. "
                "Never claim results from data you were not given.\n"
                + json.dumps(evidence, sort_keys=True, separators=(",", ":"))
            )
            try:
                response = await self._client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{self.model}:generateContent",
                    headers={
                        "x-goog-api-key": self._key,
                        "Content-Type": "application/json",
                    },
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {
                            "temperature": 0,
                            "responseMimeType": "application/json",
                            "maxOutputTokens": 650,
                        },
                    },
                    follow_redirects=False,
                )
                response.raise_for_status()
                raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise TypeError("non-object Gemini output")
                direction, confidence = result.get("direction"), result.get("confidence")
                thesis, risk = result.get("thesis"), result.get("risk")
                if (
                    direction not in ("up", "down", "uncertain")
                    or confidence not in ("low", "medium", "high")
                    or not isinstance(thesis, str) or not thesis.strip()
                    or not isinstance(risk, str) or not risk.strip()
                ):
                    raise ValueError("unvalidated Gemini output")
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if code in (401, 403, 429):
                    await asyncio.to_thread(
                        self.market.pause_ai_requests, model=self.model,
                        now=now, seconds=3600,
                    )
                _LOG.warning("RivaL Gemini unavailable, HTTP %d", code)
                raise ResearchUnavailable(
                    f"Gemini unavailable (HTTP {code}); no AI forecast generated."
                ) from exc
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                _LOG.warning("RivaL Gemini response rejected: %s", type(exc).__name__)
                raise ResearchUnavailable(
                    "Gemini response invalid/unavailable. No AI forecast generated."
                ) from exc
            thesis, risk = thesis.strip()[:400], risk.strip()[:250]
            await asyncio.to_thread(
                self.market.save_ai_research,
                asset=asset, model=self.model, evidence_sha256=digest,
                issued_at=now, direction=direction, confidence=confidence,
                thesis=thesis, risk=risk, source_count=len(evidence["sources"]),
            )
            return AiResearch(
                asset=asset, model=self.model, generated_at=now,
                direction=direction, confidence=confidence,
                thesis=thesis, risk=risk,
                source_count=len(evidence["sources"]),
                evidence_sha256=digest,
            )
