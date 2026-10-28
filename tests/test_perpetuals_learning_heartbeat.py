"""RivaL auto-learning must leave durable proof of cycles and failures."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.research_dashboard import ResearchDashboard
from parlay_bot.perpetuals.autonomous import AutonomousResearch
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


@pytest.mark.asyncio
async def test_automatic_cycle_records_real_restart_safe_heartbeat(tmp_path):
    store = SQLiteHotStore(tmp_path / "autonomous.sqlite")
    market = PerpetualMarketStore(store)
    system = AutonomousResearch(market, ai=None, venues=())
    try:
        assert system.learning_heartbeat() is None
        issued, settled, ai_count = await system.cycle()
        assert (issued, settled, ai_count) == (0, 0, 0)
        restarted = AutonomousResearch(
            PerpetualMarketStore(store), ai=None, venues=(),
        )
        latest = restarted.learning_heartbeat()
        assert latest is not None
        assert latest["math_ok"] == 1
        assert latest["ai_settlement_ok"] == 1
        assert latest["model_review_ok"] == 1
        assert latest["gemini_ok"] == 1
        assert latest["math_issued"] == 0
        assert latest["math_settled"] == 0
        assert latest["ai_issued"] == 0
        when = datetime.fromisoformat(latest["completed_at"])
        assert when.tzinfo is not None
        assert datetime.now(UTC) - when < timedelta(minutes=1)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_learning_cycle_stage_failure_is_visible_not_silenced(tmp_path, monkeypatch):
    store = SQLiteHotStore(tmp_path / "errors.sqlite")
    market = PerpetualMarketStore(store)
    system = AutonomousResearch(market, ai=None, venues=())
    try:
        def failing_math():
            raise RuntimeError("simulated model failure")

        monkeypatch.setattr(system.forward, "run_once", failing_math)
        assert await system.cycle() == (0, 0, 0)
        result = system.learning_heartbeat()
        assert result is not None
        assert result["math_ok"] == 0
        assert result["ai_settlement_ok"] == 1
        assert result["gemini_ok"] == 1
        assert result["math_issued"] == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_accuracy_screen_reads_persisted_learning_cycle(tmp_path):
    store = SQLiteHotStore(tmp_path / "view.sqlite")
    market = PerpetualMarketStore(store)
    system = AutonomousResearch(market, ai=None, venues=())
    try:
        await system.cycle()
        panel = ResearchDashboard(
            owner_id=42, market=market, ai=None, venues=("hyperliquid",),
            shadow_enabled=True,
        )
        panel.screen = "accuracy"
        panel.section = "engine"
        panel._rebuild()
        embed = await panel.render()
        result = next(
            field.value for field in embed.fields
            if field.name == "Automatic learning engine · LIVE"
        )
        assert "Background research cycle completed" in result
        assert "math OK" in result
        assert "0** math forecasts" in result
        assert "not** that prior predictions became more accurate" in result
    finally:
        store.close()
