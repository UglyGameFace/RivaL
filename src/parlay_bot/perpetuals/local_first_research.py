"""Private RivaL Intelligence LFM3: local-only, cached, CPU serialized.

No third-party AI transport. Private inference has no daily cap by default;
an explicit positive daily limit is respected. Persisted reservations enforce
60 s minimum spacing, 3 per Discord requester per day and backend quarantine.
Every LLM call reserves capacity first. Background callers can wait for the
reported spacing deadline; other refusals remain explicit. The
deterministic evidence-only summary is not an LLM call and needs no quota.
Background loops run only when the operator opts in
(rival_llm_background_opt_in); user-initiated asks reserve quota but do
not need the opt-in.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
from datetime import UTC, datetime

import httpx

from parlay_bot.perpetuals.ai_types import AiResearch, ResearchUnavailable
from parlay_bot.perpetuals.analysis_evidence import build_evidence
from parlay_bot.perpetuals.evidence_policy import enforce as enforce_grounding
from parlay_bot.perpetuals.local_llm_discovery import discover_private_llm

_LOG = logging.getLogger(__name__)
LOCAL_MODEL = "LiquidAI/LFM3-1.7B-GGUF:Q4_K_M"
EVIDENCE_MODEL = "RivaL/verified-evidence-summary-v1"
LOCAL_ENDPOINT = "http://rival-llm:8765/v1/research"
MAX_LOCAL_EVIDENCE_BYTES = 24_000
# The private CPU worker needs spacing, not a paid-provider daily allowance.
DEFAULT_DAILY_MAX = 0
DEFAULT_COOLDOWN_SECONDS = 60
# Circuit breaker (batch 2 §3.4): the gateway has no Postgres access, so the
# bot mirrors its BackendRepair quarantine threshold — 3 backend failures
# in 1800 s pauses local LLM calls for 3600 s via pause_ai_requests().
BACKEND_FAILURE_WINDOW_SECONDS = 1800
BACKEND_FAILURE_QUARANTINE_COUNT = 3
BACKEND_FAILURE_PAUSE_SECONDS = 3600
# The gateway allows model reviews 90 s of CPU; wait slightly longer.
EXPLAIN_TIMEOUT = 100.0
# Background callers wait for the single-slot CPU gateway instead of losing
# their slot to one busy reply. Delays are jittered +/-20% and slept OUTSIDE
# the request lock so an interactive ask can still go first.
BUSY_RETRY_DELAYS: tuple[float, ...] = (5.0, 15.0, 45.0)
# Gateway failure kinds worth another attempt; others (token_limit,
# invalid_json, ...) repeat deterministically on the same evidence.
TRANSIENT_FAILURE_KINDS = frozenset({"backend_timeout", "backend_unreachable"})
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,47}")


class LocalBusy(ResearchUnavailable):
    """The private gateway is busy or briefly unavailable; safe to retry later."""


class LocalCapacityDeferred(LocalBusy):
    """Atomic spacing reservation refused; inference has not run."""

    def __init__(self, status: dict) -> None:
        self.retry_at = status.get("retry_at")
        self.reasons = tuple(status.get("reasons", ()))
        super().__init__(f"Local AI deferred (reasons={','.join(self.reasons)}; "
                         f"retry_at={self.retry_at}). No inference ran.")


def failure_detail(response: httpx.Response) -> str:
    """Status plus the gateway's allowlisted error codes; never body prose."""
    error = kind = "none"
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        for key in ("error", "failure_kind"):
            value = body.get(key)
            if isinstance(value, str) and _SAFE_CODE.fullmatch(value):
                if key == "error":
                    error = value
                else:
                    kind = value
    return f"status={response.status_code} error={error} failure_kind={kind}"


