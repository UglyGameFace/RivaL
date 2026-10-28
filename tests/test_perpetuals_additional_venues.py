"""Deribit/Binance read-only futures metadata, OHLC and storage regressions."""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from parlay_bot.perpetuals.additional_venues import (
    BinanceFuturesMarketClient,
    DeribitMarketClient,
)
from parlay_bot.perpetuals.hyperliquid import MarketDataError
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


def cutoff() -> int:
    return int(datetime.now(UTC).timestamp() // 300 * 300) * 1000


def deribit_reply(req: httpx.Request) -> httpx.Response:
    assert req.url.host == "www.deribit.com"
    assert "authorization" not in req.headers
    symbol = req.url.params["instrument_name"]
    if req.url.path.endswith("get_instrument"):
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": {
            "instrument_name": symbol, "kind": "future",
            "settlement_period": "perpetual", "instrument_type": "linear",
            "counter_currency": "USDC", "settlement_currency": "USDC",
            "is_active": True, "state": "open",
        }})
    assert req.url.params["resolution"] == "5"
    end = int(req.url.params["end_timestamp"])
    assert end + 1 == cutoff()
    c = cutoff()
    return httpx.Response(200, json={"jsonrpc": "2.0", "result": {
        "status": "ok", "ticks": [c - 600_000, c - 300_000],
        "open": [100, 100], "high": [110, 110], "low": [90, 90],
        "close": [101, 102], "volume": [1.25, 2.25],
    }})


def binance_reply(req: httpx.Request) -> httpx.Response:
    assert req.url.host == "fapi.binance.com"
    assert "authorization" not in req.headers
    if req.url.path.endswith("exchangeInfo"):
        return httpx.Response(200, json={"symbols": [{
            "symbol": "BTCUSDT", "contractType": "PERPETUAL",
            "status": "TRADING", "baseAsset": "BTC",
            "quoteAsset": "USDT", "marginAsset": "USDT",
        }]})
    assert req.url.params["symbol"] == "BTCUSDT"
    assert req.url.params["interval"] == "5m"
    c = cutoff()
    return httpx.Response(200, json=[
        [c - 600_000, "100", "110", "90", "101", "2.5",
         c - 300_001, "252", 51],
        [c - 300_000, "101", "112", "90", "102", "3.5",
         c - 1, "354", 72],
    ])


@pytest.mark.asyncio
@pytest.mark.parametrize("venue,client,reply,contract,quote", [
    ("deribit", DeribitMarketClient, deribit_reply,
     "BTC_USDC-PERPETUAL", "USDC"),
    ("binance", BinanceFuturesMarketClient, binance_reply,
     "BTCUSDT", "USDT"),
])
async def test_perpetual_candles_persist_with_true_venue_and_volume(
    tmp_path, venue, client, reply, contract, quote,
):
    raw = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    store = SQLiteHotStore(tmp_path / "data.sqlite")
    try:
        batch = await client(raw).collect(assets=("BTC",), bars=2)
        assert batch.contexts == ()
        assert len(batch.candles) == 2
        assert all(
            c.venue == venue and c.contract == contract and c.quote == quote
            and c.closed_at < c.observed_at
            for c in batch.candles
        )
        assert batch.candles[0].volume_base == pytest.approx(1.25 if venue == "deribit" else 2.5)
        assert batch.candles[0].trades == (-1 if venue == "deribit" else 51)
        market = PerpetualMarketStore(store)
        assert market.ingest(batch).inserted_candles == 2
        assert market.ingest(batch).unchanged_candles == 2
        rows = market.recent_candles(
            venue=venue, contract=contract, quote=quote, interval_minutes=5,
        )
        assert len(rows) == 2
        assert rows[-1]["close_price"] == 102
    finally:
        await raw.aclose()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [DeribitMarketClient, BinanceFuturesMarketClient])
async def test_blocked_public_api_fails_closed(provider):
    for code in (401, 403, 429, 451):
        raw = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _, status=code: httpx.Response(status)
        ))
        try:
            with pytest.raises(MarketDataError, match="request failed"):
                await provider(raw).collect(assets=("BTC",), bars=2)
        finally:
            await raw.aclose()


@pytest.mark.asyncio
async def test_wrong_instrument_type_or_deliverable_futures_rejected():
    def bad_deribit(req):
        if req.url.path.endswith("get_instrument"):
            return httpx.Response(200, json={"result": {
                "instrument_name": "BTC_USDC-PERPETUAL",
                "kind": "future", "settlement_period": "month",
                "instrument_type": "linear", "counter_currency": "USDC",
                "settlement_currency": "USDC", "is_active": True,
            }})
        return deribit_reply(req)

    def bad_binance(req):
        if req.url.path.endswith("exchangeInfo"):
            return httpx.Response(200, json={"symbols": [{
                "symbol": "BTCUSDT", "contractType": "CURRENT_QUARTER",
                "status": "TRADING", "baseAsset": "BTC",
                "quoteAsset": "USDT", "marginAsset": "USDT",
            }]})
        return binance_reply(req)

    for provider, reply in (
        (DeribitMarketClient, bad_deribit),
        (BinanceFuturesMarketClient, bad_binance),
    ):
        raw = httpx.AsyncClient(transport=httpx.MockTransport(reply))
        try:
            with pytest.raises(MarketDataError, match="unavailable"):
                await provider(raw).collect(assets=("BTC",), bars=2)
        finally:
            await raw.aclose()


@pytest.mark.asyncio
async def test_deribit_misaligned_arrays_cannot_fake_complete_candles():
    def truncated(req):
        result = deribit_reply(req)
        if req.url.path.endswith("get_tradingview_chart_data"):
            data = result.json()
            data["result"]["volume"] = [1.25]
            return httpx.Response(200, json=data)
        return result

    raw = httpx.AsyncClient(transport=httpx.MockTransport(truncated))
    try:
        with pytest.raises(MarketDataError, match="misaligned"):
            await DeribitMarketClient(raw).collect(assets=("BTC",), bars=2)
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_binance_incorrect_close_time_is_not_written():
    def broken(req):
        result = binance_reply(req)
        if req.url.path.endswith("/klines"):
            rows = result.json()
            rows[0][6] += 1_000
            return httpx.Response(200, json=rows)
        return result

    raw = httpx.AsyncClient(transport=httpx.MockTransport(broken))
    try:
        with pytest.raises(MarketDataError, match="closing timestamp"):
            await BinanceFuturesMarketClient(raw).collect(assets=("BTC",), bars=2)
    finally:
        await raw.aclose()
