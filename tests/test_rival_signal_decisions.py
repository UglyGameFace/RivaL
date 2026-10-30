"""Real persisted forward forecasts drive private, source-matched RivaL signals."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from parlay_bot.discord_app.signals import model_strength, signal_embed
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle
from parlay_bot.perpetuals.live_quotes import (
    BookQuote,
    PerpReferenceBook,
    QuoteUnavailable,
)
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


def seed_prediction(market, now, *, venue, probability, expires_in=420):
    market.initialize()
    symbols = {
        "hyperliquid": ("BTC", "USDC"),
        "binance": ("BTCUSDT", "USDT"),
        "gate": ("BTC_USDT", "USDT"),
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
                venue, contract, quote, "BTC", 5, "prospective-logistic-shadow-v1",
                opened.isoformat(), 81001,
                (now - timedelta(seconds=30)).isoformat(),
                (now + timedelta(seconds=expires_in - 300)).isoformat(),
                probability, .5, 120,
            ),
        )


def seed_settled(market, now, *, venue, count=50, beating=True, correct=30,
                 model_brier=None):
    """Settled rows deciding one venue's 7-day verdict (50+ to qualify)."""
    ForwardAccuracyMonitor(market).initialize()
    with market.store.connect() as conn:
        for i in range(count):
            issued = now - timedelta(days=1) + timedelta(minutes=i)
            conn.execute(
                """INSERT INTO perp_forward_audit
                   (venue,contract,quote,asset,horizon_minutes,model_version,
                    reference_opened_at,reference_close,issued_at,target_opened_at,
                    probability_up,baseline_up,training_evaluated,
                    settled_at,outcome_up,model_brier,baseline_brier)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    venue, "BTC", "USDC", "BTC", 5,
                    "prospective-logistic-shadow-v2",
                    (issued - timedelta(minutes=5)).isoformat(), 81001,
                    issued.isoformat(),
                    (issued + timedelta(minutes=5)).isoformat(),
                    .7, .5, 120,
                    (issued + timedelta(minutes=10)).isoformat(),
                    1 if i < correct else 0,
                    (model_brier if model_brier is not None
                     else (.20 if beating else .30)), .25,
                ),
            )


def books(now):
    hyper = PerpReferenceBook(
        asset="BTC", venue="hyperliquid", bid=81000, ask=81001,
        bid_quantity=10, ask_quantity=10, exchange_at=now, received_at=now,
    )
    binance = BookQuote(
        asset="BTC", venue="binance_usdm", symbol="BTCUSDT",
        bid=81003, ask=81004, bid_quantity=9, ask_quantity=9,
        exchange_at=now, received_at=now,
    )
    return hyper, binance


def mock_quotes(now):
    hyper, binance = books(now)
    return type("Quotes", (), {
        "get_reference": AsyncMock(return_value=hyper),
        "get": AsyncMock(return_value=binance),
    })()


@pytest.mark.parametrize(("score", "expected"), [
    (.514, "very weak"), (.54, "weak"), (.59, "directional watch"),
    (.41, "directional watch"), (.5, "very weak"),
])
def test_model_strength_never_calls_uncalibrated_model_confidence(score, expected):
    assert model_strength(score) == expected


@pytest.mark.asyncio
async def test_strong_forward_watch_uses_matching_hyperliquid_quote(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "watch.sqlite")
    market = PerpetualMarketStore(store)
    quotes = mock_quotes(now)
    try:
        seed_prediction(market, now, venue="hyperliquid", probability=.61)
        seed_settled(market, now, venue="hyperliquid")
        signal = await signal_embed(market, quotes, asset="BTC", horizon_minutes=5, now=now)
        assert "UP WATCH" in signal.title
        assert "RESEARCH ONLY" in signal.title
        assert "Hyperliquid" in next(
            x.value for x in signal.fields if x.name.startswith("Fresh matching")
        )
        assert "$81,000.00" in str(signal.to_dict())
        assert "not a validated" in signal.description
        quotes.get.assert_not_awaited()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_weak_model_is_pass_not_actionable_even_with_current_quote(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "weak.sqlite")
    market = PerpetualMarketStore(store)
    try:
        seed_prediction(market, now, venue="hyperliquid", probability=.514)
        report = await signal_embed(market, mock_quotes(now), asset="BTC",
                                    horizon_minutes=5, now=now)
        assert "UP LEAN • NO SIGNAL" in report.title
        assert "coin flip" in report.description
        assert "51.4%" in next(x.value for x in report.fields if x.name == "⚡ Model strength")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_blocked_exchange_uses_only_another_forecasts_matching_book(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "fallback.sqlite")
    market = PerpetualMarketStore(store)
    quotes = mock_quotes(now)
    quotes.get_reference = AsyncMock(
        side_effect=QuoteUnavailable("Hyperliquid unavailable")
    )
    try:
        seed_prediction(market, now, venue="hyperliquid", probability=.62)
        seed_prediction(market, now, venue="binance", probability=.63)
        seed_settled(market, now, venue="binance")
        signal = await signal_embed(market, quotes, asset="BTC",
                                    horizon_minutes=5, now=now)
        assert "UP WATCH" in signal.title
        assert "Source: **binance**" in next(x.value for x in signal.fields if x.name == "Model and expiry")
        assert "$81,003.00" in next(x.value for x in signal.fields if x.name == "Fresh matching-market bid / ask 💱")
        assert "$81,000.00" not in next(x.value for x in signal.fields if x.name == "Fresh matching-market bid / ask 💱")
        quotes.get.assert_awaited_once_with("BTC")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_unmatched_price_or_near_expiry_is_not_a_fabricated_signal(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "blocked.sqlite")
    market = PerpetualMarketStore(store)
    quotes = mock_quotes(now)
    try:
        seed_prediction(market, now, venue="gate", probability=.72)
        unmatched = await signal_embed(market, quotes, asset="BTC",
                                       horizon_minutes=5, now=now)
        assert "MARKET PRICE UNAVAILABLE" in unmatched.title
        assert "not a valid real-time entry" in next(x.value for x in unmatched.fields if x.name == "Matching live price")
        quotes.get.assert_not_awaited()
        quotes.get_reference.assert_not_awaited()

        seed_prediction(market, now, venue="binance",
                        probability=.69, expires_in=30)
        near_close = await signal_embed(
            market, quotes, asset="BTC", horizon_minutes=5,
            now=now + timedelta(minutes=6),
        )
        assert "NO LIVE SIGNAL" in near_close.title
    finally:
        store.close()


@pytest.mark.asyncio
async def test_no_data_and_disconnected_market_are_clean_no_signal(tmp_path):
    now = datetime.now(UTC)
    quotes = mock_quotes(now)
    disconnected = await signal_embed(
        None, quotes, asset="ETH", horizon_minutes=15, now=now,
    )
    assert "NO LIVE SIGNAL" in disconnected.title
    quotes.get.assert_not_awaited()
    store = SQLiteHotStore(tmp_path / "empty.sqlite")
    try:
        result = await signal_embed(
            PerpetualMarketStore(store), quotes,
            asset="BTC", horizon_minutes=5, now=now,
        )
        assert "NO LIVE SIGNAL" in result.title
    finally:
        store.close()
    with pytest.raises(ValueError):
        await signal_embed(None, quotes, asset="DOGE", horizon_minutes=5)


@pytest.mark.asyncio
async def test_signal_venues_are_raced_not_tried_one_after_another(tmp_path, caplog):
    """A slow first venue must not consume the whole signal budget.

    Both venue fetches start before either finishes: under the old
    sequential loop the Binance fetch could not start until Hyperliquid
    returned, so a 5s Hyperliquid timeout plus a slow Binance fetch blew
    past the signal cap into SIGNAL DELAYED. Priority is unchanged: the
    Hyperliquid book still wins when it validates.
    """
    import asyncio as _asyncio
    import logging as _logging

    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "raced.sqlite")
    market = PerpetualMarketStore(store)
    hyper, binance = books(now)
    started: list[str] = []
    release = _asyncio.Event()

    async def _gate(name):
        started.append(name)
        if len(started) == 2:
            release.set()
        await _asyncio.wait_for(release.wait(), timeout=5)
        return hyper if name == "hyperliquid" else binance

    class RacedQuotes:
        async def get_reference(self, asset):
            return await _gate("hyperliquid")

        async def get(self, asset):
            return await _gate("binance")

    try:
        seed_prediction(market, now, venue="hyperliquid", probability=.61)
        seed_prediction(market, now, venue="binance", probability=.58)
        seed_settled(market, now, venue="hyperliquid")
        with caplog.at_level(_logging.INFO, logger="parlay_bot.discord_app.signals"):
            signal = await signal_embed(
                market, RacedQuotes(), asset="BTC", horizon_minutes=5, now=now,
            )
        assert started == ["hyperliquid", "binance"] or set(started) == {
            "hyperliquid", "binance"}
        assert "UP WATCH" in signal.title
        assert "Hyperliquid" in next(
            x.value for x in signal.fields if x.name.startswith("Fresh matching")
        )
        assert "RivaL signal asset=BTC horizon=5" in caplog.text
        assert "book_venue=hyperliquid" in caplog.text
    finally:
        store.close()
