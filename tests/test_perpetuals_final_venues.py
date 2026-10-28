"""Public Lighter/Phemex no-key perpetual candle schema and store tests."""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from parlay_bot.perpetuals.final_venues import LighterMarketClient, PhemexMarketClient
from parlay_bot.perpetuals.hyperliquid import MarketDataError
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


def cutoff() -> int:
    return int(datetime.now(UTC).timestamp() // 300 * 300)


def lighter_reply(req: httpx.Request) -> httpx.Response:
    assert req.url.host == "mainnet.zklighter.elliot.ai"
    assert "authorization" not in req.headers
    if req.url.path.endswith("/orderBooks"):
        return httpx.Response(200, json={"code": 200, "order_books": [
            {"symbol": "BTC", "market_id": 1, "market_type": "perp",
             "status": "active"},
            {"symbol": "ETH", "market_id": 0, "market_type": "perp",
             "status": "active"},
            {"symbol": "BTC", "market_id": 2048, "market_type": "spot",
             "status": "active"},
        ]})
    assert req.url.params["market_id"] == "1"
    assert req.url.params["resolution"] == "5m"
    assert req.url.params["count_back"] == "2"
    c = cutoff() * 1000
    return httpx.Response(200, json={"code": 200, "r": "5m", "c": [
        {"t": c - 600_000, "o": 100, "h": 105, "l": 95, "c": 101,
         "v": 2.5, "V": 252.5, "i": 99},
        {"t": c - 300_000, "o": 101, "h": 110, "l": 100, "c": 102,
         "v": 3.5, "V": 357, "i": 100},
    ]})


def phemex_reply(req: httpx.Request) -> httpx.Response:
    assert req.url.host == "api.phemex.com"
    assert "authorization" not in req.headers
    if req.url.path.endswith("/products"):
        return httpx.Response(200, json={"code": 0, "data": {
            "perpProductsV2": [
                {"symbol": "BTCUSDT", "type": "PerpetualV2",
                 "status": "Listed", "baseCurrency": "BTC",
                 "quoteCurrency": "USDT", "settleCurrency": "USDT"},
                {"symbol": "BTCUSD", "type": "Perpetual",
                 "status": "Listed", "baseCurrency": "BTC",
                 "quoteCurrency": "USD", "settleCurrency": "BTC"},
            ],
        }})
    assert req.url.params["resolution"] == "300"
    assert req.url.params["limit"] == "50"
    c = cutoff()
    return httpx.Response(200, json={"code": 0, "msg": "OK", "data": {
        "total": -1, "rows": [
            [c - 600, 300, "99", "100", "105", "95", "101", "2.5", "252"],
            [c - 300, 300, "101", "101", "110", "100", "102", "3.5", "357"],
        ],
    }})


@pytest.mark.asyncio
@pytest.mark.parametrize("venue,provider,reply,symbol,quote", [
    ("lighter", LighterMarketClient, lighter_reply, "BTC", "USD"),
    ("phemex", PhemexMarketClient, phemex_reply, "BTCUSDT", "USDT"),
])
async def test_final_two_venue_contracts_persist_without_fabrication(
    tmp_path, venue, provider, reply, symbol, quote,
):
    raw = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    store = SQLiteHotStore(tmp_path / "test.sqlite")
    try:
        batch = await provider(raw).collect(assets=("BTC",), bars=2)
        assert batch.contexts == ()
        assert len(batch.candles) == 2
        assert all(
            c.venue == venue and c.contract == symbol and c.quote == quote
            and c.trades == -1 and c.closed_at < c.observed_at
            for c in batch.candles
        )
        assert batch.candles[0].volume_base == pytest.approx(2.5)
        market = PerpetualMarketStore(store)
        assert market.ingest(batch).inserted_candles == 2
        assert market.ingest(batch).unchanged_candles == 2
        rows = market.recent_candles(
            venue=venue, contract=symbol, quote=quote, interval_minutes=5,
        )
        assert len(rows) == 2
        assert rows[-1]["close_price"] == 102
    finally:
        await raw.aclose()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [LighterMarketClient, PhemexMarketClient])
async def test_public_blocked_sources_fail_safely(provider):
    for status in (403, 429, 451):
        raw = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _, code=status: httpx.Response(code)
        ))
        try:
            with pytest.raises(MarketDataError, match="request failed"):
                await provider(raw).collect(assets=("BTC",), bars=2)
        finally:
            await raw.aclose()


@pytest.mark.asyncio
async def test_lighter_refuses_spot_market_or_inactive_perpetual():
    def spot_only(req):
        if req.url.path.endswith("/orderBooks"):
            return httpx.Response(200, json={"code": 200, "order_books": [
                {"symbol": "BTC", "market_id": 2048, "market_type": "spot",
                 "status": "active"},
            ]})
        return lighter_reply(req)
    raw = httpx.AsyncClient(transport=httpx.MockTransport(spot_only))
    try:
        with pytest.raises(MarketDataError, match="active perp missing"):
            await LighterMarketClient(raw).collect(assets=("BTC",), bars=2)
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_phemex_delisted_perp_must_not_be_collected():
    def delisted(req):
        if req.url.path.endswith("/products"):
            return httpx.Response(200, json={"code": 0, "data": {
                "perpProductsV2": [{
                    "symbol": "BTCUSDT", "type": "PerpetualV2",
                    "status": "Delisted", "baseCurrency": "BTC",
                    "quoteCurrency": "USDT", "settleCurrency": "USDT",
                }],
            }})
        return phemex_reply(req)
    raw = httpx.AsyncClient(transport=httpx.MockTransport(delisted))
    try:
        with pytest.raises(MarketDataError, match="unavailable"):
            await PhemexMarketClient(raw).collect(assets=("BTC",), bars=2)
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_no_fake_lighter_candle_from_mismatched_time_unit():
    def bad_clock(req):
        if req.url.path.endswith("/candles"):
            c = cutoff()
            return httpx.Response(200, json={"code": 200, "r": "5m", "c": [
                {"t": c - 300, "o": 100, "h": 110,
                 "l": 90, "c": 101, "v": 3.5},
            ]})
        return lighter_reply(req)
    raw = httpx.AsyncClient(transport=httpx.MockTransport(bad_clock))
    try:
        with pytest.raises(MarketDataError, match="timestamp"):
            await LighterMarketClient(raw).collect(assets=("BTC",), bars=2)
    finally:
        await raw.aclose()
