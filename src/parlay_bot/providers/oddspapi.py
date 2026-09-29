from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import ClassVar, Self

import httpx

from parlay_bot.domain.models import (
    AccountSnapshot,
    BookmakerEntitlement,
    Fixture,
)
from parlay_bot.providers.errors import (
    ProviderProtocolError,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    QuotaReserveReached,
)


class _CooldownGate:
    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next_allowed - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_allowed = time.monotonic() + self._seconds


class OddsPapiClient:
    """Quota-aware wrapper around the documented OddsPapi v4 REST API."""

    DEFAULT_COOLDOWNS: ClassVar[dict[str, float]] = {
        "account": 1.0,
        "fixtures": 2.0,
        "odds": 0.5,
        "historical": 5.0,
        "odds_by_tournaments": 2.0,
    }

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.oddspapi.io",
        quota_reserve: int = 20,
        account_cache_seconds: int = 60,
        client: httpx.AsyncClient | None = None,
        cooldowns: Mapping[str, float] | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OddsPapi API key is required")
        if quota_reserve < 0:
            raise ValueError("quota_reserve cannot be negative")

        self._api_key = api_key
        self._quota_reserve = quota_reserve
        self._account_cache_seconds = account_cache_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(30.0),
        )

        configured = dict(self.DEFAULT_COOLDOWNS)
        if cooldowns:
            configured.update(cooldowns)
        self._gates = {
            name: _CooldownGate(seconds) for name, seconds in configured.items()
        }

        self._account_cache: AccountSnapshot | None = None
        self._account_cached_at = 0.0
        self._projected_request_count: int | None = None

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
        params: Mapping[str, object] | None,
        endpoint: str,
        billable: bool,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        await self._gates[endpoint].wait()

        request_params = dict(params or {})
        request_params["apiKey"] = self._api_key

        response = await self._client.get(path, params=request_params, headers=headers)

        if billable and self._projected_request_count is not None:
            # OddsPapi documents that processed 2xx/4xx/5xx responses consume quota.
            self._projected_request_count += 1

        if response.status_code == 429:
            raise ProviderRateLimited(f"OddsPapi rate limited endpoint {path}")
        if response.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"OddsPapi request failed with {response.status_code}",
                request=response.request,
                response=response,
            )
        return response

    def _parse_account(self, payload: dict) -> AccountSnapshot:
        subscription_id = payload.get("current_subscription_id")
        subscriptions = payload.get("subscriptions")
        if not isinstance(subscriptions, list):
            raise ProviderProtocolError("OddsPapi account response has no subscriptions list")

        active = None
        if subscription_id:
            active = next(
                (
                    item
                    for item in subscriptions
                    if item.get("subscription_id") == subscription_id
                ),
                None,
            )
        if active is None:
            active = next(
                (item for item in subscriptions if item.get("is_active") is True),
                None,
            )
        if active is None:
            raise ProviderProtocolError(
                "OddsPapi account response has no active subscription"
            )

        bookmakers_raw = active.get("bookmakers") or {}
        bookmakers = {
            slug: BookmakerEntitlement.model_validate(entitlement)
            for slug, entitlement in bookmakers_raw.items()
        }

        return AccountSnapshot(
            subscription_id=active["subscription_id"],
            request_limit=int(active.get("request_limit") or 0),
            request_count=int(active.get("request_count") or 0),
            sport_ids=[int(item) for item in active.get("sport_ids") or []],
            bookmakers=bookmakers,
            websocket_access=int(active.get("websocket_access") or 0),
            last_request=active.get("last_request"),
        )

    async def get_account(self, *, force: bool = False) -> AccountSnapshot:
        now = time.monotonic()
        if (
            not force
            and self._account_cache is not None
            and now - self._account_cached_at < self._account_cache_seconds
        ):
            return self._account_cache

        response = await self._request(
            "/v4/account",
            params=None,
            endpoint="account",
            billable=False,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ProviderProtocolError("OddsPapi account response is not an object")

        snapshot = self._parse_account(payload)
        self._account_cache = snapshot
        self._account_cached_at = time.monotonic()
        self._projected_request_count = snapshot.request_count
        return snapshot

    async def _quota_state(self) -> tuple[AccountSnapshot, int]:
        account = await self.get_account()
        observed = account.request_count
        if self._projected_request_count is not None:
            observed = max(observed, self._projected_request_count)
        remaining = max(account.request_limit - observed, 0)
        return account, remaining

    async def _ensure_billable_capacity(self) -> None:
        account, remaining = await self._quota_state()
        if remaining <= 0:
            raise ProviderQuotaExhausted(
                f"OddsPapi quota exhausted ({account.request_limit}/{account.request_limit})"
            )
        if remaining <= self._quota_reserve:
            raise QuotaReserveReached(
                f"OddsPapi reserve protected: {remaining} requests remain; "
                f"reserve is {self._quota_reserve}"
            )

    async def _ensure_provider_available(self) -> None:
        _account, remaining = await self._quota_state()
        if remaining <= 0:
            raise ProviderQuotaExhausted(
                "OddsPapi blocks free historical endpoints after the regular quota is exhausted"
            )

    @staticmethod
    def _bookmaker_param(
        bookmakers: Sequence[str] | None,
        *,
        max_count: int | None = None,
    ) -> str | None:
        if not bookmakers:
            return None
        cleaned = [item.strip() for item in bookmakers if item.strip()]
        if max_count is not None and len(cleaned) > max_count:
            raise ValueError(
                f"At most {max_count} bookmakers are allowed for this endpoint"
            )
        return ",".join(cleaned)

    @staticmethod
    def _utc_iso(value: datetime) -> str:
        if value.tzinfo is None:
            raise ValueError("Fixture timestamps must be timezone-aware")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    async def get_fixtures(
        self,
        *,
        sport_id: int,
        from_time: datetime,
        to_time: datetime,
        status_id: int | None = None,
        has_odds: bool | None = None,
        bookmakers: Sequence[str] | None = None,
    ) -> list[Fixture]:
        if to_time <= from_time:
            raise ValueError("to_time must be after from_time")
        if to_time - from_time >= timedelta(days=10):
            raise ValueError("OddsPapi sport fixture windows must be under 10 days")

        bookmaker_param = self._bookmaker_param(bookmakers)
        await self._ensure_billable_capacity()

        params: dict[str, object] = {
            "sportId": sport_id,
            "from": self._utc_iso(from_time),
            "to": self._utc_iso(to_time),
        }
        if status_id is not None:
            params["statusId"] = status_id
        if has_odds is not None:
            params["hasOdds"] = str(has_odds).lower()
        if bookmaker_param:
            params["bookmakers"] = bookmaker_param

        response = await self._request(
            "/v4/fixtures",
            params=params,
            endpoint="fixtures",
            billable=True,
        )
        payload = response.json()
        if not isinstance(payload, list):
            raise ProviderProtocolError("OddsPapi fixtures response is not a list")
        return [Fixture.model_validate(item) for item in payload]

    async def get_odds(
        self,
        fixture_id: str,
        *,
        bookmakers: Sequence[str] | None = None,
    ) -> dict:
        bookmaker_param = self._bookmaker_param(bookmakers)
        await self._ensure_billable_capacity()

        params: dict[str, object] = {
            "fixtureId": fixture_id,
            "language": "en",
            "verbosity": 3,
        }
        if bookmaker_param:
            params["bookmakers"] = bookmaker_param

        response = await self._request(
            "/v4/odds",
            params=params,
            endpoint="odds",
            billable=True,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ProviderProtocolError("OddsPapi odds response is not an object")
        return payload

    async def get_odds_by_tournaments(
        self,
        tournament_ids: Sequence[int],
        *,
        bookmakers: Sequence[str],
    ) -> dict | list:
        if not tournament_ids:
            raise ValueError("At least one tournament ID is required")
        bookmaker_param = self._bookmaker_param(bookmakers, max_count=3)
        await self._ensure_billable_capacity()

        response = await self._request(
            "/v4/odds-by-tournaments",
            params={
                "tournamentIds": ",".join(str(item) for item in tournament_ids),
                "bookmakers": bookmaker_param,
            },
            endpoint="odds_by_tournaments",
            billable=True,
        )
        payload = response.json()
        if not isinstance(payload, (dict, list)):
            raise ProviderProtocolError(
                "OddsPapi tournament odds response has unexpected shape"
            )
        return payload

    async def get_historical_odds(
        self,
        fixture_id: str,
        *,
        bookmakers: Sequence[str],
        etag: str | None = None,
    ) -> tuple[dict | None, str | None, bool]:
        bookmaker_param = self._bookmaker_param(bookmakers, max_count=3)
        await self._ensure_provider_available()

        headers = {"If-None-Match": etag} if etag else None
        response = await self._request(
            "/v4/historical-odds",
            params={"fixtureId": fixture_id, "bookmakers": bookmaker_param},
            endpoint="historical",
            billable=False,
            headers=headers,
        )
        response_etag = response.headers.get("ETag")
        if response.status_code == 304:
            return None, response_etag or etag, True

        payload = response.json()
        if not isinstance(payload, dict):
            raise ProviderProtocolError(
                "OddsPapi historical response is not an object"
            )
        return payload, response_etag, False
