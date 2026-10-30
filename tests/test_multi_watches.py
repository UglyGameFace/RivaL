"""Promoted fused forecasts pass the same freshness gates as live ones.

A multi watch is priced through its reference venue's live book, so it
must clear the same feed-health and latest-candle checks as a live watch
before it can reach Get Signal.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.opportunities import recent_multi_watches
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.multi_exchange import MODEL as MULTI_MODEL
from parlay_bot.perpetuals.multi_exchange import MultiExchangeResearch
from parlay_bot.storage.hot import SQLiteHotStore


def seed_candle_and_feed(market, now, *, attempted_at=None, last_candle_close=None):
    market.initialize()
    opened = now - timedelta(minutes=6)
    closed = now - timedelta(minutes=1)
    candle = MarketCandle(
        "hyperliquid",
        "BTC",
        "USDC",
        5,
        opened,
        closed,
        closed + timedelta(milliseconds=1),
        80999,
        81005,
        80991,
        81001,
        20.0,
        42,
    )
    market.ingest(
        MarketBatch(
            (),
            (candle,),
            5,
            closed + timedelta(milliseconds=1),
        )
    )
    market.record_feed_health(
        venue="hyperliquid",
        asset="BTC",
        status="ok",
        attempted_at=attempted_at or now,
        new_candles=1,
        total_candles=1,
        last_candle_close=last_candle_close or closed,
    )


def seed_multi_watch(
    market, now, *, probability=0.68, venue="hyperliquid", contract="BTC", quote="USDC"
):
    MultiExchangeResearch(market.store).initialize()
    with market.store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_multi_forward (
               asset,horizon_minutes,target_opened_at,issued_at,
               model_version,probability_up,reference_probability_up,
               reference_venue,reference_contract,reference_quote,
               reference_opened_at,contributor_count,contributors_json
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "BTC",
                5,
                (now + timedelta(seconds=120)).isoformat(),
                (now - timedelta(seconds=30)).isoformat(),
                MULTI_MODEL,
                probability,
                0.60,
                venue,
                contract,
                quote,
                (now - timedelta(minutes=6)).isoformat(),
                4,
                "[]",
            ),
        )


def test_multi_watch_serves_with_engine_provenance(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "multi.sqlite")
    market = PerpetualMarketStore(store)
    try:
        seed_candle_and_feed(market, now)
        seed_multi_watch(market, now)
        watches = recent_multi_watches(market, asset="BTC", now=now)
        assert len(watches) == 1
        watch = watches[0]
        assert watch["venue"] == "hyperliquid"
        assert watch["probability_up"] == pytest.approx(0.68)
        assert watch["engine"] == "multi"
        assert watch["model_version"] == MULTI_MODEL
        assert watch["contributor_count"] == 4
    finally:
        store.close()


def test_multi_watch_blocked_by_stale_feed(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "stale.sqlite")
    market = PerpetualMarketStore(store)
    try:
        seed_candle_and_feed(
            market,
            now,
            attempted_at=now - timedelta(minutes=30),
            last_candle_close=now - timedelta(minutes=31),
        )
        seed_multi_watch(market, now)
        assert recent_multi_watches(market, asset="BTC", now=now) == []
    finally:
        store.close()


def test_multi_watch_blocked_by_contract_mismatch(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "mismatch.sqlite")
    market = PerpetualMarketStore(store)
    try:
        seed_candle_and_feed(market, now)
        seed_multi_watch(market, now, contract="ETH")
        assert recent_multi_watches(market, asset="BTC", now=now) == []
    finally:
        store.close()
