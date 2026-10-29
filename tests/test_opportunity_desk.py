"""Owner-facing opportunities never mistake offline research for live money."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.opportunities import (
    crypto_desk_embed,
    recent_watches,
    sportsbook_desk_embed,
)
from parlay_bot.jurisdiction.registry import JurisdictionRegistry
from parlay_bot.parlays.builder import ParlayBuilder
from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


def seeded_market(tmp_path, now):
    store = SQLiteHotStore(tmp_path / "live.sqlite")
    market = PerpetualMarketStore(store)
    market.initialize()
    opened = now - timedelta(minutes=6)
    observed = now - timedelta(minutes=1)
    candle = MarketCandle(
        "gate", "BTC_USDT", "USDT", 5, opened,
        opened + timedelta(minutes=5) - timedelta(milliseconds=1),
        observed, 100000, 100500, 99700, 100200, 3.5, 500,
    )
    market.ingest(MarketBatch((), (candle,), 5, observed))
    market.record_feed_health(
        venue="gate", asset="BTC", status="ok", attempted_at=observed,
        new_candles=1, total_candles=1,
        last_candle_close=candle.closed_at,
    )
    monitor = ForwardAccuracyMonitor(market)
    monitor.initialize()
    with store.connect() as con:
        con.execute(
            """INSERT INTO perp_forward_audit(
                 venue,contract,quote,asset,horizon_minutes,model_version,
                 reference_opened_at,reference_close,issued_at,target_opened_at,
                 probability_up,baseline_up,training_evaluated
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "gate", "BTC_USDT", "USDT", "BTC", 5, MODEL,
                opened.isoformat(), 100200,
                observed.isoformat(), (now + timedelta(minutes=3)).isoformat(),
                0.61, 0.5, 100,
            ),
        )
    return store, market


def test_live_watch_displays_real_price_and_pending_forecast(tmp_path):
    now = datetime(2026, 10, 29, 15, 30, tzinfo=UTC)
    store, market = seeded_market(tmp_path, now)
    try:
        items = recent_watches(market, asset="BTC", now=now)
        assert len(items) == 1
        assert items[0]["price"] == pytest.approx(100200)
        assert items[0]["direction"] == "UP"
        displayed = crypto_desk_embed(market, asset="BTC", now=now).to_dict()
        assert "PASS" in displayed["description"]
        assert "WATCH" in displayed["fields"][0]["value"]
        assert "$100,200.00" in displayed["fields"][0]["value"]
        assert "not profit-calibrated" in displayed["fields"][0]["value"]
        assert "No automatic orders" in displayed["footer"]["text"]
    finally:
        store.close()


def test_stale_candle_or_expired_window_never_becomes_live_opportunity(tmp_path):
    now = datetime(2026, 10, 29, 15, 30, tzinfo=UTC)
    store, market = seeded_market(tmp_path, now)
    try:
        assert recent_watches(market, asset="BTC", now=now + timedelta(hours=1)) == []
        embed = crypto_desk_embed(
            market, asset="BTC", now=now + timedelta(hours=1),
        )
        assert "No qualified live watch" in embed.fields[0].name
    finally:
        store.close()


def test_disconnected_price_feed_is_explicit_pass():
    embed = crypto_desk_embed(None, asset="BTC")
    assert "PASS" in embed.description
    assert "Data unavailable" in embed.fields[0].name


def test_no_sports_prices_produces_pass_not_a_fake_pick(tmp_path):
    store = SQLiteHotStore(tmp_path / "sports.sqlite")
    try:
        builder = ParlayBuilder(store=store, registry=JurisdictionRegistry([]))
        embed = sportsbook_desk_embed(builder, user_id="123")
        assert "PASS" in embed.description
        assert "Set your betting state" in embed.description
    finally:
        store.close()
