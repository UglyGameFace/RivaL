from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.providers.errors import QuotaReserveReached
from parlay_bot.providers.oddspapi import OddsPapiClient


def account_payload(*, count: int = 100, limit: int = 250) -> dict:
    return {
        "current_subscription_id": "sub-1",
        "subscriptions": [
            {
                "subscription_id": "sub-1",
                "is_active": True,
                "bookmakers": {
                    "draftkings": {
                        "has_live_odds": False,
                        "has_player_props": True,
                    }
                },
                "sport_ids": [2, 4],
                "websocket_access": 0,
                "request_limit": limit,
                "request_count": count,
                "last_request": "2026-09-28T12:00:00Z",
            }
        ],
    }


def zero_cooldowns() -> dict[str, float]:
    return {
        "account": 0,
        "fixtures": 0,
        "odds": 0,
        "historical": 0,
        "odds_by_tournaments": 0,
        "markets": 0,
    }


@pytest.mark.asyncio
async def test_account_parses_entitlements_and_hides_key_from_models() -> None:
    seen_api_key = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_api_key
        seen_api_key = request.url.params.get("apiKey")
        return httpx.Response(200, json=account_payload(), request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://api.oddspapi.io"
    ) as http_client:
        client = OddsPapiClient(
            "secret",
            client=http_client,
            cooldowns=zero_cooldowns(),
        )
        account = await client.get_account()

    assert seen_api_key == "secret"
    assert account.remaining_requests == 150
    assert account.bookmakers["draftkings"].has_player_props is True
    assert "secret" not in account.model_dump_json()


@pytest.mark.asyncio
async def test_billable_request_stops_at_configured_reserve() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json=account_payload(count=230, limit=250),
            request=request,
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://api.oddspapi.io"
    ) as http_client:
        client = OddsPapiClient(
            "secret",
            quota_reserve=20,
            client=http_client,
            cooldowns=zero_cooldowns(),
        )
        with pytest.raises(QuotaReserveReached):
            await client.get_odds("fixture-1")

    assert calls == ["/v4/account"]


@pytest.mark.asyncio
async def test_historical_request_can_use_free_endpoint_inside_reserve() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v4/account":
            return httpx.Response(
                200,
                json=account_payload(count=230, limit=250),
                request=request,
            )
        return httpx.Response(
            200,
            headers={"ETag": '"abc"'},
            json={"fixtureId": "fixture-1", "bookmakers": {}},
            request=request,
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://api.oddspapi.io"
    ) as http_client:
        client = OddsPapiClient(
            "secret",
            quota_reserve=20,
            client=http_client,
            cooldowns=zero_cooldowns(),
        )
        payload, etag, not_modified = await client.get_historical_odds(
            "fixture-1",
            bookmakers=["draftkings"],
        )

    assert calls == ["/v4/account", "/v4/historical-odds"]
    assert payload == {"fixtureId": "fixture-1", "bookmakers": {}}
    assert etag == '"abc"'
    assert not_modified is False


@pytest.mark.asyncio
async def test_fixture_window_rejects_ten_days_or_more_without_network_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network should not be called")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://api.oddspapi.io"
    ) as http_client:
        client = OddsPapiClient(
            "secret",
            client=http_client,
            cooldowns=zero_cooldowns(),
        )
        start = datetime(2026, 9, 1, tzinfo=UTC)
        with pytest.raises(ValueError):
            await client.get_fixtures(
                sport_id=4,
                from_time=start,
                to_time=start + timedelta(days=10),
            )


@pytest.mark.asyncio
async def test_historical_endpoint_rejects_more_than_three_books() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=account_payload(), request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://api.oddspapi.io"
    ) as http_client:
        client = OddsPapiClient(
            "secret",
            client=http_client,
            cooldowns=zero_cooldowns(),
        )
        with pytest.raises(ValueError):
            await client.get_historical_odds(
                "fixture-1",
                bookmakers=["a", "b", "c", "d"],
            )

    assert calls == []


@pytest.mark.asyncio
async def test_markets_endpoint_parses_catalog_and_counts_as_billable() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v4/account":
            return httpx.Response(200, json=account_payload(count=100, limit=250), request=request)
        if request.url.path == "/v4/markets":
            return httpx.Response(
                200,
                json=[
                    {
                        "marketId": 106,
                        "marketLength": 2,
                        "marketName": "Over Under Full Time",
                        "playerProp": False,
                        "sportId": 10,
                        "handicap": 0.5,
                        "period": "fulltime",
                        "marketType": "totals",
                        "outcomes": [
                            {"outcomeId": 106, "outcomeName": "Over"},
                            {"outcomeId": 107, "outcomeName": "Under"},
                        ],
                    }
                ],
                request=request,
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://api.oddspapi.io"
    ) as http_client:
        client = OddsPapiClient(
            "secret",
            client=http_client,
            cooldowns=zero_cooldowns(),
        )
        markets = await client.get_markets()

    assert calls == ["/v4/account", "/v4/markets"]
    assert markets[0].market_id == "106"
    assert markets[0].market_name == "Over Under Full Time"
    assert markets[0].outcomes[1].outcome_name == "Under"
