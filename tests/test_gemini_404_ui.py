"""Production Gemini 404 and Discord menu rebind regression coverage."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from parlay_bot.discord_app.research_dashboard import ResearchDashboard
from parlay_bot.perpetuals.gemini_research import GeminiResearch, ResearchUnavailable
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


class _Response:
    def __init__(self):
        self.deferred = False

    async def defer(self):
        self.deferred = True


class _Interaction:
    def __init__(self):
        self.user = SimpleNamespace(id=42)
        self.response = _Response()
        self.embed = None
        self.view = None

    async def edit_original_response(self, *, embed, view):
        self.embed = embed
        self.view = view


class _FailingAi:
    available = True
    model = "gemini-3.8-flash"

    async def ask(self, *, asset, venues):
        assert asset == "BTC" and venues == ("hyperliquid", "dydx")
        raise ResearchUnavailable("Gemini model unavailable (HTTP 404).")

    async def list_advertised_models(self):
        return ("gemini-3.8-flash", "gemini-3.5-flash-lite")


def _button(dashboard, custom_id):
    return next(item for item in dashboard.children if item.custom_id == custom_id)


@pytest.mark.asyncio
async def test_failed_gemini_keeps_rebuilt_discord_view_and_shows_error():
    dashboard = ResearchDashboard(
        owner_id=42, market=object(), ai=_FailingAi(),
        venues=("hyperliquid", "dydx"),
    )
    dashboard.screen = "ai"
    dashboard._rebuild()
    button = _button(dashboard, "rival:research:analyze")
    interaction = _Interaction()
    await button.callback(interaction)
    assert button.view is None  # clear_items detached the old button
    assert interaction.response.deferred
    assert interaction.view is dashboard
    assert interaction.embed.title == "RivaL  |  BTC Research"
    assert "HTTP 404" in next(
        field.value for field in interaction.embed.fields
        if field.name == "Analysis status"
    )
    assert _button(dashboard, "rival:research:analyze").view is dashboard


@pytest.mark.asyncio
async def test_model_diagnostic_survives_rebuild_and_labels_unverified_metadata():
    dashboard = ResearchDashboard(
        owner_id=42, market=object(), ai=_FailingAi(),
        venues=("hyperliquid", "dydx"),
    )
    dashboard.screen = "ai"
    dashboard._rebuild()
    button = _button(dashboard, "rival:research:models")
    interaction = _Interaction()
    await button.callback(interaction)
    assert button.view is None
    assert interaction.view is dashboard
    assert "gemini-3.8-flash" in next(
        field.value for field in interaction.embed.fields
        if field.name == "Analysis status"
    )
    assert "may still reject" in next(
        field.value for field in interaction.embed.fields
        if field.name == "Analysis status"
    )


@pytest.mark.asyncio
async def test_legacy_25_flash_is_migrated_and_404_pauses_future_calls(
    monkeypatch, tmp_path,
):
    import parlay_bot.perpetuals.gemini_research as module

    monkeypatch.setattr(
        module, "build_evidence",
        lambda *args, **kwargs: (
            {"asset": "BTC", "sources": [{"venue": "hyperliquid"},
                                        {"venue": "dydx"}, {"venue": "bitget"}]},
            "c" * 64,
        ),
    )
    store = SQLiteHotStore(tmp_path / "gemini.sqlite")
    market = PerpetualMarketStore(store)
    seen = []

    def reply(request):
        seen.append(request)
        assert request.url.path.endswith(
            "/models/gemini-3.8-flash:generateContent"
        )
        assert request.headers["x-goog-api-key"] == "test-key-never-log"
        return httpx.Response(404)

    raw = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    service = GeminiResearch(
        market, api_key="test-key-never-log", client=raw,
        model="gemini-2.5-flash",
    )
    assert service.model == "gemini-3.8-flash"
    try:
        with pytest.raises(ResearchUnavailable, match="HTTP 404"):
            await service.ask(asset="BTC", venues=("hyperliquid", "dydx", "bitget"))
        with pytest.raises(ResearchUnavailable, match="temporarily paused"):
            await service.ask(asset="BTC", venues=("hyperliquid", "dydx", "bitget"))
        assert len(seen) == 1
    finally:
        await raw.aclose()
        store.close()


@pytest.mark.asyncio
async def test_advertised_models_read_only_metadata_cached_per_hour(tmp_path):
    store = SQLiteHotStore(tmp_path / "gemini.sqlite")
    market = PerpetualMarketStore(store)
    requests = []

    def reply(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/v1beta/models"
        assert request.headers["x-goog-api-key"] == "fake-key"
        return httpx.Response(200, json={"models": [
            {"name": "models/gemini-3.8-flash",
             "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.5-flash-lite",
             "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-embedding", "supportedGenerationMethods":
             ["embedContent"]},
        ]})

    raw = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    service = GeminiResearch(market, api_key="fake-key", client=raw)
    try:
        result = await service.list_advertised_models()
        assert result == ("gemini-3.5-flash-lite", "gemini-3.8-flash")
        assert await service.list_advertised_models() == result
        assert len(requests) == 1
    finally:
        await raw.aclose()
        store.close()
