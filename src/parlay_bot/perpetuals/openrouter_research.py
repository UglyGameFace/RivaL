"""Strictly zero-cost OpenRouter coding-model reviewer of verified RivaL evidence.

Coding LLMs can *explain* deterministic math; their prose NEVER drives
model weights, promotion, Polymarket verdicts, or trade execution.
Select only a live catalog-advertised :free model with $0 model charges.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx

from parlay_bot.perpetuals.analysis_evidence import build_evidence
from parlay_bot.perpetuals.gemini_research import AiResearch, ResearchUnavailable
from parlay_bot.perpetuals.market_store import PerpetualMarketStore

_LOG = logging.getLogger(__name__)
_CATALOG = "https://openrouter.ai/api/v1/models"
_COMPLETE = "https://openrouter.ai/api/v1/chat/completions"
# Curated for code/analysis tasks; these are NOT trading-accuracy benchmarks.
# A listed model is always catalog-verified, never assumed available.
_CODING_MODELS = (
    "cohere/north-mini-code:free",
    "poolside/laguna-s-2.1:free",
    "qwen/qwen3-coder:free",
    "mistralai/devstral-2512:free",
)
_BUDGET_ID = "rival-openrouter-free-coding"
_FREE_DAILY_MAX = 18  # Below standard OpenRouter 50/day :free quota.
_CACHE_TTL = timedelta(hours=1)


def _zero(value: object) -> bool:
    try:
        number = Decimal(str(value))
        return number.is_finite() and number == 0
    except (InvalidOperation, TypeError, ValueError):
        return False


def _unexpired(value: object, now: datetime) -> bool:
    if value is None:
        return True
    try:
        if isinstance(value, (float, int)):
            return datetime.fromtimestamp(value, UTC) > now
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC) > now
    except (ValueError, OverflowError, OSError, TypeError):
        pass
    return False


def eligible_free_coding_models(payload: object, *, now: datetime) -> tuple[str, ...]:
    """Reject paid/expired/non-chat catalog entries even if named 'free'."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ResearchUnavailable("OpenRouter catalog unavailable or invalid.")
    observed: set[str] = set()
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if model_id not in _CODING_MODELS:
            continue
        price = item.get("pricing")
        architecture = item.get("architecture")
        endpoints = item.get("endpoints")
        if (
            not isinstance(price, dict)
            or not all(_zero(price.get(k)) for k in ("prompt", "completion"))
            or not _zero(price.get("request", "0"))
            or not isinstance(architecture, dict)
            or "text" not in architecture.get("output_modalities", [])
            or not isinstance(endpoints, list)
            or "/api/v1/chat/completions" not in endpoints
            or not _unexpired(item.get("expiration_date"), now)
        ):
            continue
        observed.add(model_id)
    return tuple(m for m in _CODING_MODELS if m in observed)


