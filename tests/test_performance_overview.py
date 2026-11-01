"""Performance scorecard: honest forward evidence and failure-isolated health."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from parlay_bot.discord_app import home as home_module
from parlay_bot.perpetuals.autonomous import AutonomousResearch
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from tests.test_guided_home import _client, _home


def _score(*, brier: float = .31, baseline: float = .24) -> dict:
    return {
        "last_7d": {5: {
            "decided": 7, "correct": 3, "hit_rate": 3 / 7,
            "issued": 8, "settled": 7, "scored": 6,
            "brier": brier, "baseline_brier": baseline,
        }},
    }


def _fields(embed) -> dict[str, str]:
    return {field.name: field.value for field in embed.fields}


def _make_home(tmp_path):
    bot = _client(tmp_path)
    home = _home(bot)
    now = datetime.now(UTC)
    home.market = SimpleNamespace(
        feed_health=lambda: [{
            "asset": "BTC", "venue": "hyperliquid", "status": "ok",
            "last_success_at": now.isoformat(),
            "last_candle_close": (now-timedelta(minutes=5)).isoformat(),
        }],
        store=bot.store,
    )
    home.venues = ("hyperliquid",)
    home.polymarket_status = lambda *_: {
        "settled_forward_contracts": 300,
        "learned_contracts": 402,
        "score_window_days": 7,
        "forward_brier": .2387,
        "market_baseline_brier": .2222,
    }
    home.navigate("performance")
    return bot, home


async def _close(bot, home):
    home.stop()
    await bot.zip_resolver.aclose()
    await bot.public_quotes.aclose()
    bot.store.close()


@pytest.mark.asyncio
async def test_scorecard_is_short_source_specific_and_skips_heavy_training(tmp_path, monkeypatch):
    bot, home = _make_home(tmp_path)
    now = datetime.now(UTC)

    def heavy_training(*_):
        raise AssertionError("full Drive/model summary must not run on Performance")

    home.training_status = heavy_training
    monkeypatch.setattr(home_module, "_performance_exchange", lambda *_: _score())
    monkeypatch.setattr(home_module, "_performance_learning", lambda *_: {
        "benefit": {"status": "insufficient", "targets": 198, "days": 2},
        "lineage": {"lineage_proven": 10, "historically_seeded": 0},
    })
    monkeypatch.setattr(home_module, "_performance_cycles", lambda *_: {
        "forecast": {
            "status": "recorded", "checked_at": now,
            "issued": 20, "expired": 0,
        },
        "learning": {
            "status": "recorded", "checked_at": now,
            "math_ok": True, "model_review_ok": True,
            "issued": 5, "settled": 4,
        },
    })
    try:
        panel = await home.render()
        fields = _fields(panel)
        assert list(fields) == [
            "📈 Exchange accuracy", "🎯 Polymarket",
            "🧠 Learning", "📡 System status",
        ]
        assert "NO VERIFIED TRADING EDGE" in panel.description
        assert "Not better than baseline" in fields["📈 Exchange accuracy"]
        assert "6/7 paired scores" in fields["📈 Exchange accuracy"]
        assert "Not better than market" in fields["🎯 Polymarket"]
        assert "402 historical outcomes (not forward calls)" in fields["🎯 Polymarket"]
        assert "Too few future outcomes" in fields["🧠 Learning"]
        assert "Historical seeds in recent verified lineage: 0/10" in fields["🧠 Learning"]
        assert "Engine: **Running**" in fields["🧠 Learning"]
        assert "Feeds: **1/1** fresh" in fields["📡 System status"]
        assert "Forecast check:" in fields["📡 System status"]
        assert max(map(len, fields.values())) < 1024
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_slow_or_broken_learning_cannot_hide_exchange_or_cycle(tmp_path, monkeypatch):
    bot, home = _make_home(tmp_path)
    now = datetime.now(UTC)
    monkeypatch.setattr(home_module, "_performance_exchange", lambda *_: _score())
    monkeypatch.setattr(home_module, "_performance_learning", lambda *_:
                        (_ for _ in ()).throw(RuntimeError("learning read failure")))
    monkeypatch.setattr(home_module, "_performance_cycles", lambda *_: {
        "forecast": {"status": "recorded", "checked_at": now, "issued": 7, "expired": 0},
        "learning": {"status": "unavailable"},
    })

    def bad_feeds():
        raise RuntimeError("feed read failure")

    home.market.feed_health = bad_feeds
    try:
        fields = _fields(await home.render())
        assert "Not better than baseline" in fields["📈 Exchange accuracy"]
        assert "14d comparison: read unavailable" in fields["🧠 Learning"]
        assert "Learning heartbeat: read unavailable" in fields["🧠 Learning"]
        assert "Feeds: **read unavailable**" in fields["📡 System status"]
        assert "**7** issued" in fields["📡 System status"]
        assert "Not better than market" in fields["🎯 Polymarket"]
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_persisted_learning_heartbeat_is_read_without_model_registry(tmp_path):
    bot = _client(tmp_path)
    market = PerpetualMarketStore(bot.store)
    autonomous = AutonomousResearch(market, ai=None, venues=())
    try:
        assert await autonomous.cycle() == (0, 0, 0)
        heartbeat = home_module._latest_learning_cycle(market)
        assert heartbeat is not None
        assert heartbeat["status"] == "recorded"
        assert heartbeat["math_ok"]
        assert heartbeat["model_review_ok"]
        assert heartbeat["issued"] == 0
    finally:
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()
