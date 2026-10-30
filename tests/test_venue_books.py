"""OKX + Bitget venue books: parsers, client bounds, and Get Signal wiring.

A forecast's book must still come from its OWN venue. These tests prove the
two new venues can supply one — and that a bad book fails closed to WAIT,
never into another venue's price.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from parlay_bot.discord_app.signals import signal_embed
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle
from parlay_bot.perpetuals.live_quotes import (
    PublicBestQuoteClient,
    QuoteUnavailable,
    VenueBookQuote,
    parse_bitget_ticker,
    parse_okx_ticker,
)
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


def _okx_payload(now: datetime, **overrides) -> dict:
    row = {
        "instId": "BTC-USDT-SWAP",
        "bidPx": "81000",
        "bidSz": "10",
        "askPx": "81001",
        "askSz": "10",
        "ts": str(int(now.timestamp() * 1000)),
    }
    row.update(overrides)
    return {"code": "0", "data": [row]}


def _bitget_payload(now: datetime, **overrides) -> dict:
    row = {
        "symbol": "BTCUSDT",
        "bidPx": "81000",
        "bidSz": "10",
        "askPx": "81001",
        "askSz": "10",
        "ts": str(int(now.timestamp() * 1000)),
    }
    row.update(overrides)
    return {"code": "00000", "data": [row]}


def test_okx_ticker_parses_and_validates() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    quote = parse_okx_ticker(_okx_payload(now), asset="BTC", received_at=now)
    assert quote.venue == "okx"
    assert quote.symbol == "BTC-USDT-SWAP"
    assert (quote.bid, quote.ask) == (81000.0, 81001.0)


def test_bitget_ticker_parses_and_validates() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    quote = parse_bitget_ticker(_bitget_payload(now), asset="BTC", received_at=now)
    assert quote.venue == "bitget"
    assert quote.symbol == "BTCUSDT"


@pytest.mark.parametrize("parser,payload_fn", [
    (parse_okx_ticker, _okx_payload),
    (parse_bitget_ticker, _bitget_payload),
])
def test_venue_ticker_rejects_bad_books(parser, payload_fn) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    wrong_symbol = (
        {"instId": "ETH-USDT-SWAP"} if parser is parse_okx_ticker
        else {"symbol": "ETHUSDT"}
    )
    with pytest.raises(QuoteUnavailable):
        parser(payload_fn(now, **wrong_symbol), asset="BTC", received_at=now)
    with pytest.raises(QuoteUnavailable):
        parser(payload_fn(now, bidPx="81001", askPx="81000"), asset="BTC", received_at=now)
    stale = now - timedelta(seconds=30)
    with pytest.raises(QuoteUnavailable):
        parser(
            payload_fn(now, ts=str(int(stale.timestamp() * 1000))),
            asset="BTC", received_at=now,
        )
    bad_code = {"code": "1"} if parser is parse_okx_ticker else {"code": "40001"}
    payload = payload_fn(now)
    payload.update(bad_code)
    with pytest.raises(QuoteUnavailable):
        parser(payload, asset="BTC", received_at=now)
    with pytest.raises(QuoteUnavailable):
        parser({"code": "0", "data": []}, asset="BTC", received_at=now)


def _transport(calls: list, status: int, payload: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(status, json=payload)
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_okx_client_caches_and_blocks() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    calls: list[str] = []
    client = httpx.AsyncClient(
        transport=_transport(calls, 200, _okx_payload(now)),
        base_url="https://www.okx.com",
    )
    quotes = PublicBestQuoteClient(venue_clients={"okx": client})
    try:
        first = await quotes.get_venue("BTC", "okx")
        second = await quotes.get_venue("BTC", "okx")
        assert first is second
        assert calls == ["/api/v5/market/ticker"]
    finally:
        await quotes.aclose()
        await client.aclose()

    calls.clear()
    blocked_client = httpx.AsyncClient(
        transport=_transport(calls, 429, {}),
        base_url="https://www.okx.com",
    )
    blocked = PublicBestQuoteClient(venue_clients={"okx": blocked_client})
    try:
        with pytest.raises(QuoteUnavailable):
            await blocked.get_venue("BTC", "okx")
        with pytest.raises(QuoteUnavailable):
            await blocked.get_venue("BTC", "okx")
        assert len(calls) == 1
    finally:
        await blocked.aclose()
        await blocked_client.aclose()


@pytest.mark.asyncio
async def test_bitget_client_fetches_futures_ticker() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    seen: list[httpx.QueryParams] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params)
        return httpx.Response(200, json=_bitget_payload(now))

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.bitget.com",
    )
    quotes = PublicBestQuoteClient(venue_clients={"bitget": client})
    try:
        quote = await quotes.get_venue("BTC", "bitget")
        assert quote.venue == "bitget"
        assert seen[0]["symbol"] == "BTCUSDT"
        assert seen[0]["productType"] == "USDT-FUTURES"
    finally:
        await quotes.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_unknown_venue_book_is_unavailable() -> None:
    quotes = PublicBestQuoteClient()
    try:
        with pytest.raises(QuoteUnavailable):
            await quotes.get_venue("BTC", "gate")
    finally:
        await quotes.aclose()


def _seed_okx_prediction(market, now, *, probability: float) -> None:
    market.initialize()
    opened = now - timedelta(minutes=6)
    closed = now - timedelta(minutes=1)
    candle = MarketCandle(
        "okx", "BTC-USDT-SWAP", "USDT", 5, opened, closed,
        closed + timedelta(milliseconds=1),
        80999, 81005, 80991, 81001, 20.0, 42,
    )
    market.ingest(MarketBatch(
        (), (candle,), 5, closed + timedelta(milliseconds=1),
    ))
    market.record_feed_health(
        venue="okx", asset="BTC", status="ok", attempted_at=now,
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
                "okx", "BTC-USDT-SWAP", "USDT", "BTC", 5,
                "prospective-logistic-shadow-v1",
                opened.isoformat(), 81001,
                (now - timedelta(seconds=30)).isoformat(),
                (now + timedelta(seconds=120)).isoformat(),
                probability, .5, 120,
            ),
        )


@pytest.mark.asyncio
async def test_okx_forecast_can_watch_with_its_own_book(tmp_path) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    store = SQLiteHotStore(tmp_path / "okx.sqlite")
    market = PerpetualMarketStore(store)
    book = VenueBookQuote(
        asset="BTC", venue="okx", symbol="BTC-USDT-SWAP",
        bid=81000, ask=81001, bid_quantity=10, ask_quantity=10,
        exchange_at=now, received_at=now,
    )
    quotes = type("Quotes", (), {
        "get": AsyncMock(side_effect=QuoteUnavailable("Binance unavailable")),
        "get_reference": AsyncMock(
            side_effect=QuoteUnavailable("Hyperliquid unavailable")
        ),
        "get_venue": AsyncMock(return_value=book),
    })()
    try:
        _seed_okx_prediction(market, now, probability=.61)
        from tests.test_rival_signal_decisions import seed_settled
        seed_settled(market, now, venue="okx")
        signal = await signal_embed(
            market, quotes, asset="BTC", horizon_minutes=5, now=now,
        )
        assert "UP WATCH" in signal.title
        assert "Source: **okx**" in next(x.value for x in signal.fields if x.name == "Model and expiry")
        assert "OKX" in next(x.value for x in signal.fields if x.name == "Fresh matching-market bid / ask 💱")
        assert "$81,000.00" in next(x.value for x in signal.fields if x.name == "Fresh matching-market bid / ask 💱")
        quotes.get_venue.assert_awaited_once_with("BTC", "okx")
    finally:
        store.close()
