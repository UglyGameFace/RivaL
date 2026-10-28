"""Existing 10-provider candle store must not leak future/backfilled data."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore

_CONTRACT = "BTC-ASOF-REGRESSION"


@pytest.fixture(params=["sqlite", "postgres"])
def market(request, tmp_path):
    if request.param == "postgres":
        dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
        if not dsn:
            pytest.skip("RIVAL_TEST_DATABASE_URL not configured")
        hot = PostgresHotStore(dsn)
    else:
        hot = SQLiteHotStore(tmp_path / "point_in_time.sqlite")
    store = PerpetualMarketStore(hot)
    store.initialize()
    try:
        with hot.connect() as conn:
            conn.execute(
                "DELETE FROM perp_market_candles WHERE contract=?", (_CONTRACT,)
            )
        yield store
    finally:
        with hot.connect() as conn:
            conn.execute(
                "DELETE FROM perp_market_candles WHERE contract=?", (_CONTRACT,)
            )
        hot.close()


def _insert(market, *, venue: str, opened: datetime, first_seen: datetime):
    closed = opened + timedelta(minutes=5) - timedelta(milliseconds=1)
    with market.store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_market_candles
               (venue,contract,quote,interval_minutes,opened_at,
                closed_at,first_observed_at,open_price,high_price,
                low_price,close_price,volume_base,trades)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                venue, _CONTRACT, "USDC", 5, opened.isoformat(),
                closed.isoformat(), first_seen.isoformat(), 100., 103.,
                99., 102., 3., 6,
            ),
        )


def test_as_of_filters_first_observation_and_close_before_limit(market):
    start = datetime(2026, 10, 27, 23, tzinfo=UTC)
    first_seen = start + timedelta(minutes=5, seconds=10)
    second_start = start + timedelta(minutes=5)
    # Later backfill exposes a closed candle only after 23:15, not at 23:10.
    second_seen = start + timedelta(minutes=15)
    _insert(market, venue="hyperliquid", opened=start, first_seen=first_seen)
    _insert(market, venue="hyperliquid", opened=second_start, first_seen=second_seen)
    _insert(market, venue="dydx", opened=start, first_seen=first_seen)
    args = {
        "venue": "hyperliquid", "contract": _CONTRACT, "quote": "USDC",
        "interval_minutes": 5,
    }
    assert len(market.recent_candles(**args)) == 2
    assert market.recent_candles(**args, as_of=first_seen - timedelta(seconds=1)) == []
    as_of = second_start + timedelta(seconds=1)
    rows = market.recent_candles(**args, count=1, as_of=as_of)
    assert len(rows) == 1
    assert rows[0]["opened_at"] == start.isoformat()
    assert len(market.recent_candles(**args, as_of=second_seen)) == 2
    assert all(r["venue"] == "hyperliquid" for r in market.recent_candles(**args))


def test_as_of_rejects_naive_datetime(market):
    with pytest.raises(ValueError, match="timezone-aware"):
        market.recent_candles(
            venue="hyperliquid", contract=_CONTRACT, quote="USDC",
            interval_minutes=5, as_of=datetime(2026, 10, 27),
        )
