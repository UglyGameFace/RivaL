"""Automatic shadow scoring uses real-known timestamps and never sends orders."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.shadow_monitor import (
    MIN_EVALUATED,
    MODEL,
    ShadowLearningMonitor,
)
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore

START = datetime(2026, 10, 27, 0, 0, tzinfo=UTC)


@pytest.fixture(params=["sqlite", "postgres"])
def monitor(request, tmp_path):
    if request.param == "postgres":
        dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
        if not dsn:
            pytest.skip("PostgreSQL service unavailable")
        store = PostgresHotStore(dsn)
    else:
        store = SQLiteHotStore(tmp_path / "shadow.sqlite")
    engine = ShadowLearningMonitor(store)
    engine.initialize()
    try:
        yield engine
    finally:
        # Only clean our own test fixtures. Never drop the production schemas.
        with store.connect() as conn:
            conn.execute(
                "DELETE FROM perp_shadow_evaluations WHERE contract IN (?,?)",
                ("BTC-SHADOW-TEST", "ETH-SHADOW-TEST"),
            )
            conn.execute(
                "DELETE FROM perp_market_candles WHERE contract IN (?,?)",
                ("BTC-SHADOW-TEST", "ETH-SHADOW-TEST"),
            )
        store.close()


def seed(
    monitor, *, contract="BTC-SHADOW-TEST",
    count=140, backfill=False, venue="hyperliquid",
):
    with monitor.store.connect() as conn:
        for i in range(count):
            opened = START + timedelta(minutes=5 * i)
            closed = opened + timedelta(minutes=5) - timedelta(milliseconds=1)
            observed = (START + timedelta(minutes=5 * count, seconds=30)
                        if backfill else closed + timedelta(seconds=1))
            price = 100 + i / 10
            conn.execute(
                """INSERT INTO perp_market_candles
                   (venue,contract,quote,interval_minutes,opened_at,
                    closed_at,first_observed_at,open_price,high_price,low_price,
                    close_price,volume_base,trades)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (venue, contract, "USDC", 5, opened.isoformat(),
                 closed.isoformat(), observed.isoformat(), price,
                 price + 1, price - 1, price + 0.2, 10 + i % 5, 12),
            )
    return START + timedelta(minutes=5 * count + 2)


def test_scoring_is_automatic_restart_safe_and_evidence_only(monitor):
    now = seed(monitor)
    assert monitor.run_once(now=now) == 3
    saved = monitor.latest(asset="BTC")
    assert len(saved) == 3
    assert {x["horizon_minutes"] for x in saved} == {5, 15, 60}
    assert all(x["model_version"] == MODEL for x in saved)
    assert all(x["evaluated"] >= MIN_EVALUATED for x in saved)
    assert all(x["brier"] is not None and 0 <= x["brier"] <= 1 for x in saved)
    assert all(x["baseline_brier"] is not None for x in saved)
    assert all(x["matured_updates"] >= x["evaluated"] for x in saved)
    # Reboot/replay does not create duplicate snapshots or model promotions.
    restarted = ShadowLearningMonitor(monitor.store)
    assert restarted.run_once(now=now) == 3
    assert len(restarted.latest(asset="BTC")) == 3
    assert restarted.latest(asset="ETH") == []


def test_historical_backfill_is_not_fake_live_training(monitor):
    now = seed(monitor, contract="ETH-SHADOW-TEST", backfill=True)
    assert monitor.run_once(now=now) == 3
    snapshots = monitor.latest(asset="ETH")
    assert len(snapshots) == 3
    assert all(x["causal_examples"] == 0 for x in snapshots)
    assert all(x["evaluated"] == 0 for x in snapshots)
    assert all(x["brier"] is None for x in snapshots)
    assert all(x["candidate_beats_baseline"] == 0 for x in snapshots)


def test_no_sources_and_invalid_clock_are_safe(monitor):
    assert monitor.run_once(now=START) == 0
    assert monitor.latest(asset="BTC") == []
    with pytest.raises(ValueError, match="timezone-aware"):
        monitor.run_once(now=datetime(2026, 10, 28))
    with pytest.raises(ValueError, match="unsupported"):
        monitor.latest(asset="DOGE")