class OpenRouterResearch:
    """Compatibility adapter for the existing research/automatic-AI track.

    Only ONE research provider is instantiated, and every model uses the same
    persisted account-level request ledger to avoid model-switch quota resets.
    """

    def __init__(
        self, market: PerpetualMarketStore, *, api_key: str | None,
        daily_max: int = _FREE_DAILY_MAX, cooldown_seconds: int = 3600,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not 1 <= daily_max <= _FREE_DAILY_MAX:
            raise ValueError("OpenRouter free reviewer daily cap must be <=18")
        if not 60 <= cooldown_seconds <= 86400:
            raise ValueError("invalid OpenRouter cooldown")
        self.market = market
        self._key = api_key
        self.model = _BUDGET_ID  # Shared stable ID for automated slot + quota.
        self.daily_max = daily_max
        self.cooldown_seconds = cooldown_seconds
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(16), follow_redirects=False,
        )
        self._owned_client = client is None
        self._lock = asyncio.Lock()
        self._models: tuple[str, ...] = ()
        self._catalog_at: datetime | None = None

    @property
    def available(self) -> bool:
        return bool(self._key)

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    async def list_advertised_models(self) -> tuple[str, ...]:
        if not self._key:
            raise ResearchUnavailable("OpenRouter API key is not configured.")
        now = datetime.now(UTC)
        if (
            self._catalog_at is not None
            and now - self._catalog_at < _CACHE_TTL
            and self._models
        ):
            return self._models
        try:
            response = await self._client.get(
                _CATALOG, headers={"Authorization": f"Bearer {self._key}"},
            )
            response.raise_for_status()
            models = eligible_free_coding_models(response.json(), now=now)
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            _LOG.warning(
                "RivaL free coding-model catalog unavailable type=%s",
                type(exc).__name__,
            )
            raise ResearchUnavailable(
                "Could not verify OpenRouter's current free coding models. "
                "No paid fallback or model request was attempted."
            ) from exc
        if not models:
            raise ResearchUnavailable(
                "No curated free coding model is currently usable. "
                "RivaL will not silently select a paid model."
            )
        self._models = models
        self._catalog_at = now
        return models

    async def request_status(self) -> dict:
        # Only local, persisted usage status. No catalog or model request.
        return await asyncio.to_thread(
            self.market.ai_request_status,
            model=self.model, now=datetime.now(UTC),
            daily_max=self.daily_max,
            cooldown_seconds=self.cooldown_seconds,
        )

    async def ask(self, *, asset: str, venues: tuple[str, ...]) -> AiResearch:
        if not self.available:
            raise ResearchUnavailable("OpenRouter key unavailable to RivaL.")
        async with self._lock:
            evidence, digest = await asyncio.to_thread(
                build_evidence, self.market, asset=asset, venues=venues,
                horizon_minutes=5,
            )
            models = await self.list_advertised_models()
            selected = models[0]
            cached = await asyncio.to_thread(
                self.market.cached_ai_research, asset=asset,
                model=selected, evidence_sha256=digest,
            )
            if cached is not None:
                return AiResearch(
                    asset=asset, model=selected,
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
                    "OpenRouter free research budget/cooldown reached; "
                    "live mathematical market monitoring continues."
                )
            instruction = (
                "Explain ONLY the attached verified historical market features. "
                "You are a software-analysis/coding research assistant, "
                "NOT a calibrated trading model. Inputs are independent "
                "exchange candles, not Polymarket's Chainlink TWAP labels. "
                "Avoid inventing source data, six-month coverage, future prices, "
                "profits, trade entries, or reliable win odds. Distinguish a "
                "five-minute exchange-candle lean from Polymarket settlement. "
                "Return a single JSON object with direction (up/down/uncertain), "
                "confidence (low/medium/high, SELF-REPORTED ONLY), "
                "thesis under 400 characters, risk under 250 characters. "
                "Prefer uncertain if features disagree. This output never "
                "changes the authoritative quantitative forecast. Evidence:"
            )
            try:
                response = await self._client.post(
                    _COMPLETE,
                    headers={
                        "Authorization": f"Bearer {self._key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": selected,
                        "messages": [
                            {"role": "system", "content": instruction},
                            {"role": "user", "content": json.dumps(
                                evidence, sort_keys=True, separators=(",", ":"),
                            )},
                        ],
                        "temperature": 0,
                        "max_tokens": 500,
                    },
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("model") not in (selected, None):
                    raise ValueError("provider changed selected model")
                content = payload["choices"][0]["message"]["content"]
                result = json.loads(content)
                if not isinstance(result, dict):
                    raise ValueError("AI response was not a JSON object")
                direction = result.get("direction")
                confidence = result.get("confidence")
                thesis = result.get("thesis")
                risk = result.get("risk")
                if (
                    direction not in ("up", "down", "uncertain")
                    or confidence not in ("low", "medium", "high")
                    or not isinstance(thesis, str) or not thesis.strip()
                    or not isinstance(risk, str) or not risk.strip()
                ):
                    raise ValueError("unvalidated coding-model response")
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in (401, 402, 403, 404, 429):
                    await asyncio.to_thread(
                        self.market.pause_ai_requests,
                        model=self.model, now=now, seconds=3600,
                    )
                _LOG.warning("RivaL OpenRouter free reviewer HTTP %s", status)
                raise ResearchUnavailable(
                    f"OpenRouter free reviewer unavailable (HTTP {status}). "
                    "No paid fallback or AI direction was used."
                ) from exc
            except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
                _LOG.warning(
                    "RivaL OpenRouter output rejected reason=%s",
                    type(exc).__name__,
                )
                raise ResearchUnavailable(
                    "Free AI research response failed validation; "
                    "no forecast or model change occurred."
                ) from exc
            thesis = thesis.strip()[:400]
            risk = risk.strip()[:250]
            await asyncio.to_thread(
                self.market.save_ai_research,
                asset=asset, model=selected, evidence_sha256=digest,
                issued_at=now, direction=direction, confidence=confidence,
                thesis=thesis, risk=risk,
                source_count=len(evidence["sources"]),
            )
            return AiResearch(
                asset=asset, model=selected, generated_at=now,
                direction=direction, confidence=confidence,
                thesis=thesis, risk=risk,
                source_count=len(evidence["sources"]),
                evidence_sha256=digest,
            )
