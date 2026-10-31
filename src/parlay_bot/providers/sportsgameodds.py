from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Self

import httpx

from parlay_bot.providers.errors import (
    ProviderProtocolError,
    ProviderRateLimited,
    QuotaReserveReached,
)
from parlay_bot.providers.owner_limits import OwnerLimitStore


@dataclass(frozen=True)
class SportsGameOddsUsage:
    tier: str | None
    monthly_max_entities: int | None
    monthly_current_entities: int | None
    minute_max_requests: int | None
    minute_current_requests: int

    @property
    def monthly_remaining_entities(self) -> int | None:
        if self.monthly_max_entities is None or self.monthly_current_entities is None:
            return None
        return max(self.monthly_max_entities - self.monthly_current_entities, 0)


class _CooldownGate:
    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def wait(self) -> None:
        async with self._lock:
            delay = self._next_allowed - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_allowed = time.monotonic() + self._seconds


def _int_or_none(value: Any) -> int | None:
    if value in (None, "unlimited", "n/a"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _interval_value(interval: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        if key in interval:
            return _int_or_none(interval[key])
    return None


class SportsGameOddsClient:
    """Minimal SportsGameOdds v2 client for RivaL's current-board feed."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.sportsgameodds.com/v2",
        monthly_entity_reserve: int = 250,
        client: httpx.AsyncClient | None = None,
        request_cooldown_seconds: float = 6.1,
        owner_limits: OwnerLimitStore | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("SportsGameOdds API key is required")
        if monthly_entity_reserve < 0:
            raise ValueError("monthly_entity_reserve cannot be negative")

        self._owner_limits = owner_limits
        self._api_key = api_key
        self._monthly_entity_reserve = monthly_entity_reserve
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(30.0),
            headers={"x-api-key": api_key},
        )
        self._gate = _CooldownGate(request_cooldown_seconds)
        # Guard event reads against concurrently refreshing collectors.
        self._event_lock = asyncio.Lock()
        self._projected_entities: int | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(
        self,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        throttled: bool = True,
    ) -> httpx.Response:
        if throttled:
            await self._gate.wait()
        headers = {"x-api-key": self._api_key}
        response = await self._client.get(path, params=params, headers=headers)

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            suffix = f" retry_after={retry_after}" if retry_after else ""
            raise ProviderRateLimited(f"SportsGameOdds rate limited {path}.{suffix}")
        if response.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"SportsGameOdds request failed with {response.status_code}",
                request=response.request,
                response=response,
            )
        return response

    @staticmethod
    def _payload(response: httpx.Response) -> dict[str, Any]:
        payload = response.json()
        if not isinstance(payload, dict):
            raise ProviderProtocolError("SportsGameOdds response is not an object")
        if payload.get("success") is not True:
            raise ProviderProtocolError(
                f"SportsGameOdds response reported failure: {payload.get('error') or 'unknown error'}"
            )
        return payload

    async def get_available_leagues(self) -> tuple[dict[str, str], ...]:
        """Actual key-entitled league IDs, TTL cached to conserve metadata quota.

        The public provider's advertised 67+ leagues is not proof the owner's
        account can read them. /leagues is authoritative for this API key.
        """
        current = time.monotonic()
        cached = getattr(self, "_league_cache", None)
        if cached is not None and current - cached[0] < 3600:
            return cached[1]
        # /leagues returns its full matching catalog; "limit" is not a documented
        # query parameter for this endpoint. Do not add an unsupported argument.
        payload = self._payload(await self._request("/leagues"))
        data = payload.get("data")
        if not isinstance(data, list) or len(data) > 300:
            raise ProviderProtocolError("Malformed available league catalog")
        leagues: dict[str, dict[str, str]] = {}
        for row in data:
            if not isinstance(row, dict):
                continue
            league = row.get("leagueID")
            sport = row.get("sportID")
            if (
                not isinstance(league, str)
                or not isinstance(sport, str)
                or not 1 <= len(league) <= 64
                or not 1 <= len(sport) <= 64
            ):
                continue
            # The enabled flag is the plan-entitlement contract. Missing,
            # null, false or malformed flags do NOT establish authorized access.
            if row.get("enabled") is not True or sport == "NON_SPORTS":
                continue
            leagues[league] = {
                "league_id": league, "sport_id": sport,
                "name": str(row.get("name") or league)[:80],
            }
        result = tuple(leagues[k] for k in sorted(leagues))
        self._league_cache = (time.monotonic(), result)
        return result

    async def get_usage(self) -> SportsGameOddsUsage:
        payload = self._payload(await self._request("/account/usage", throttled=False))
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProviderProtocolError("SportsGameOdds usage response has no data object")

        rate_limits = data.get("rateLimits") or {}
        if not isinstance(rate_limits, dict):
            rate_limits = {}

        month = rate_limits.get("per-month") or rate_limits.get("perMonth") or {}
        minute = rate_limits.get("per-minute") or rate_limits.get("perMinute") or {}
        if not isinstance(month, dict):
            month = {}
        if not isinstance(minute, dict):
            minute = {}

        monthly_max = _interval_value(
            month,
            "max-entities",
            "maxEntitiesPerInterval",
            "max_entities",
        )
        monthly_current = _interval_value(
            month,
            "current-entities",
            "currentIntervalEntities",
            "current_entities",
        )
        minute_max = _interval_value(
            minute,
            "max-requests",
            "maxRequestsPerInterval",
            "max_requests",
        )
        minute_current = _interval_value(
            minute,
            "current-requests",
            "currentIntervalRequests",
            "current_requests",
        )

        return SportsGameOddsUsage(
            tier=str(data.get("tier")) if data.get("tier") is not None else None,
            monthly_max_entities=monthly_max,
            monthly_current_entities=monthly_current,
            minute_max_requests=minute_max,
            minute_current_requests=minute_current or 0,
        )

    async def _safe_event_limit(self, requested: int) -> int:
        """Fail closed on unknown usage; guard calls under the event lock."""
        if requested < 1:
            raise ValueError("requested event limit must be positive")
        usage = await self.get_usage()
        if (
            usage.monthly_max_entities is None
            or usage.monthly_current_entities is None
            or usage.monthly_max_entities < 0
            or usage.monthly_current_entities < 0
            or usage.monthly_current_entities > usage.monthly_max_entities
        ):
            raise QuotaReserveReached("SportsGameOdds monthly usage unknown; event requests paused")
        observed = max(usage.monthly_current_entities, self._projected_entities or 0)
        self._projected_entities = observed
        remaining = usage.monthly_max_entities - observed - self._monthly_entity_reserve
        owner = self._owner_limits.get("sportsgameodds") if self._owner_limits else None
        if owner is not None:
            owner_remaining = owner.maximum_units - observed - owner.reserve_units
            if owner_remaining <= 0:
                raise QuotaReserveReached("Owner-configured SportsGameOdds budget exhausted")
            remaining = min(remaining, owner_remaining)
        if remaining <= 0:
            raise QuotaReserveReached("SportsGameOdds monthly entity reserve reached")
        return min(requested, remaining)

    def _reserve_event_entities(self, count: int) -> None:
        """Reserve pessimistically before network dispatch; failures may bill."""
        if self._projected_entities is None or count < 1:
            raise RuntimeError("Event usage must be checked before reservation")
        self._projected_entities += count

    async def get_result_events(self, event_ids: tuple[str, ...]) -> list[dict[str, Any]]:
        """Bounded official eventID lookup for already observed fixtures.

        The provider documents that an explicit eventID overrides started/
        finalized filters. We therefore verify finalization on EACH result.
        """
        if not 1 <= len(event_ids) <= 8 or any(
            not isinstance(e, str) or not 1 <= len(e) <= 128
            or any(c in e for c in (",", " ", "\\n"))
            for e in event_ids
        ):
            raise ValueError("Invalid bounded result lookup")
        async with self._event_lock:
            budget = await self._safe_event_limit(len(event_ids))
            if budget < len(event_ids):
                raise QuotaReserveReached("Results lookup blocked by provider quota reserve")
            self._reserve_event_entities(len(event_ids))
            response = self._payload(await self._request(
                "/events", params={
                    ("eventID" if len(event_ids) == 1 else "eventIDs"):
                        ",".join(event_ids),
                    "limit": len(event_ids),
                },
            ))
        data = response.get("data")
        if not isinstance(data, list) or len(data) > len(event_ids):
            raise ProviderProtocolError("Malformed finalized event batch")
        permitted = set(event_ids)
        if any(
            not isinstance(row, dict) or row.get("eventID") not in permitted
            for row in data
        ):
            raise ProviderProtocolError("Unexpected event in results lookup")
        return data

    async def get_events(
        self,
        *,
        league_ids: Sequence[str],
        bookmaker_ids: Sequence[str] | None = None,
        limit: int = 25,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, str | None]:
        leagues = [item.strip() for item in league_ids if item.strip()]
        if not leagues:
            raise ValueError("At least one league ID is required")

        params: dict[str, object] = {
            "leagueID": ",".join(leagues),
            "oddsAvailable": "true",
            "started": "false",
            "cancelled": "false",
            "includeAltLines": "false",
            "limit": limit,
        }
        if bookmaker_ids:
            books = [item.strip() for item in bookmaker_ids if item.strip()]
            if books:
                params["bookmakerID"] = ",".join(books)
        if cursor:
            params["cursor"] = cursor

        async with self._event_lock:
            safe_limit = await self._safe_event_limit(limit)
            params["limit"] = safe_limit
            self._reserve_event_entities(safe_limit)
            payload = self._payload(await self._request("/events", params=params))
        data = payload.get("data")
        if not isinstance(data, list):
            raise ProviderProtocolError("SportsGameOdds events response has no data list")

        events = [item for item in data if isinstance(item, dict)]
        next_cursor = payload.get("nextCursor")
        notice = payload.get("notice")
        return (
            events,
            str(next_cursor) if next_cursor else None,
            str(notice) if notice else None,
        )