def _transient(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    if response.status_code != 503:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and (
        body.get("error") == "local_model_unavailable"
        or body.get("failure_kind") in TRANSIENT_FAILURE_KINDS
    )


async def _with_busy_retry(attempt, *, retry: bool, label: str):
    """Run attempt(); on LocalBusy, sleep and retry when allowed."""
    delays = BUSY_RETRY_DELAYS if retry else ()
    for index in range(len(delays) + 1):
        try:
            return await attempt()
        except LocalBusy as exc:
            if index >= len(delays):
                raise
            delay = delays[index] * random.uniform(0.8, 1.2)
            if isinstance(exc, LocalCapacityDeferred):
                if exc.retry_at is None or set(exc.reasons) != {"minimum_spacing"}:
                    raise
                delay = max(0.05, (exc.retry_at - datetime.now(UTC)).total_seconds() + 0.05)
                if delay > DEFAULT_COOLDOWN_SECONDS + 1:
                    raise
            _LOG.info("Private %s busy; retry %d/%d in %.1fs",
                      label, index + 1, len(delays), delay)
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


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
    """Private AI requests share persisted CPU capacity and optional daily caps.

    Every LLM call reserves one unit from the persisted quota tables
    before any inference runs. Cached evidence summaries cost no quota.
    """

    def __init__(
        self, market, *, local_key: str | None,
        client: httpx.AsyncClient | None = None,
        daily_max: int = DEFAULT_DAILY_MAX,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        if daily_max < 0 or cooldown_seconds < 60:
            raise ValueError("invalid AI budget policy")
        self.market = market
        self._key = local_key
        self.model = LOCAL_MODEL
        self._daily_max = daily_max
        self._cooldown_seconds = cooldown_seconds
        self._lock = asyncio.Lock()
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(52), follow_redirects=False,
        )
        self._owned_client = client is None
        self._peer_ip: str | None = None
        # Consecutive gateway backend failures (timestamps, UTC). Three
        # inside BACKEND_FAILURE_WINDOW_SECONDS quarantines the backend
        # through pause_ai_requests(); any successful inference clears it.
        self._backend_failures: list[datetime] = []

    @property
    def available(self) -> bool:
        return bool(self._key)

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    async def _reserve_quota(self, *, requester_id: int | None = None) -> None:
        """Reserve one LLM call from the persisted quota tables, or refuse.

        Raises ResearchUnavailable carrying the stored reasons and retry_at
        when the daily cap, per-requester cap, spacing, or circuit breaker
        blocks the call. No inference runs after this raises.
        """
        now = datetime.now(UTC)
        status = await asyncio.to_thread(
            self.market.ai_request_status,
            model=LOCAL_MODEL, now=now, daily_max=self._daily_max,
            cooldown_seconds=self._cooldown_seconds, requester_id=requester_id,
        )
        if not status["ready"]:
            if set(status["reasons"]) == {"minimum_spacing"}:
                raise LocalCapacityDeferred(status)
            raise ResearchUnavailable(
                "Local AI budget exhausted "
                f"(reasons={','.join(status['reasons'])}; "
                f"retry_at={status['retry_at']}). No inference ran."
            )
        reserved = await asyncio.to_thread(
            self.market.reserve_ai_request,
            model=LOCAL_MODEL, now=now, daily_max=self._daily_max,
            cooldown_seconds=self._cooldown_seconds, requester_id=requester_id,
        )
        if not reserved:
            # Lost a race with another worker: re-read the stored reasons.
            status = await asyncio.to_thread(
                self.market.ai_request_status,
                model=LOCAL_MODEL, now=now, daily_max=self._daily_max,
                cooldown_seconds=self._cooldown_seconds,
                requester_id=requester_id,
            )
            if set(status["reasons"]) == {"minimum_spacing"}:
                raise LocalCapacityDeferred(status)
            raise ResearchUnavailable(
                "Local AI budget exhausted "
                f"(reasons={','.join(status['reasons'])}; "
                f"retry_at={status['retry_at']}). No inference ran."
            )

    async def _reserve_once(self, reservation: list[bool], *,
                            requester_id: int | None = None) -> None:
        """Reserve quota at most once per ask/proposal call.

        Busy retries re-enter the same call; they reuse the first
        reservation instead of spending (and spacing-blocking) a second.
        """
        if not reservation:
            await self._reserve_quota(requester_id=requester_id)
            reservation.append(True)

    def _record_backend_success(self) -> None:
        self._backend_failures.clear()

    async def _record_backend_failure(self) -> None:
        """Count a gateway backend fault toward the quarantine threshold.

        Only gateway-side faults count (502/503/504, unreachable backend):
        a busy 429 is the single-slot semaphore, and client/invalid
        responses repeat deterministically, so neither quarantines.
        """
        now = datetime.now(UTC)
        self._backend_failures = [
            stamp for stamp in self._backend_failures
            if (now - stamp).total_seconds() < BACKEND_FAILURE_WINDOW_SECONDS
        ]
        self._backend_failures.append(now)
        if len(self._backend_failures) < BACKEND_FAILURE_QUARANTINE_COUNT:
            return
        self._backend_failures.clear()
        try:
            await asyncio.to_thread(
                self.market.pause_ai_requests,
                model=LOCAL_MODEL, now=now,
                seconds=BACKEND_FAILURE_PAUSE_SECONDS,
            )
        except Exception:  # noqa: BLE001 - a failed pause must not mask research
            _LOG.exception("Local AI backend quarantine pause failed")
            return
        _LOG.warning(
            "Local AI backend quarantined after %d failures in %ds; "
            "local LLM calls paused for %ds",
            BACKEND_FAILURE_QUARANTINE_COUNT,
            BACKEND_FAILURE_WINDOW_SECONDS, BACKEND_FAILURE_PAUSE_SECONDS,
        )

    async def pause_ai_requests(self, *, seconds: int = 3600) -> None:
        """Operator circuit breaker: block local LLM calls for `seconds`.

        Manual use (Felix) and the automatic backend quarantine both go
        through the persisted pause; it survives bot restarts.
        """
        await asyncio.to_thread(
            self.market.pause_ai_requests,
            model=LOCAL_MODEL, now=datetime.now(UTC), seconds=seconds,
        )

    async def request_status(self) -> dict:
        """Live quota status for the local model: usage, limits, retry_at."""
        if not self.available:
            return {
                "model": LOCAL_MODEL,
                "ready": False,
                "reasons": ("not_configured",),
                "requests_today": 0,
                "rival_daily_limit": self._daily_max,
                "retry_at": None,
                "runtime_verified": False,
            }
        status = await asyncio.to_thread(
            self.market.ai_request_status,
            model=LOCAL_MODEL, now=datetime.now(UTC),
            daily_max=self._daily_max, cooldown_seconds=self._cooldown_seconds,
        )
        return {
            "model": LOCAL_MODEL,
            "ready": status["ready"],
            "reasons": status["reasons"],
            "requests_today": status["requests_today"],
            "rival_daily_limit": status["rival_daily_limit"],
            "retry_at": status["retry_at"],
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
                  requester_id: int | None = None,
                  busy_retry: bool = False) -> AiResearch:
        """Research one asset. Interactive clicks never wait on retries;
        background callers pass busy_retry=True to survive a busy gateway.

        Quota is reserved once per call, after an exact-evidence cache hit
        (which costs no quota) and before any inference. The per-requester
        cap applies when requester_id is given.
        """
        if not self.available:
            raise ResearchUnavailable("Private RivaL Intelligence token not configured.")
        reservation: list[bool] = []
        return await _with_busy_retry(
            lambda: self._ask_once(asset=asset, venues=venues,
                                   requester_id=requester_id,
                                   _reservation=reservation),
            retry=busy_retry, label="research",
        )

    async def _ask_once(self, *, asset: str, venues: tuple[str, ...],
                        requester_id: int | None = None,
                        _reservation: list[bool] | None = None) -> AiResearch:
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
            await self._reserve_once(
                _reservation if _reservation is not None else [],
                requester_id=requester_id,
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

    async def explain_models(self, *, asset: str, venues: tuple[str, ...],
                             calls: dict, market_context: dict | None = None) -> dict:
        reservation: list[bool] = []
        return await _with_busy_retry(
            lambda: self._explain_models_once(asset=asset, venues=venues, calls=calls,
                                              market_context=market_context,
                                              _reservation=reservation),
            retry=True, label="model review")

    async def _explain_models_once(self, *, asset: str, venues: tuple[str, ...],
                                   calls: dict, market_context: dict | None = None,
                                   _reservation: list[bool] | None = None) -> dict:
        """RivaL Intelligence reviews the prediction models' current calls.

        Returns per-timeframe 'follow'/'abstain' stances plus a short
        explanation. Raises ResearchUnavailable when the private model is not
        configured, busy or does not support the review endpoint yet.
        """
        if not self.available:
            raise ResearchUnavailable("Private RivaL Intelligence token not configured.")
        if not calls:
            raise ResearchUnavailable("No current model calls to review.")
        async with self._lock:
            evidence, _digest = await asyncio.to_thread(
                build_evidence, self.market, asset=asset, venues=venues,
                horizon_minutes=5,
            )
            evidence = {**evidence, "model_calls": calls}
            if market_context:
                evidence["market_context"] = market_context
            if len(json.dumps(evidence, sort_keys=True, separators=(",", ":"),
                              default=str).encode("utf-8")) > MAX_LOCAL_EVIDENCE_BYTES:
                raise ResearchUnavailable("Model review evidence too large.")
            # Background-only path: reserves shared quota (no requester cap).
            await self._reserve_once(_reservation if _reservation is not None else [])
            headers = {"Authorization": f"Bearer {self._key}",
                       "Content-Type": "application/json"}
            payload = json.loads(json.dumps({"asset": asset, "evidence": evidence},
                                            default=str))
            base = (f"http://{self._peer_ip}:8765" if self._peer_ip
                    else LOCAL_ENDPOINT.rsplit("/v1/", 1)[0])
            try:
                response = await self._client.post(
                    base + "/v1/explain", headers=headers, json=payload,
                    follow_redirects=False, timeout=EXPLAIN_TIMEOUT)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                self._peer_ip = None
                peer = await asyncio.to_thread(discover_private_llm, self._key)
                if peer is None:
                    await self._record_backend_failure()
                    raise ResearchUnavailable("Private RivaL Intelligence unreachable.") from None
                self._peer_ip = peer
                response = await self._client.post(
                    f"http://{peer}:8765/v1/explain", headers=headers, json=payload,
                    follow_redirects=False, timeout=EXPLAIN_TIMEOUT)
            except httpx.RequestError as exc:
                await self._record_backend_failure()
                raise ResearchUnavailable("Private RivaL Intelligence network error.") from exc
            if response.status_code == 404:
                raise ResearchUnavailable("RivaL Intelligence service needs the review update.")
            if response.status_code == 429:
                raise LocalBusy("RivaL Intelligence busy.")
            if response.status_code in (502, 503, 504):
                await self._record_backend_failure()
                raise ResearchUnavailable(
                    f"RivaL Intelligence review failed (HTTP {response.status_code}).")
            if response.status_code != 200:
                raise LocalBusy("RivaL Intelligence busy.")
            if response.status_code != 200:
                # The gateway returns a bounded failure_kind code (never model
                # text): surface it so a failing review explains itself.
                kind = "unknown"
                try:
                    body = response.json()
                    kind = str(body.get("failure_kind") or body.get("error") or "unknown")[:40]
                except ValueError:
                    pass
                raise ResearchUnavailable(
                    f"RivaL Intelligence review failed (HTTP {response.status_code}, {kind}).")
            data = response.json()
            if not (isinstance(data, dict) and data.get("model") == LOCAL_MODEL
                    and data.get("mode") == "explain"
                    and all(data.get(f"stance_{h}m") in ("follow", "abstain")
                            for h in (5, 15, 60))
                    and isinstance(data.get("thesis"), str)
                    and 0 < len(data["thesis"].strip()) <= 240
                    and isinstance(data.get("risk"), str)
                    and 0 < len(data["risk"].strip()) <= 160):
                raise ResearchUnavailable("RivaL Intelligence returned an invalid review.")
            # Repeat the guard in RivaL: valid JSON from an outdated gateway
            # still cannot store or display ungrounded review prose. The
            # gateway already enforces this; re-apply here so an old gateway
            # cannot leak raw LLM thesis/risk into rival_intelligence_calls.
            # Stances, model and mode pass through unchanged; no direction
            # or confidence is asserted for a model review.
            grounded = enforce_grounding(data, evidence, asset=asset)
            self._record_backend_success()
            return {
                **data,
                "thesis": grounded["thesis"][:240],
                "risk": grounded["risk"][:160],
                "grounded_by": grounded["grounded_by"],
            }

    async def propose_feature_hypothesis(
        self, *, asset: str, venues: tuple[str, ...],
        busy_retry: bool = True, prior_trials: list[dict] | None = None,
    ) -> dict:
        """Use the private CPU under the persisted quota (batch 2 §3.4).

        Suggestions cannot write weights or scores. They are recorded separately
        and must compete against a no-LLM baseline on chronological source data.
        Background-only, so a busy or briefly unavailable gateway is retried.
        Quota is reserved once per proposal call, before the first inference
        attempt; busy retries reuse that reservation.
        """
        if not self.available:
            raise ResearchUnavailable("Private RivaL Intelligence token unavailable")
        reservation: list[bool] = []
        # Set by _propose_once when the gateway backend itself faulted.
        # The wrapper records one failure per call, not one per retry.
        failure_state: dict[str, bool] = {"backend_failed": False}
        try:
            proposed = await _with_busy_retry(
                lambda: self._propose_once(asset=asset, venues=venues,
                                           prior_trials=prior_trials,
                                           _reservation=reservation,
                                           _failure_state=failure_state),
                retry=busy_retry, label="hypothesis",
            )
        except ResearchUnavailable:
            if failure_state["backend_failed"]:
                await self._record_backend_failure()
            raise
        self._record_backend_success()
        return proposed

    async def _propose_once(self, *, asset: str, venues: tuple[str, ...],
                            prior_trials: list[dict] | None = None,
                            _reservation: list[bool] | None = None,
                            _failure_state: dict[str, bool] | None = None) -> dict:
        async with self._lock:
            evidence, digest = await asyncio.to_thread(
                build_evidence, self.market, asset=asset, venues=venues,
                horizon_minutes=5,
            )
            if prior_trials:
                # Earlier ideas' real offline scores. Part of the evidence, so
                # its SHA covers what the model was shown.
                evidence = {**evidence, "prior_trials": prior_trials[:12]}
                digest = hashlib.sha256(json.dumps(
                    evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if (len(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode())
                    > MAX_LOCAL_EVIDENCE_BYTES):
                raise ResearchUnavailable("Verified feature evidence is oversized")
            await self._reserve_once(
                _reservation if _reservation is not None else [],
            )
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
                if response.status_code != 200:
                    detail = failure_detail(response)
                    _LOG.warning("Private hypothesis research rejected %s", detail)
                    if (response.status_code in (502, 503, 504)
                            and _failure_state is not None):
                        _failure_state["backend_failed"] = True
                    error = LocalBusy if _transient(response) else ResearchUnavailable
                    raise error(
                        f"Private hypothesis endpoint unavailable ({detail}); "
                        "no model weights changed"
                    )
                hypothesis = response.json()
            except (httpx.RequestError, ValueError) as exc:
                if isinstance(exc, ResearchUnavailable):
                    raise
                if (isinstance(exc, httpx.RequestError)
                        and _failure_state is not None):
                    _failure_state["backend_failed"] = True
                _LOG.warning("Private hypothesis research unavailable %s",
                             type(exc).__name__)
                raise ResearchUnavailable(
                    "Private hypothesis endpoint unavailable; no model weights changed"
                ) from exc
            from parlay_bot.perpetuals.research_hypotheses import validate_proposal

            if not validate_proposal(hypothesis):
                raise ResearchUnavailable("Untrusted feature hypothesis rejected")
            proposal = {
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
            if hypothesis["hypothesis"] == "feature_subset_v2":
                proposal["feature_indices"] = hypothesis["feature_indices"]
            return proposal

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
            self._record_backend_success()
            return {
                "direction": grounded["direction"], "confidence": grounded["confidence"],
                "thesis": grounded["thesis"], "risk": grounded["risk"],
                "served_model": LOCAL_MODEL,
            }
        except httpx.HTTPStatusError as exc:
            # An authentication error, malformed client request, or BUSY is
            # not permission to spend a second provider's capacity.
            detail = failure_detail(exc.response)
            if exc.response.status_code not in (502, 503, 504):
                _LOG.warning("Local AI rejected request %s", detail)
                error = LocalBusy if exc.response.status_code == 429 else ResearchUnavailable
                raise error(
                    f"Local AI rejected research (HTTP {exc.response.status_code})."
                ) from exc
            _LOG.warning("Local inference unavailable %s", detail)
            await self._record_backend_failure()
        except httpx.RequestError:
            _LOG.warning("Private local AI network unavailable")
            await self._record_backend_failure()
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
