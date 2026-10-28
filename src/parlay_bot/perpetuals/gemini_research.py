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
from datetime import UTC, datetime, timedelta

import httpx

from parlay_bot.perpetuals.analysis_evidence import build_evidence
from parlay_bot.perpetuals.market_store import PerpetualMarketStore

_LOG = logging.getLogger(__name__)
_MODEL = re.compile(r"^[a-z0-9][a-z0-9.\-]{2,63}$")


_NON_RESEARCH_VARIANTS = (
    "-image", "-tts", "-live", "-computer-use", "-embedding",
    "-transcribe", "-translate", "-native-audio", "-audio",
    "-speech", "-nano-banana",
)


def _is_text_research_model(name: str) -> bool:
    """Exclude video/image/audio and computer-control tools from price research."""
    return (
        name.startswith("gemini-")
        and not any(part in name for part in _NON_RESEARCH_VARIANTS)
    )


def _model_rank(name: str) -> tuple[int, int, int, int, str]:
    """Newer versions first, stable before previews; not a capability ranking."""
    match = re.match(r"^gemini-(\\d+)(?:\\.(\\d+))?", name)
    major = int(match.group(1)) if match else -1
    minor = int(match.group(2)) if match and match.group(2) else 0
    stable = int("preview" not in name and "exp" not in name)
    return (major, minor, stable, int("flash" in name), name)


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
        model: str = "gemini-3.8-flash", daily_max: int = 24,
        cooldown_seconds: int = 900,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not _MODEL.fullmatch(model) or not 1 <= daily_max <= 96:
            raise ValueError("invalid Gemini configuration")
        if not 60 <= cooldown_seconds <= 86400:
            raise ValueError("invalid Gemini cooldown")
        self.market = market
        self._key = api_key
        # This app's old 2.5 Flash endpoint returned HTTP 404 in production.
        # Migrate that legacy selection once; do not repeatedly spend requests
        # retrying a known-failing model. A different explicit model stays explicit.
        if model == "gemini-2.5-flash":
            _LOG.warning(
                "RivaL Gemini legacy model gemini-2.5-flash replaced by "
                "gemini-3.8-flash after production HTTP 404"
            )
            model = "gemini-3.8-flash"
        self.model = model
        self.daily_max = daily_max
        self.cooldown_seconds = cooldown_seconds
        self._client = client or httpx.AsyncClient(timeout=25, follow_redirects=False)
        self._owned_client = client is None
        self._lock = asyncio.Lock()
        self._model_list: tuple[str, ...] | None = None
        self._model_list_checked_at: datetime | None = None

    @property
    def available(self) -> bool:
        return bool(self._key)

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    async def list_advertised_models(self) -> tuple[str, ...]:
        """List relevant text-generation models across ALL catalog pages.

        The older implementation sorted models alphabetically and displayed
        only the first 12, making Gemini 2.5 look like a recommendation.
        Listing is metadata only, not a successful generateContent call.
        """
        if not self._key:
            raise ResearchUnavailable("Gemini API key is not configured.")
        async with self._lock:
            now = datetime.now(UTC)
            if (
                self._model_list is not None
                and self._model_list_checked_at is not None
                and now - self._model_list_checked_at < timedelta(hours=1)
            ):
                return self._model_list
            names: set[str] = set()
            token: str | None = None
            seen_tokens: set[str] = set()
            try:
                # Google's catalog is paginated. Never say the selected model
                # is absent based on an incomplete first page.
                for _ in range(5):
                    params: dict[str, str | int] = {"pageSize": 1000}
                    if token:
                        params["pageToken"] = token
                    response = await self._client.get(
                        "https://generativelanguage.googleapis.com/v1beta/models",
                        params=params,
                        headers={"x-goog-api-key": self._key},
                        follow_redirects=False,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if (
                        not isinstance(payload, dict)
                        or not isinstance(payload.get("models"), list)
                    ):
                        raise TypeError("missing model metadata")
                    for item in payload["models"]:
                        if not isinstance(item, dict):
                            continue
                        name = item.get("name")
                        methods = item.get("supportedGenerationMethods", [])
                        if (
                            isinstance(name, str)
                            and name.startswith("models/gemini-")
                            and isinstance(methods, list)
                            and "generateContent" in methods
                            and _is_text_research_model(name[7:])
                        ):
                            names.add(name[7:])
                    next_token = payload.get("nextPageToken")
                    if not next_token:
                        break
                    if not isinstance(next_token, str) or next_token in seen_tokens:
                        raise ValueError("invalid catalog pagination")
                    seen_tokens.add(next_token)
                    token = next_token
                else:
                    raise ValueError("model catalog exceeds safe pagination limit")
                self._model_list = tuple(sorted(names, key=_model_rank, reverse=True))
                self._model_list_checked_at = now
                return self._model_list
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                _LOG.warning(
                    "Gemini model metadata lookup unavailable type=%s",
                    type(exc).__name__,
                )
                raise ResearchUnavailable(
                    "Unable to complete Gemini text-model catalog lookup "
                    "for this API key. No generation request was made."
                ) from exc

    async def request_status(self) -> dict:
        """Report local, persisted limits without spending a Google request."""
        return await asyncio.to_thread(
            self.market.ai_request_status,
            model=self.model, now=datetime.now(UTC),
            daily_max=self.daily_max,
            cooldown_seconds=self.cooldown_seconds,
        )

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
                    f"RivaL temporarily paused {self.model} analysis. "
                    "See the exact reason and retry time below. "
                    "Market collection continues."
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
                if code in (401, 403, 404, 429):
                    await asyncio.to_thread(
                        self.market.pause_ai_requests, model=self.model,
                        now=now, seconds=3600,
                    )
                _LOG.warning("RivaL Gemini unavailable, HTTP %d", code)
                explanation = (
                    f"Gemini model {self.model} returned HTTP 404. "
                    "It is unavailable for this API key/project. "
                    "Use Check models to inspect advertised alternatives "
                    "(listing alone cannot guarantee generation)."
                    if code == 404 else
                    f"Gemini unavailable (HTTP {code}); no AI forecast generated."
                )
                raise ResearchUnavailable(explanation) from exc
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
