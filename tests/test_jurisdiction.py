from __future__ import annotations

import httpx
import pytest

from parlay_bot.jurisdiction.catalog_us import verified_us_sportsbooks
from parlay_bot.jurisdiction.onboarding import LocationOnboardingService
from parlay_bot.jurisdiction.registry import JurisdictionRegistry
from parlay_bot.jurisdiction.states import normalize_state, state_name
from parlay_bot.jurisdiction.zip_lookup import ZipLookupError, ZipStateResolver
from parlay_bot.storage.hot import SQLiteHotStore


def test_state_normalization_accepts_code_name_and_dc_alias() -> None:
    assert normalize_state("ct") == "CT"
    assert normalize_state("Connecticut") == "CT"
    assert normalize_state("Washington D.C.") == "DC"
    assert state_name("ny") == "New York"


def test_verified_connecticut_catalog_only_returns_confirmed_online_books() -> None:
    registry = JurisdictionRegistry(verified_us_sportsbooks())

    assert registry.eligible_books("CT") == ["draftkings", "fanduel", "fanatics"]


@pytest.mark.asyncio
async def test_zip_resolver_returns_state_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/us/06001"
        return httpx.Response(
            200,
            json={
                "post code": "06001",
                "country": "United States",
                "places": [
                    {
                        "place name": "Avon",
                        "state": "Connecticut",
                        "state abbreviation": "CT",
                    }
                ],
            },
            request=request,
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://api.zippopotam.us",
    ) as client:
        resolver = ZipStateResolver(client=client)
        assert await resolver.resolve("06001") == "CT"


@pytest.mark.asyncio
async def test_invalid_zip_never_calls_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network should not be called")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://api.zippopotam.us",
    ) as client:
        resolver = ZipStateResolver(client=client)
        with pytest.raises(ZipLookupError):
            await resolver.resolve("601")


@pytest.mark.asyncio
async def test_zip_onboarding_persists_state_but_not_zip(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "post code": "06001",
                "country": "United States",
                "places": [{"state abbreviation": "CT"}],
            },
            request=request,
        )

    db_path = tmp_path / "rival.sqlite"
    store = SQLiteHotStore(db_path)
    registry = JurisdictionRegistry(verified_us_sportsbooks())
    transport = httpx.MockTransport(handler)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://api.zippopotam.us",
    ) as client:
        service = LocationOnboardingService(
            store=store,
            registry=registry,
            zip_resolver=ZipStateResolver(client=client),
        )
        result = await service.choose_zip(
            platform="discord",
            user_id="123",
            zip_code="06001",
        )

    saved = store.get_user_jurisdiction(platform="discord", user_id="123")
    assert result.state_code == "CT"
    assert result.eligible_books == ("draftkings", "fanduel", "fanatics")
    assert saved is not None
    assert saved["state_code"] == "CT"
    assert saved["source"] == "zip"

    with store.connect() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(user_jurisdictions)").fetchall()
        }
    assert "zip" not in columns
    assert "zip_code" not in columns
    assert b"06001" not in db_path.read_bytes()


def test_state_onboarding_does_not_require_zip_lookup(tmp_path) -> None:
    class ResolverThatMustNotRun:
        async def resolve(self, _zip_code: str) -> str:
            raise AssertionError("ZIP resolver should not run for state selection")

    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    service = LocationOnboardingService(
        store=store,
        registry=JurisdictionRegistry(verified_us_sportsbooks()),
        zip_resolver=ResolverThatMustNotRun(),
    )

    result = service.choose_state(
        platform="discord",
        user_id="456",
        state="Connecticut",
    )

    assert result.state_code == "CT"
    assert store.get_user_jurisdiction(platform="discord", user_id="456")["state_code"] == "CT"
