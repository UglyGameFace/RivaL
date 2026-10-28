"""PostgreSQL 16 service integration for the new candle table."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.candles import PerpCandleStore
from parlay_bot.perpetuals.hyperliquid import normalize_candle
from parlay_bot.storage.hot import PostgresHotStore


def test_perp_candles_are_idempotent_and_point_in_time_on_postgres():
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("RIVAL_TEST_DATABASE_URL not configured")
    store = PostgresHotStore(dsn)
    now = datetime(2026, 10, 27, 23, 5, tzinfo=UTC)
    opened = datetime(2026, 10, 27, 23, 0, tzinfo=UTC)
    start = int(opened.timestamp() * 1000)
    payload = {
        "s": "BTC", "i": "5m", "t": start, "T": start + 300_000 - 1,
        "o": "100", "h": "104", "l": "99", "c": "102", "v": "3.25", "n": 8,
    }
    candle = normalize_candle(payload, coin="BTC", interval="5m", requested_at=now)
    assert candle is not None
    try:
        with store.connect() as conn:
            conn.execute("DROP TABLE IF EXISTS perp_candles")
        storage = PerpCandleStore(store)
        storage.initialize()
        assert storage.ingest((candle,), ingested_at=now) == 1
        assert storage.ingest((candle,), ingested_at=now) == 0
        args = {
            "venue": "hyperliquid", "asset": "BTC", "quote": "USDC",
            "contract_kind": "linear_perpetual", "interval": "5m",
        }
        assert storage.recent(**args, as_of=now - timedelta(seconds=1)) == []
        assert len(storage.recent(**args, as_of=now)) == 1
    finally:
        with store.connect() as conn:
            conn.execute("DROP TABLE IF EXISTS perp_candles")
        store.close()
