"""Round-4 audit quick fixes: each one pinned by a behavioural test."""
from __future__ import annotations

from datetime import UTC, datetime

from parlay_bot.discord_app import auto_hunt
from parlay_bot.discord_app.status_card import STARTUP_GRACE, STREAM_STARTUP_GRACE, plain
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


def test_xrp_ai_research_can_be_saved_and_read_back(tmp_path):
    store = SQLiteHotStore(tmp_path / "xrp.sqlite")
    market = PerpetualMarketStore(store)
    try:
        market.initialize()
        now = datetime(2026, 11, 5, 23, 0, tzinfo=UTC)
        market.save_ai_research(
            asset="XRP", model="LiquidAI/LFM3-1.7B-GGUF:Q4_K_M", evidence_sha256="b" * 64,
            issued_at=now, direction="uncertain", confidence="low",
            thesis="Mixed venue returns.", risk="Thin depth.", source_count=3,
        )
        assert market.cached_ai_research(
            asset="XRP", model="LiquidAI/LFM3-1.7B-GGUF:Q4_K_M", evidence_sha256="b" * 64,
        ) is not None
    finally:
        store.close()


def test_auto_hunt_alerts_are_off_unless_explicitly_enabled():
    service = auto_hunt.AutoHuntService(bot=None, market=None, quotes=None, store=None)
    assert service.alerts_enabled is False


def test_stream_grace_and_startup_noise_window_are_separate_settings():
    assert STREAM_STARTUP_GRACE.total_seconds() == 300
    assert STARTUP_GRACE.total_seconds() == 600


def test_plain_keeps_the_exchange_and_never_invents_a_cause():
    line = plain("RivaL streaming forward proof failed venue=okx asset=BTC")
    assert line == "After-fee check failed (okx/BTC)"
    assert "no live price" not in line
    assert plain("RivaL perp collection FAILED venue=gate asset=XRP reason=timeout") \
        .startswith("gate/XRP")
