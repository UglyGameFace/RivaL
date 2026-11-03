"""Private RivaL Intelligence LFM3: local-only, cached, CPU serialized.

No third-party AI transport, free-tier quota, token budget or daily cooldown.
"""
from __future__ import annotations

from datetime import UTC, datetime

import asyncio
import json
import logging

import httpx

from parlay_bot.perpetuals.evidence_policy import enforce as enforce_grounding
from parlay_bot.perpetuals.ai_types import AiResearch, ResearchUnavailable
from parlay_bot.perpetuals.local_llm_discovery import discover_private_llm
from parlay_bot.perpetuals.analysis_evidence import build_evidence

_LOG = logging.getLogger(__name__)
LOCAL_MODEL = "LiquidAI/LFM3-1.7B-GGUF:Q4_K_M"
EVIDENCE_MODEL = "RivaL/verified-evidence-summary-v1"
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


class LocalFirstResearch:
    """Private AI requests use bounded concurrency, not daily paid-API caps."""

    def __init__(
        self, market, *, local_key: str | None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.market = market
        self._key = local_key
        self.model = LOCAL_MODEL
        self._lock = asyncio.Lock()
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(52), follow_redirects=False,
        )
        self._owned_client = client is None
        self._peer_ip: str | None = None

    @property
    def available(self) -> bool:
        return bool(self._key)

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    async def request_status(self) -> dict:
        """Configuration status only: does not claim the host is currently healthy."""
        return {
            "model": LOCAL_MODEL,
            "ready": self.available,
            "reasons": () if self.available else ("not_configured",),
            "unmetered_local": True,
            "runtime_verified": False,
        }

    async def list_advertised_models(self) -> tuple[str, ...]:
        if not self.available:
            raise ResearchUnavailable("Private local LLM token is not configured.")
        # Fixed only after operator deploys matching local model; not a
        # diagnostic claim that the app is currently running.
        return (LOCAL_MODEL,)

    async def review_contract(self, evidence: dict) -> dict:
        raise ResearchUnavailable(
            "Local LLM cannot verify Polymarket oracle settlements. "
            "Contract-specific research requires authoritative contract evidence."
        )

    async def ask(self, *, asset: str, venues: tuple[str, ...],
                  requester_id: int | None = None) -> AiResearch:
        if not self.available:
            raise ResearchUnavailable("Private RivaL Intelligence token not configured.")
        async with self._lock:
            evidence, digest = await asyncio.to_thread(
                build_evidence, self.market, asset=asset, venues=venues,
                horizon_minutes=5,
            )
            for candidate in (LOCAL_MODEL, EVIDENCE_MODEL):
                cached = await asyncio.to_thread(
                    self.market.cached_ai_research, asset=asset,
                    model=candidate, evidence_sha256=digest,
                )
                if cached is not None:
                    return AiResearch(
                        asset=asset, model=candidate,
                        generated_at=datetime.fromisoformat(cached["issued_at"]),
                        direction=cached["direction"], confidence=cached["confidence"],
                        thesis=cached["thesis"], risk=cached["risk"],
                        source_count=int(cached["source_count"]),
                        evidence_sha256=digest, from_cache=True,
                    )
            now = datetime.now(UTC)
            result = await self._complete(evidence=evidence)
            served = result["served_model"]
            direction, confidence = result["direction"], result["confidence"]
            thesis, risk = result["thesis"], result["risk"]
            thesis = thesis.strip()[:400]
            risk = risk.strip()[:250]
            await asyncio.to_thread(
                self.market.save_ai_research,
                asset=asset, model=served, evidence_sha256=digest,
                issued_at=now, direction=direction, confidence=confidence,
                thesis=thesis, risk=risk,
                source_count=len(evidence["sources"]),
            )
            return AiResearch(
                asset=asset, model=served, generated_at=now,
                direction=direction, confidence=confidence,
                thesis=thesis, risk=risk,
                source_count=len(evidence["sources"]),
                evidence_sha256=digest,
            )

    async def propose_feature_hypothesis(
        self, *, asset: str, venues: tuple[str, ...],
    ) -> dict:
        """Use the private CPU under the existing lock, without API quotas.

        Suggestions cannot write weights or scores. They are recorded separately
        and must compete against a no-LLM baseline on chronological source data.
        """
        if not self.available:
            raise ResearchUnavailable("Private RivaL Intelligence token unavailable")
        async with self._lock:
            evidence, digest = await asyncio.to_thread(
                build_evidence, self.market, asset=asset, venues=venues,
                horizon_minutes=5,
            )
            if (len(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode())
                    > MAX_LOCAL_EVIDENCE_BYTES):
                raise ResearchUnavailable("Verified feature evidence is oversized")
            now = datetime.now(UTC)
            headers = {
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
            }
            request = {"asset": asset, "evidence": evidence}
            endpoint = (f"http://{self._peer_ip}:8765/v1/hypothesis"
                        if self._peer_ip else "http://rival-llm:8765/v1/hypothesis")
            try:
                try:
                    response = await self._client.post(
                        endpoint, headers=headers, json=request, follow_redirects=False,
                    )
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    self._peer_ip = None
                    peer = await asyncio.to_thread(discover_private_llm, self._key)
                    if peer is None:
                        raise
                    self._peer_ip = peer
                    response = await self._client.post(
                        f"http://{peer}:8765/v1/hypothesis", headers=headers,
                        json=request, follow_redirects=False,
                    )
                response.raise_for_status()
                hypothesis = response.json()
            except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
                _LOG.warning("Private hypothesis research unavailable %s",
                             type(exc).__name__)
                raise ResearchUnavailable(
                    "Private hypothesis endpoint unavailable; no model weights changed"
                ) from exc
            from parlay_bot.perpetuals.research_hypotheses import validate_proposal

            if not validate_proposal(hypothesis):
                raise ResearchUnavailable("Untrusted feature hypothesis rejected")
            return {
                "asset": asset, "evidence_sha256": digest,
                "observed_at": now.isoformat(),
                "venues": sorted({
                    str(row.get("venue")) for row in evidence.get("sources", [])
                    if isinstance(row, dict) and row.get("venue") in venues
                }),
                "model": hypothesis["model"],
                "hypothesis": hypothesis["hypothesis"],
                "rationale": hypothesis["rationale"],
                "falsification": hypothesis["falsification"],
                "research_only": True,
            }

    async def _complete(self, *, evidence: dict) -> dict:
        if len(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")) > MAX_LOCAL_EVIDENCE_BYTES:
            raise ResearchUnavailable("Verified evidence too large for local inference.")
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
                "to another provider."
            ) from exc

        # A CPU timeout must not masquerade as an LFM3 answer. Only the same
        # prevalidated, hashed source evidence can produce this free, read-only
        # deterministic fallback, with its OWN model identity and cache key.
        if evidence.get("analysis") != "unvalidated_research":
            raise ResearchUnavailable("Verified mathematical evidence unavailable.")
        grounded = enforce_grounding(
            {"direction": "uncertain", "confidence": "low"}, evidence,
            asset=evidence.get("asset"),
        )
        return {
            "direction": grounded["direction"], "confidence": grounded["confidence"],
            "thesis": grounded["thesis"], "risk": grounded["risk"],
            "served_model": EVIDENCE_MODEL,
        }
