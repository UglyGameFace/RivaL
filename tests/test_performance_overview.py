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
    monkeypatch.setattr(home_module, "_performance_benefit", lambda *_: {
        "status": "insufficient", "targets": 198, "days": 2,
    })
    monkeypatch.setattr(home_module, "_performance_lineage", lambda *_: {
        "lineage_proven": 10, "historically_seeded": 0,
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
            "📈 BTC exchange predictions (up or down?)",
            "🎯 Polymarket / Kalshi predictions",
            "🧠 Is it learning?",
            "📡 Is everything running?",
        ]
        assert "Does it make money?** Not proven yet" in panel.description
        assert "Better than a simple guess?** No, not yet" in fields["📈 BTC exchange predictions (up or down?)"]
        assert "(Error score: RivaL 0.310, simple guess 0.240; lower is better." in (
            fields["📈 BTC exchange predictions (up or down?)"]
        )
        assert "current model only" in fields["📈 BTC exchange predictions (up or down?)"]
        assert "1 couldn't be scored" in fields["📈 BTC exchange predictions (up or down?)"]
        assert "Better than Polymarket's own odds?** No, not yet" in fields["🎯 Polymarket / Kalshi predictions"]
        assert "(Error score: RivaL" in fields["🎯 Polymarket / Kalshi predictions"]
        assert "Past results studied:** 402 (history, not live predictions)" in fields["🎯 Polymarket / Kalshi predictions"]
        assert "Getting more accurate?** Too early to tell" in fields["🧠 Is it learning?"]
        assert "Built from the 6-year history?** 0 of the last 10 predictions" in fields["🧠 Is it learning?"]
        assert "Learning loop:** ✅ running" in fields["🧠 Is it learning?"]
        assert "Price feeds:** ✅ 1 of 1 exchanges up to date" in fields["📡 Is everything running?"]
        assert "Prediction cycle:" in fields["📡 Is everything running?"]
        assert max(map(len, fields.values())) < 1024
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_slow_or_broken_learning_cannot_hide_exchange_or_cycle(tmp_path, monkeypatch):
    bot, home = _make_home(tmp_path)
    now = datetime.now(UTC)
    monkeypatch.setattr(home_module, "_performance_exchange", lambda *_: _score())
    monkeypatch.setattr(home_module, "_performance_benefit", lambda *_:
                        (_ for _ in ()).throw(RuntimeError("learning read failure")))
    monkeypatch.setattr(home_module, "_performance_lineage", lambda *_:
                        (_ for _ in ()).throw(RuntimeError("lineage read failure")))
    monkeypatch.setattr(home_module, "_performance_cycles", lambda *_: {
        "forecast": {"status": "recorded", "checked_at": now, "issued": 7, "expired": 0},
        "learning": {"status": "unavailable"},
    })

    def bad_feeds():
        raise RuntimeError("feed read failure")

    home.market.feed_health = bad_feeds
    try:
        fields = _fields(await home.render())
        assert "Better than a simple guess?** No, not yet" in fields["📈 BTC exchange predictions (up or down?)"]
        assert "Getting more accurate?** Couldn't check right now" in fields["🧠 Is it learning?"]
        assert "Learning loop:** couldn't check right now" in fields["🧠 Is it learning?"]
        assert "Price feeds:** couldn't check right now" in fields["📡 Is everything running?"]
        assert "made 7 predictions" in fields["📡 Is everything running?"]
        assert "Better than Polymarket's own odds?** No, not yet" in fields["🎯 Polymarket / Kalshi predictions"]
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


class _MappingRowConnection:
    """Fake the actual psycopg dict_row contract, not sqlite.Row[0]."""

    def __init__(self, table, result):
        self.table, self.result = table, result

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, *_args):
        assert self.table in sql
        return self

    def fetchone(self):
        return self.result


class _MappingRowStore:
    def __init__(self, learning, forecast):
        self.rows = {
            "perp_learning_cycle_health": learning,
            "perp_forward_cycle_diagnostics": forecast,
        }

    def connect(self):
        # The production readers open separate connections per query. Return
        # psycopg-style dictionaries with no integer keys.
        store = self

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql):
                for table, row in store.rows.items():
                    if table in sql:
                        if isinstance(row, Exception):
                            raise row
                        return _MappingRowConnection(table, row)
                raise AssertionError("unexpected table")
        return Connection()


def test_postgres_dictionary_rows_make_both_worker_receipts_visible():
    now = datetime.now(UTC)
    market = SimpleNamespace(store=_MappingRowStore(
        learning={
            "completed_at": now.isoformat(), "math_ok": 1,
            "model_review_ok": 1, "math_issued": 12, "math_settled": 9,
        },
        forecast={
            "checked_at": now.isoformat(), "issued_forecasts": 12,
            "target_expired": 2,
        },
    ))
    result = home_module._performance_cycles(market)
    assert result["learning"] == {
        "status": "recorded", "checked_at": now, "math_ok": True,
        "model_review_ok": True, "issued": 12, "settled": 9,
    }
    assert result["forecast"] == {
        "status": "recorded", "checked_at": now, "issued": 12, "expired": 2,
    }


def test_missing_forecast_table_does_not_hide_learning_receipt():
    now = datetime.now(UTC)
    market = SimpleNamespace(store=_MappingRowStore(
        learning={
            "completed_at": now.isoformat(), "math_ok": 0,
            "model_review_ok": 1, "math_issued": 0, "math_settled": 4,
        },
        forecast=RuntimeError("missing forecast diagnostics table"),
    ))
    result = home_module._performance_cycles(market)
    assert result["forecast"]["status"] == "unavailable"
    assert result["learning"]["status"] == "recorded"
    assert result["learning"]["math_ok"] is False


def test_empty_cycle_tables_are_absent_not_misreported_as_healthy():
    market = SimpleNamespace(store=_MappingRowStore(learning=None, forecast=None))
    result = home_module._performance_cycles(market)
    assert result == {"forecast": None, "learning": None}


@pytest.mark.asyncio
async def test_timed_out_improvement_still_shows_training_lineage(tmp_path, monkeypatch):
    bot, home = _make_home(tmp_path)
    now = datetime.now(UTC)
    monkeypatch.setattr(home_module, "_performance_exchange", lambda *_: _score())
    monkeypatch.setattr(home_module, "_performance_benefit", lambda *_:
                        (_ for _ in ()).throw(TimeoutError("independent score unavailable")))
    monkeypatch.setattr(home_module, "_performance_lineage", lambda *_: {
        "lineage_proven": 100, "historically_seeded": 0,
    })
    monkeypatch.setattr(home_module, "_performance_cycles", lambda *_: {
        "learning": {"status": "recorded", "checked_at": now,
                     "math_ok": True, "model_review_ok": True, "issued": 108, "settled": 85},
        "forecast": {"status": "recorded", "checked_at": now,
                     "issued": 108, "expired": 0},
    })
    try:
        field = _fields(await home.render())["🧠 Is it learning?"]
        assert "Couldn't check right now" in field or "Still calculating" in field
        assert "0 of the last 100 predictions" in field
        assert "all coins and timeframes" in field
    finally:
        await _close(bot, home)
