from __future__ import annotations

import httpx
import pytest

from parlay_bot.providers.errors import QuotaReserveReached
from parlay_bot.providers.sportsgameodds import SportsGameOddsClient


def usage_payload(*, current: int = 100, maximum: int = 2500) -> dict:
    return {
        "success": True,
        "data": {
            "tier": "amateur",
            "rateLimits": {
                "per-month": {
                    "max-entities": maximum,
                    "current-entities": current,
                },
                "per-minute": {
                    "max-requests": 10,
                    "current-requests": 1,
                },
            },
        },
    }


@pytest.mark.asyncio
async def test_usage_parses_entity_and_request_limits() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/account/usage"
        assert request.headers["x-api-key"] == "secret"
        assert "api" not in request.url.params
        return httpx.Response(200, json=usage_payload(), request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://api.sportsgameodds.com/v2",
    ) as http_client:
        client = SportsGameOddsClient(
            "secret",
            client=http_client,
            request_cooldown_seconds=0,
        )
        usage = await client.get_usage()

    assert usage.tier == "amateur"
    assert usage.monthly_max_entities == 2500
    assert usage.monthly_current_entities == 100
    assert usage.monthly_remaining_entities == 2400
    assert usage.minute_max_requests == 10
    assert usage.minute_current_requests == 1


@pytest.mark.asyncio
async def test_event_request_uses_header_filters_and_cursor() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers["x-api-key"] == "secret"
        assert "apiKey" not in request.url.params

        if request.url.path == "/account/usage":
            return httpx.Response(200, json=usage_payload(), request=request)

        assert request.url.path == "/events"
        assert request.url.params["leagueID"] == "NBA,NFL"
        assert request.url.params["bookmakerID"] == "draftkings,fanduel"
        assert request.url.params["oddsAvailable"] == "true"
        assert request.url.params["started"] == "false"
        assert request.url.params["cancelled"] == "false"
        assert request.url.params["includeAltLines"] == "false"
        assert request.url.params["limit"] == "25"
        assert request.url.params["cursor"] == "cursor-1"
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [{"eventID": "e1"}],
                "nextCursor": "cursor-2",
                "notice": "test notice",
            },
            request=request,
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://api.sportsgameodds.com/v2",
    ) as http_client:
        client = SportsGameOddsClient(
            "secret",
            client=http_client,
            request_cooldown_seconds=0,
        )
        events, cursor, notice = await client.get_events(
            league_ids=["NBA", "NFL"],
            bookmaker_ids=["draftkings", "fanduel"],
            limit=25,
            cursor="cursor-1",
        )

    assert calls == ["/account/usage", "/events"]
    assert events == [{"eventID": "e1"}]
    assert cursor == "cursor-2"
    assert notice == "test notice"


@pytest.mark.asyncio
async def test_monthly_reserve_blocks_event_call_before_spending_entities() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json=usage_payload(current=2300, maximum=2500),
            request=request,
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://api.sportsgameodds.com/v2",
    ) as http_client:
        client = SportsGameOddsClient(
            "secret",
            monthly_entity_reserve=250,
            client=http_client,
            request_cooldown_seconds=0,
        )
        with pytest.raises(QuotaReserveReached):
            await client.get_events(league_ids=["NBA"], limit=25)

    assert calls == ["/account/usage"]


@pytest.mark.asyncio
async def test_limit_is_trimmed_to_safe_remaining_entity_budget() -> None:
    observed_limit: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_limit
        if request.url.path == "/account/usage":
            return httpx.Response(
                200,
                json=usage_payload(current=2235, maximum=2500),
                request=request,
            )
        observed_limit = request.url.params["limit"]
        return httpx.Response(
            200,
            json={"success": True, "data": []},
            request=request,
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://api.sportsgameodds.com/v2",
    ) as http_client:
        client = SportsGameOddsClient(
            "secret",
            monthly_entity_reserve=250,
            client=http_client,
            request_cooldown_seconds=0,
        )
        await client.get_events(league_ids=["NBA"], limit=25)

    assert observed_limit == "15"
