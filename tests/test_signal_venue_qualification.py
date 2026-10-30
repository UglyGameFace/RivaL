"""Venue-qualified signal selection: priority never overrides qualification.

Covers the second-pass audit's signal findings: a stronger qualified
forecast must not hide behind a weaker priority venue; a venue's WATCH
gate uses its OWN 7-day record, never the global one; and a missing venue
record yields UNVERIFIED research, never a watch.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from parlay_bot.discord_app.signals import signal_embed
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle
from parlay_bot.perpetuals.live_quotes import PerpReferenceBook, VenueBookQuote
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore
from tests.test_rival_signal_decisions import seed_settled


def seed_watch(market, now, *, venue, probability):
    market.initialize()
    symbols = {
        "hyperliquid": ("BTC", "USDC"),
        "okx": ("BTC-USDT-SWAP", "USDT"),
        "binance": ("BTCUSDT", "USDT"),
    }
    contract, quote = symbols[venue]
    opened = now - timedelta(minutes=6)
    closed = now - timedelta(minutes=1)
    candle = MarketCandle(
        venue, contract, quote, 5, opened, closed,
        closed + timedelta(milliseconds=1),
        80999, 81005, 80991, 81001, 20.0, 42,
    )
    market.ingest(MarketBatch(
        (), (candle,), 5, closed + timedelta(milliseconds=1),
    ))
    market.record_feed_health(
        venue=venue, asset="BTC", status="ok", attempted_at=now,
        new_candles=1, total_candles=1, last_candle_close=closed,
    )
    ForwardAccuracyMonitor(market).initialize()
    with market.store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_forward_audit
               (venue,contract,quote,asset,horizon_minutes,model_version,
                reference_opened_at,reference_close,issued_at,target_opened_at,
                probability_up,baseline_up,training_evaluated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                venue, contract, quote, "BTC", 5, "prospective-logistic-shadow-v2",
                opened.isoformat(), 81001,
                (now - timedelta(seconds=30)).isoformat(),
                (now + timedelta(seconds=120)).isoformat(),
                probability, .5, 120,
            ),
        )


def mock_books(now, *, hyper=True, okx=True):
    hyper_book = PerpReferenceBook(
        asset="BTC", venue="hyperliquid", bid=81000, ask=81001,
        bid_quantity=10, ask_quantity=10, exchange_at=now, received_at=now,
    )
    okx_book = VenueBookQuote(
        asset="BTC", venue="okx", symbol="BTC-USDT-SWAP",
        bid=81000, ask=81001, bid_quantity=10, ask_quantity=10,
        exchange_at=now, received_at=now,
    )
    return type("Quotes", (), {
        "get_reference": AsyncMock(return_value=hyper_book) if hyper else AsyncMock(
            side_effect=Exception("no hyperliquid book")),
        "get_venue": AsyncMock(return_value=okx_book) if okx else AsyncMock(
            side_effect=Exception("no okx book")),
        "get": AsyncMock(side_effect=Exception("no binance book")),
    })()


@pytest.mark.asyncio
async def test_stronger_qualified_venue_wins_over_priority(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "select.sqlite")
    market = PerpetualMarketStore(store)
    try:
        seed_watch(market, now, venue="hyperliquid", probability=.51)
        seed_watch(market, now, venue="okx", probability=.68)
        seed_settled(market, now, venue="okx")
        signal = await signal_embed(
            market, mock_books(now), asset="BTC", horizon_minutes=5, now=now,
        )
        assert "UP WATCH" in signal.title
        assert "Source: **okx**" in signal.fields[0].value
        alternatives = next(
            x.value for x in signal.fields
            if x.name.startswith("Other venues")
        )
        assert "hyperliquid" in alternatives
    finally:
        store.close()


@pytest.mark.asyncio
async def test_global_beats_but_venue_trails_is_pass(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "pass.sqlite")
    market = PerpetualMarketStore(store)
    try:
        seed_watch(market, now, venue="hyperliquid", probability=.62)
        seed_settled(market, now, venue="hyperliquid", beating=False)
        seed_settled(market, now, venue="binance", model_brier=0.10)
        signal = await signal_embed(
            market, mock_books(now, okx=False), asset="BTC",
            horizon_minutes=5, now=now,
        )
        assert "PASS • MODEL UNDERPERFORMING" in signal.title
        assert "WATCH" not in signal.title
        track = next(
            x.value for x in signal.fields
            if x.name.startswith("Model track record")
        )
        assert "hyperliquid" in track and "Trails baseline" in track
        assert "All venues:" in track
    finally:
        store.close()


@pytest.mark.asyncio
async def test_unverified_when_no_venue_record(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "unknown.sqlite")
    market = PerpetualMarketStore(store)
    try:
        seed_watch(market, now, venue="hyperliquid", probability=.61)
        signal = await signal_embed(
            market, mock_books(now, okx=False), asset="BTC",
            horizon_minutes=5, now=now,
        )
        assert "UNVERIFIED" in signal.title
        assert "WATCH" not in signal.title
        assert "track record could not be verified" in signal.description
    finally:
        store.close()


@pytest.mark.asyncio
async def test_weak_forecast_stays_lean_even_when_venue_qualified(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "lean.sqlite")
    market = PerpetualMarketStore(store)
    try:
        seed_watch(market, now, venue="hyperliquid", probability=.54)
        seed_settled(market, now, venue="hyperliquid")
        signal = await signal_embed(
            market, mock_books(now, okx=False), asset="BTC",
            horizon_minutes=5, now=now,
        )
        assert "LEAN • NO SIGNAL" in signal.title
    finally:
        store.close()


def test_venue_summary_needs_fifty_settled(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "threshold.sqlite")
    market = PerpetualMarketStore(store)
    try:
        seed_settled(market, now, venue="hyperliquid", count=49)
        monitor = ForwardAccuracyMonitor(market)
        assert monitor.venue_summary(
            asset="BTC", venue="hyperliquid", horizon=5, now=now,
        )["model_beats_baseline"] is None
        seed_settled(
            market, now + timedelta(hours=1), venue="hyperliquid", count=1,
        )
        assert monitor.venue_summary(
            asset="BTC", venue="hyperliquid", horizon=5, now=now,
        )["model_beats_baseline"] is True
    finally:
        store.close()
