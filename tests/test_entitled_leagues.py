"""SportsGameOdds entitled sport discovery must never invent all-sports rights."""
from __future__ import annotations

import httpx
import pytest

from parlay_bot.providers.errors import ProviderProtocolError
from parlay_bot.providers.sportsgameodds import SportsGameOddsClient


@pytest.mark.asyncio
async def test_provider_entitled_leagues_exclude_disabled_and_non_sports_and_cache():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.path == "/leagues"
        assert not request.url.params
        return httpx.Response(200, json={
            "success": True,
            "data": [
                {"leagueID": "NFL", "sportID": "FOOTBALL", "name": "NFL", "enabled": True},
                {"leagueID": "EPL", "sportID": "SOCCER", "name": "Premier League", "enabled": True},
                {"leagueID": "UFC", "sportID": "MMA", "name": "UFC", "enabled": True},
                {"leagueID": "TV", "sportID": "NON_SPORTS", "name": "TV", "enabled": True},
                {"leagueID": "UNKNOWN", "sportID": "BASKETBALL"},
                {"leagueID": "NULL", "sportID": "TENNIS", "enabled": None},
                {"leagueID": "STRING", "sportID": "GOLF", "enabled": "true"},
                {"leagueID": "INACTIVE", "sportID": "GOLF", "enabled": False},
            ],
        })

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url='https://api.sportsgameodds.com')
    client = SportsGameOddsClient(
        "test-api-key", client=http, request_cooldown_seconds=0,
    )
    try:
        leagues = await client.get_available_leagues()
        assert {l["sport_id"] for l in leagues} == {"FOOTBALL", "SOCCER", "MMA"}
        assert len(leagues) == 3
        assert await client.get_available_leagues() == leagues
        assert len(requests) == 1
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_provider_catalog_fails_closed_on_malformed_data():
    http = httpx.AsyncClient(base_url='https://api.sportsgameodds.com', transport=httpx.MockTransport(
        lambda _req: httpx.Response(
            200, json={"success": True, "data": {"NFL": "not a list"}},
        ),
    ))
    client = SportsGameOddsClient("test", client=http, request_cooldown_seconds=0)
    try:
        with pytest.raises(ProviderProtocolError):
            await client.get_available_leagues()
    finally:
        await http.aclose()
