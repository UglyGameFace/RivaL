"""PostgreSQL 16 smoke test for restart-safe dual-track forecast ledger."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.autonomous import AutonomousResearch
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import PostgresHotStore


def test_postgres_auto_ai_forecast_settles_exact_once_and_survives_restart():
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Postgres integration database unavailable")
    store = PostgresHotStore(dsn)
    market = PerpetualMarketStore(store)
    system = AutonomousResearch(market, ai=None, venues=())
    now = datetime.now(UTC)
    boundary = now.replace(
        minute=now.minute // 5 * 5, second=0, microsecond=0,
    )
    target = boundary + timedelta(minutes=10)
    issued = now
    digest = "d" * 64
    contract = "SOL-PROSPECTIVE-CI"
    model = "gemini-ci-forecast"
    try:
        system.initialize()
        assert system._insert_ai_forecast(
            asset="SOL", model=model, digest=digest, issued=issued,
            target_open=target, direction="down", count=3,
            reference={
                "venue": "ci-auto-ai", "contract": contract,
                "quote": "USDT", "close": 100,
            },
        )
        assert not system._insert_ai_forecast(
            asset="SOL", model=model, digest=digest, issued=issued,
            target_open=target, direction="down", count=3,
            reference={
                "venue": "ci-auto-ai", "contract": contract,
                "quote": "USDT", "close": 100,
            },
        )
        observed = target + timedelta(minutes=5, seconds=3)
        with store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_market_candles
                   (venue,contract,quote,interval_minutes,opened_at,closed_at,
                    first_observed_at,open_price,high_price,low_price,close_price,
                    volume_base,trades)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT DO NOTHING""",
                ("ci-auto-ai", contract, "USDT", 5,
                 target.isoformat(),
                 (target + timedelta(minutes=5, milliseconds=-1)).isoformat(),
                 observed.isoformat(), 100, 101, 96, 98, 5, 20),
            )
        restarted = AutonomousResearch(
            PerpetualMarketStore(store), ai=None, venues=(),
        )
        assert restarted.settle_ai(now=target + timedelta(minutes=6)) == 1
        assert restarted.settle_ai(now=target + timedelta(minutes=6)) == 0
        with store.connect() as conn:
            row = conn.execute(
                """SELECT direction,correct,source_count,settled_at
                   FROM perp_auto_ai_forecasts
                   WHERE asset='SOL' AND model=? AND evidence_sha256=?""",
                (model, digest),
            ).fetchone()
        assert row is not None and row["direction"] == "down"
        assert row["correct"] == 1
        assert row["source_count"] == 3
        assert row["settled_at"] is not None
    finally:
        with store.connect() as conn:
            conn.execute(
                "DELETE FROM perp_auto_ai_forecasts WHERE model=?", (model,),
            )
            conn.execute(
                "DELETE FROM perp_market_candles WHERE venue='ci-auto-ai'"
            )
        store.close()
