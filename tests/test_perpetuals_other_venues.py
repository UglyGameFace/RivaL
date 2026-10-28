"""Public Bitget/Gate contract identity, units, and safely bounded data tests."""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from parlay_bot.perpetuals.hyperliquid import MarketDataError
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.other_venues import BitgetMarketClient, GateMarketClient
from parlay_bot.storage.hot import SQLiteHotStore


def cutoff() -> int:
    now = datetime.now(UTC)
    return int(now.timestamp() // 300 * 300)


def response(request: httpx.Request) -> httpx.Response:
    assert "authorization" not in request.headers
    if "bitget" in request.url.host:
        if request.url.path.endswith("/instruments"):
            symbol = request.url.params["symbol"]
            return httpx.Response(200, json={
                "code": "00000", "data": [{
                    "symbol": symbol, "category": "USDT-FUTURES",
                    "baseCoin": symbol[:-4], "quoteCoin": "USDT",
                    "type": "perpetual", "status": "online",
                }],
            })
        assert request.url.params["category"] == "USDT-FUTURES"
        assert request.url.params["interval"] == "5m"
        end = int(request.url.params["endTime"]) + 1
        return httpx.Response(200, json={
            "code": "00000", "data": [
                [str(end - 300_000), "100", "110", "90", "101", "1.25", "126"],
                [str(end - 600_000), "99", "110", "90", "100", "2", "199"],
            ],
        })
    if "/contracts/" in request.url.path:
        return httpx.Response(200, json={
            "name": request.url.path.split("/")[-1], "type": "direct",
            "status": "trading", "in_delisting": False,
            "quanto_multiplier": "0.001",
        })
    assert request.url.path.endswith("/candlesticks")
    assert request.url.params["interval"] == "5m"
    now = cutoff()
    return httpx.Response(200, json=[
        {"t": now - 600, "o": "100", "h": "110", "l": "90",
         "c": "101", "v": "2000"},
        {"t": now - 300, "o": "101", "h": "112", "l": "99",
         "c": "102", "v": "1000"},
    ])


@pytest.mark.asyncio
@pytest.mark.parametrize("venue,expected", [
    ("bitget", 2.0), ("gate", 2.0),
])
async def test_public_futures_are_bounded_and_base_volume_is_not_contract_count(
    tmp_path, venue, expected,
):
    raw = httpx.AsyncClient(transport=httpx.MockTransport(response))
    provider = BitgetMarketClient(raw) if venue == "bitget" else GateMarketClient(raw)
    store = SQLiteHotStore(tmp_path / "market.sqlite")
    try:
        batch = await provider.collect(assets=("BTC",), bars=2)
        assert batch.contexts == ()
        assert len(batch.candles) == 2
        assert all(x.venue == venue and x.quote == "USDT" for x in batch.candles)
        assert all(x.trades == -1 for x in batch.candles)
        assert batch.candles[0].volume_base == pytest.approx(expected)
        assert all(x.closed_at < x.observed_at for x in batch.candles)
        market = PerpetualMarketStore(store)
        assert market.ingest(batch).inserted_candles == 2
        assert market.ingest(batch).unchanged_candles == 2
        saved = market.recent_candles(
            venue=venue, contract="BTCUSDT" if venue == "bitget" else "BTC_USDT",
            quote="USDT", interval_minutes=5,
        )
        assert len(saved) == 2
        assert saved[0]["trades"] == -1  # Unknown, not zero trades.
    finally:
        await raw.aclose()
        store.close()


@pytest.mark.asyncio
async def test_invalid_or_delisted_contract_rejected():
    def invalid(request: httpx.Request) -> httpx.Response:
        result = response(request)
        if request.url.path.endswith("/instruments"):
            return httpx.Response(200, json={"code": "00000", "data": [
                {"symbol": "BTCUSDT", "category": "USDT-FUTURES",
                 "type": "delivery", "status": "online", "baseCoin": "BTC",
                 "quoteCoin": "USDT"},
            ]})
        if "/contracts/" in request.url.path:
            return httpx.Response(200, json={
                "name": "BTC_USDT", "type": "inverse", "status": "trading",
                "quanto_multiplier": "0.001",
            })
        return result

    raw = httpx.AsyncClient(transport=httpx.MockTransport(invalid))
    try:
        for provider in (BitgetMarketClient(raw), GateMarketClient(raw)):
            with pytest.raises(MarketDataError, match="unavailable"):
                await provider.collect(assets=("BTC",), bars=2)
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_rate_limit_or_region_block_fails_closed():
    for status in (403, 429, 451):
        raw = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _, code=status: httpx.Response(code)
        ))
        try:
            for provider in (BitgetMarketClient(raw), GateMarketClient(raw)):
                with pytest.raises(MarketDataError, match="request failed"):
                    await provider.collect(assets=("BTC",), bars=2)
        finally:
            await raw.aclose()


@pytest.mark.asyncio
async def test_provider_rejects_invalid_history_and_does_not_invent_prices():
    def malformed(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/candles"):
            return httpx.Response(200, json={"code": "00000", "data": [
                [str((cutoff() - 300) * 1000), "100", "99", "90",
                 "101", "1", "101"],
            ]})
        if request.url.path.endswith("/candlesticks"):
            return httpx.Response(200, json=[
                {"t": cutoff() - 300, "o": "100", "h": "99",
                 "l": "90", "c": "101", "v": "1000"},
            ])
        return response(request)

    raw = httpx.AsyncClient(transport=httpx.MockTransport(malformed))
    try:
        for provider in (BitgetMarketClient(raw), GateMarketClient(raw)):
            with pytest.raises(MarketDataError, match="OHLC"):
                await provider.collect(assets=("BTC",), bars=2)
    finally:
        await raw.aclose()
