"""PostgreSQL 16 compatibility for isolated perpetual market history."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle, MarketContext
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import PostgresHotStore


def test_postgres_candles_are_idempotent_and_legacy_schema_survives():
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("PostgreSQL integration service not configured")
    store = PostgresHotStore(dsn)
    opened = datetime(2026, 10, 28, 0, 0, tzinfo=UTC)
    observed = opened + timedelta(minutes=6)
    try:
        store.initialize()
        market = PerpetualMarketStore(store)
        batch = MarketBatch(
            contexts=(MarketContext(
                "hyperliquid", "BTC", "USDC", observed, 100, 99.8, 0.00001, 12,
            ),),
            candles=(MarketCandle(
                "hyperliquid", "BTC", "USDC", 5, opened,
                opened + timedelta(minutes=5) - timedelta(milliseconds=1),
                observed, 100, 110, 90, 102, 14.5, 20,
            ),),
            interval_minutes=5, observed_at=observed,
        )
        assert market.ingest(batch).inserted_candles == 1
        assert market.ingest(batch).unchanged_candles == 1
        rows = market.recent_candles(
            venue="hyperliquid", contract="BTC", quote="USDC", interval_minutes=5
        )
        assert len(rows) == 1
        assert rows[0]["close_price"] == 102
        with store.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM perp_market_contexts"
            ).fetchone()["n"] == 1
            assert store.database.column_names(conn, "user_jurisdictions")
    finally:
        # Only the isolated test tables are removed, not application state.
        with store.connect() as conn:
            conn.executescript(
                """DROP TABLE IF EXISTS perp_market_candles;
                   DROP TABLE IF EXISTS perp_market_contexts;"""
            )
        store.close()


def test_postgres_candle_only_source_does_not_invent_market_context():
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("PostgreSQL integration service not configured")
    store = PostgresHotStore(dsn)
    opened = datetime(2026, 10, 28, 1, 0, tzinfo=UTC)
    observed = opened + timedelta(minutes=6)
    try:
        market = PerpetualMarketStore(store)
        batch = MarketBatch(
            contexts=(),
            candles=(MarketCandle(
                "dydx", "BTC-USD", "USD", 5, opened,
                opened + timedelta(minutes=5) - timedelta(milliseconds=1),
                observed, 101, 103, 100, 102, 4.5, 18,
            ),),
            interval_minutes=5,
            observed_at=observed,
        )
        assert market.ingest(batch).inserted_candles == 1
        assert market.ingest(batch).unchanged_candles == 1
        saved = market.recent_candles(
            venue="dydx", contract="BTC-USD", quote="USD", interval_minutes=5,
        )
        assert len(saved) == 1
        assert saved[0]["volume_base"] == 4.5
        with store.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM perp_market_contexts WHERE venue='dydx'"
            ).fetchone()["n"] == 0
    finally:
        with store.connect() as conn:
            conn.executescript(
                """DROP TABLE IF EXISTS perp_market_candles;
                   DROP TABLE IF EXISTS perp_market_contexts;"""
            )
        store.close()
