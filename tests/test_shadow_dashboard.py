"""Owner-facing shadow learning state does not invent forecast accuracy."""
from __future__ import annotations

import pytest

from parlay_bot.discord_app.research_dashboard import ResearchDashboard
from parlay_bot.storage.hot import SQLiteHotStore


@pytest.mark.asyncio
async def test_math_panel_discloses_automatic_shadow_status(tmp_path):
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore

    market = PerpetualMarketStore(store)
    dashboard = ResearchDashboard(
        owner_id=123, market=market, ai=None,
        venues=("hyperliquid",), shadow_enabled=True,
    )
    try:
        dashboard.section = "learning"
        dashboard._rebuild()
        rendered = (await dashboard.render()).to_dict()
        fields = {field["name"]: field["value"] for field in rendered["fields"]}
        assert "Automatic shadow evaluation" in fields
        assert "Waiting for at least" in fields["Automatic shadow evaluation"]
        assert "verified production signals" not in fields["Mathematical model"]
        assert "no model is approved" in fields["Mathematical model"].lower()
    finally:
        dashboard.stop()
        store.close()


@pytest.mark.asyncio
async def test_math_panel_reports_when_background_learning_disabled(tmp_path):
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore

    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    dashboard = ResearchDashboard(
        owner_id=123, market=PerpetualMarketStore(store), ai=None,
        venues=("hyperliquid",), shadow_enabled=False,
    )
    try:
        dashboard.section = "learning"
        dashboard._rebuild()
        rendered = (await dashboard.render()).to_dict()
        fields = {field["name"]: field["value"] for field in rendered["fields"]}
        assert "disabled" in fields["Mathematical model"]
        assert "Automatic shadow evaluation" not in fields
    finally:
        dashboard.stop()
        store.close()
