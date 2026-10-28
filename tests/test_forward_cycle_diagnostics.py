"""Why did RivaL issue zero predictions? Durable exact-cycle reason counters.

These measure the real prospective engine, not how many exchange calls returned
200 or how much historical data was backfilled in one batch.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.research_dashboard import ResearchDashboard
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


def seed(market, *, now: datetime, bars: int, late_backfill: bool = False):
    boundary = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
    market.initialize()
    with market.store.connect() as conn:
        for i in range(bars):
            opened = boundary - timedelta(minutes=5 * (bars - i))
            closed = opened + timedelta(minutes=5, milliseconds=-1)
            observed = now if late_backfill else closed + timedelta(milliseconds=1)
            p = 100 + i / 15 + (i % 9) / 8
            conn.execute(
                """INSERT INTO perp_market_candles
                   (venue,contract,quote,interval_minutes,opened_at,closed_at,
                    first_observed_at,open_price,high_price,low_price,close_price,
                    volume_base,trades)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "hyperliquid", "BTC", "USDC", 5,
                    opened.isoformat(), closed.isoformat(), observed.isoformat(),
                    p, p + 2, p - 2, p + 0.5, 10, 12,
                ),
            )


def test_empty_engine_cycle_is_recorded_in_postgres_compatible_schema(tmp_path):
    store = SQLiteHotStore(tmp_path / "empty.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        assert monitor.latest_diagnostics() is None
        assert monitor.run_once() == (0, 0)
        result = ForwardAccuracyMonitor(market).latest_diagnostics()
        assert result is not None
        assert result["discovered_streams"] == 0
        assert result["selected_streams"] == 0
        assert result["short_history"] == 0
        assert result["issued_forecasts"] == 0
        assert result["settled_forecasts"] == 0
    finally:
        store.close()


def test_short_history_and_bulk_backfill_do_not_create_false_issued_counts(tmp_path):
    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "short.sqlite")
    market = PerpetualMarketStore(store)
    try:
        seed(market, now=now, bars=50, late_backfill=True)
        monitor = ForwardAccuracyMonitor(market)
        assert monitor.run_once(now=now) == (0, 0)
        result = monitor.latest_diagnostics()
        assert result["discovered_streams"] == 1
        assert result["selected_streams"] == 1
        assert result["short_history"] == 1
        assert result["issued_forecasts"] == 0
        with store.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM perp_forward_audit"
            ).fetchone()["n"] == 0
    finally:
        store.close()


def test_live_forward_predictions_are_counted_and_duplicate_replays_explained(tmp_path):
    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "live.sqlite")
    market = PerpetualMarketStore(store)
    try:
        seed(market, now=now, bars=150)
        monitor = ForwardAccuracyMonitor(market)
        assert monitor.run_once(now=now) == (2, 0)
        first = monitor.latest_diagnostics()
        assert first["discovered_streams"] == 1
        assert first["selected_streams"] == 1
        assert first["issued_forecasts"] == 2
        assert first["duplicate_forecasts"] == 0
        assert first["insufficient_causal_labels"] == 0
        assert monitor.run_once(now=now) == (0, 0)
        again = ForwardAccuracyMonitor(market).latest_diagnostics()
        assert again["issued_forecasts"] == 0
        assert again["duplicate_forecasts"] == 2
        assert again["selected_streams"] == 1
        with store.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM perp_forward_cycle_diagnostics"
            ).fetchone()["n"] == 1
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM perp_forward_audit"
            ).fetchone()["n"] == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_mobile_accuracy_engine_exposes_measured_blocker_not_stale_guess(tmp_path):
    store = SQLiteHotStore(tmp_path / "dashboard.sqlite")
    market = PerpetualMarketStore(store)
    view = None
    try:
        seed(market, now=datetime.now(UTC), bars=42, late_backfill=True)
        ForwardAccuracyMonitor(market).run_once()
        view = ResearchDashboard(
            owner_id=42, market=market, ai=None,
            venues=("hyperliquid",), shadow_enabled=True,
        )
        view.screen = "accuracy"
        view.section = "engine"
        view._rebuild()
        embed = await view.render()
        assert len(embed.fields) <= 3
        details = next(
            f.value for f in embed.fields
            if f.name == "Forward issue audit · LIVE"
        )
        assert "short history **1**" in details.lower()
        assert "Discovered **1**" in details
        assert "Issued **0**" in details
        assert "stale" not in details.lower() or "stale **0**" in details.lower()
    finally:
        if view is not None:
            view.stop()
        store.close()
