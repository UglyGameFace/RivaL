"""Explain collection vs live causal model-readiness without inventing skill."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.readiness import forecast_readiness
from parlay_bot.storage.hot import SQLiteHotStore


def _clock():
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    return now.replace(minute=now.minute // 5 * 5) + timedelta(minutes=2)


def _populate(
    market, *, now, count=100, venue="hyperliquid",
    historical_backfill=False,
):
    market.initialize()
    boundary = now.replace(second=0, microsecond=0)
    boundary = boundary.replace(minute=boundary.minute // 5 * 5)
    with market.store.connect() as conn:
        for i in range(count):
            opened = boundary - timedelta(minutes=5 * (count - i))
            closed = opened + timedelta(minutes=5, milliseconds=-1)
            # A late bulk import must not be counted as 100 live observations.
            observed = now if historical_backfill else (
                closed + timedelta(seconds=1)
            )
            p = 100 + i / 20 + (i % 5) / 10
            conn.execute(
                """INSERT INTO perp_market_candles
                   (venue,contract,quote,interval_minutes,opened_at,closed_at,
                    first_observed_at,open_price,high_price,low_price,close_price,
                    volume_base,trades)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (venue, "BTC", "USDC", 5,
                 opened.isoformat(), closed.isoformat(), observed.isoformat(),
                 p, p + 2, p - 2, p + 0.2, 12, 50),
            )
    market.record_feed_health(
        venue=venue, asset="BTC", status="ok", attempted_at=now,
        new_candles=0, total_candles=0,
        last_candle_close=boundary - timedelta(milliseconds=1),
    )


def test_nine_provider_connectivity_does_not_imply_nine_eligible_forecasts(tmp_path):
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    market = PerpetualMarketStore(store)
    now = _clock()
    try:
        _populate(market, now=now, count=45)
        readiness = forecast_readiness(
            market, asset="BTC",
            venues=("hyperliquid", "dydx", "binance"), now=now,
        )
        assert readiness["configured"] == 3
        assert readiness["eligible"] == 0
        assert readiness["best_live_streak"] == 45
        assert readiness["waiting_history"] == 1
        assert readiness["stale_or_blocked"] == 2
        assert readiness["streams"][0]["status"] == "needs_live_history"
        assert readiness["streams"][1]["status"] == "blocked_or_unavailable"
    finally:
        store.close()


def test_backfilled_observations_cannot_bootstrap_causal_math_accuracy(tmp_path):
    store = SQLiteHotStore(tmp_path / "backfill.sqlite")
    market = PerpetualMarketStore(store)
    now = _clock()
    try:
        _populate(market, now=now, count=150, historical_backfill=True)
        readiness = forecast_readiness(
            market, asset="BTC", venues=("hyperliquid",), now=now,
        )
        assert readiness["eligible"] == 0
        assert readiness["streams"][0]["observations"] == 150
        assert readiness["streams"][0]["recent_live_streak"] <= 2
        assert readiness["streams"][0]["status"] == "needs_live_history"
    finally:
        store.close()


def test_valid_forward_live_observations_can_make_shadow_math_eligible(tmp_path):
    store = SQLiteHotStore(tmp_path / "ready.sqlite")
    market = PerpetualMarketStore(store)
    now = _clock()
    try:
        _populate(market, now=now, count=150)
        result = forecast_readiness(
            market, asset="BTC", venues=("hyperliquid",), now=now,
        )
        assert result["best_live_streak"] == 150
        assert result["best_causal_5m"] >= 40
        assert result["best_evaluated_5m"] >= 20
        assert result["streams"][0]["status"] == "eligible_shadow"
        assert result["eligible"] == 1
        # Reading readiness should never create, settle, or promote a forecast.
        with store.connect() as conn:
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE name='perp_forward_audit'"
            ).fetchone()
            assert table is None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_live_accuracy_screen_explains_shortage_in_actual_database(tmp_path):
    from parlay_bot.discord_app.research_dashboard import ResearchDashboard

    store = SQLiteHotStore(tmp_path / "screen.sqlite")
    market = PerpetualMarketStore(store)
    now = _clock()
    view = None
    try:
        _populate(market, now=now, count=25)
        view = ResearchDashboard(
            owner_id=42, market=market, ai=None,
            venues=("hyperliquid", "binance"), shadow_enabled=True,
        )
        view.screen = "accuracy"
        view._rebuild()
        embed = await view.render()
        readiness = next(
            x.value for x in embed.fields
            if x.name == "Mathematical forecast readiness · LIVE"
        )
        assert "0/2" in readiness
        assert "25/65" in readiness
        assert "Stale/blocked" in readiness
        assert "No betting permission implied" in readiness
    finally:
        if view is not None:
            view.stop()
        store.close()
