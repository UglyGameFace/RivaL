"""Owner-facing shadow learning state does not invent forecast accuracy.

Product pass PR-5: this content was Math's "Is RivaL learning?" section;
it now lives under Results → Test models as the "shadow" detail topic,
and a view still pointing at the old Math section redirects there.
"""
from __future__ import annotations

import pytest

from parlay_bot.discord_app.research_dashboard import ResearchDashboard
from parlay_bot.storage.hot import SQLiteHotStore


@pytest.mark.asyncio
async def test_learning_question_discloses_automatic_shadow_status(tmp_path):
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore

    market = PerpetualMarketStore(store)
    dashboard = ResearchDashboard(
        owner_id=123, market=market, ai=None,
        venues=("hyperliquid",), shadow_enabled=True,
    )
    try:
        # A stale view/deep link at the old Math section lands on the
        # consolidated topic, never a dead section.
        dashboard.screen = "math"
        dashboard.section = "learning"
        dashboard._rebuild()
        assert dashboard.screen == "accuracy"
        assert dashboard.section == "better"
        assert dashboard.detail == "shadow"
        rendered = (await dashboard.render()).to_dict()
        fields = {field["name"]: field["value"] for field in rendered["fields"]}
        assert "Automatic test model evaluation" in fields
        assert "Waiting for at least" in fields["Automatic test model evaluation"]
        assert "verified production signals" not in fields["Main model"]
        assert "no model is approved" in fields["Main model"].lower()
    finally:
        dashboard.stop()
        store.close()


@pytest.mark.asyncio
async def test_learning_question_reports_when_background_learning_disabled(tmp_path):
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore

    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    dashboard = ResearchDashboard(
        owner_id=123, market=PerpetualMarketStore(store), ai=None,
        venues=("hyperliquid",), shadow_enabled=False,
    )
    try:
        dashboard.screen = "math"
        dashboard.section = "learning"
        dashboard._rebuild()
        assert dashboard.detail == "shadow"
        rendered = (await dashboard.render()).to_dict()
        fields = {field["name"]: field["value"] for field in rendered["fields"]}
        assert "disabled" in fields["Main model"]
        assert "Automatic test model evaluation" not in fields
    finally:
        dashboard.stop()
        store.close()
