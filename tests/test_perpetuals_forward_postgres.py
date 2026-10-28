"""Prospective audit PostgreSQL 16 schema, settlement and restart persistence."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor, MODEL
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import PostgresHotStore


def test_prospective_prediction_settles_once_in_postgresql():
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("PostgreSQL integration database is unavailable")
    store = PostgresHotStore(dsn)
    monitor = ForwardAccuracyMonitor(PerpetualMarketStore(store))
    monitor.initialize()
    venue, contract, quote = "pg-forward-test", "BTC-FORWARD-CI", "USDC"
    now = datetime.now(UTC)
    opened = (now - timedelta(minutes=30)).replace(microsecond=0)
    target = opened + timedelta(minutes=5)
    issued = opened + timedelta(minutes=2)
    closed = target + timedelta(minutes=5) - timedelta(milliseconds=1)
    observed = target + timedelta(minutes=5, seconds=2)
    try:
        with store.connect() as conn:
            row = conn.execute(
                """INSERT INTO perp_forward_audit
                   (venue,contract,quote,asset,horizon_minutes,model_version,
                    reference_opened_at,reference_close,issued_at,
                    target_opened_at,probability_up,baseline_up,
                    training_evaluated)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT DO NOTHING""",
                (venue, contract, quote, "BTC", 5, MODEL,
                 opened.isoformat(), 100.0, issued.isoformat(),
                 target.isoformat(), 0.6, 0.5, 70),
            )
            assert row.rowcount == 1
            conn.execute(
                """INSERT INTO perp_market_candles
                   (venue,contract,quote,interval_minutes,opened_at,
                    closed_at,first_observed_at,open_price,high_price,low_price,
                    close_price,volume_base,trades)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (venue, contract, quote, 5, target.isoformat(),
                 closed.isoformat(), observed.isoformat(),
                 100.0, 102.0, 99.0, 101.0, 10.0, 12),
            )
        assert monitor._settle(now) == 1
        restarted = ForwardAccuracyMonitor(PerpetualMarketStore(store))
        assert restarted._settle(now) == 0
        score = restarted.summary(asset="BTC", now=now)["last_7d"][5]
        assert score["settled"] >= 1
        with store.connect() as conn:
            item = conn.execute(
                """SELECT outcome_up,model_brier,baseline_brier
                   FROM perp_forward_audit
                   WHERE venue=? AND contract=? AND model_version=?""",
                (venue, contract, MODEL),
            ).fetchone()
            assert item is not None
            assert item["outcome_up"] == 1
            assert item["model_brier"] == pytest.approx(0.16)
            assert item["baseline_brier"] == pytest.approx(0.25)
    finally:
        with store.connect() as conn:
            conn.execute(
                "DELETE FROM perp_forward_audit WHERE venue=? AND contract=?",
                (venue, contract),
            )
            conn.execute(
                "DELETE FROM perp_market_candles WHERE venue=? AND contract=?",
                (venue, contract),
            )
        store.close()
