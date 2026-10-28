"""OKX/KuCoin futures identity, candle semantics and PostgreSQL-compatible writes."""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from parlay_bot.discord_app.data_dashboard import MarketDataDashboard
from parlay_bot.perpetuals.hyperliquid import MarketDataError
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.next_venues import KucoinMarketClient, OkxMarketClient
from parlay_bot.storage.hot import SQLiteHotStore


def cutoff() -> int:
    return int(datetime.now(UTC).timestamp() // 300 * 300)


def okx_response(request: httpx.Request) -> httpx.Response:
    assert request.url.host == "www.okx.com"
    assert "authorization" not in request.headers
    if request.url.path.endswith("/instruments"):
        contract = request.url.params["instId"]
        asset = contract.split("-")[0]
        return httpx.Response(200, json={"code": "0", "data": [{
            "instId": contract, "instType": "SWAP", "state": "live",
            "ctType": "linear", "ctValCcy": asset, "settleCcy": "USDT",
            "ctVal": "0.01", "ctMult": "1",
        }]})
    assert request.url.params["bar"] == "5m"
    assert request.url.params["limit"] == "3"
    c = cutoff() * 1000
    return httpx.Response(200, json={"code": "0", "data": [
        [str(c), "100", "110", "90", "105", "200", "2", "210", "0"],
        [str(c - 300_000), "100", "110", "90", "105", "200", "2", "210", "1"],
        [str(c - 600_000), "99", "110", "90", "100", "200", "2", "200", "1"],
    ]})


def kucoin_response(request: httpx.Request) -> httpx.Response:
    assert "authorization" not in request.headers
    if "/contracts/" in request.url.path:
        sym = request.url.path.split("/")[-1]
        asset = "XBT" if sym.startswith("XBT") else sym[:-5]
        return httpx.Response(200, json={"code": "200000", "data": {
            "symbol": sym, "baseCurrency": asset, "quoteCurrency": "USDT",
            "settleCurrency": "USDT", "status": "Open",
            "expireDate": None, "isInverse": False, "multiplier": "0.001",
        }})
    assert request.url.host == "api.kucoin.com"
    assert request.url.params["tradeType"] == "FUTURES"
    assert request.url.params["klineType"] == "TRADE"
    assert request.url.params["interval"] == "5min"
    sym = request.url.params["symbol"]
    c = cutoff()
    return httpx.Response(200, json={"code": "200000", "data": {
        "tradeType": "FUTURES", "symbol": sym, "list": [
            [c, "100", "110", "90", "105", "2000", "210000"],
            [c - 300, "100", "110", "90", "105", "2000", "210000"],
            [c - 600, "99", "110", "90", "100", "2000", "200000"],
        ],
    }})


@pytest.mark.asyncio
@pytest.mark.parametrize("name,provider,reply,contract", [
    ("okx", OkxMarketClient, okx_response, "BTC-USDT-SWAP"),
    ("kucoin", KucoinMarketClient, kucoin_response, "XBTUSDTM"),
])
async def test_two_public_venues_store_exact_contract_and_base_volume(
    tmp_path, name, provider, reply, contract,
):
    raw = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    try:
        batch = await provider(raw).collect(assets=("BTC",), bars=2)
        assert batch.contexts == ()
        assert len(batch.candles) == 2
        assert all(
            c.contract == contract and c.venue == name and c.quote == "USDT"
            and c.trades == -1 and c.volume_base == pytest.approx(2)
            and c.closed_at < c.observed_at
            for c in batch.candles
        )
        market = PerpetualMarketStore(store)
        assert market.ingest(batch).inserted_candles == 2
        assert market.ingest(batch).unchanged_candles == 2
        saved = market.recent_candles(
            venue=name, contract=contract, quote="USDT", interval_minutes=5,
        )
        assert len(saved) == 2
        assert saved[0]["volume_base"] == pytest.approx(2)
    finally:
        await raw.aclose()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,reply", [
    (OkxMarketClient, okx_response), (KucoinMarketClient, kucoin_response),
])
async def test_malformed_or_blocked_venue_fails_without_fabrication(provider, reply):
    for status in (403, 429, 451):
        raw = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _, code=status: httpx.Response(code)
            )
        )
        try:
            with pytest.raises(MarketDataError, match="request failed"):
                await provider(raw).collect(assets=("BTC",), bars=2)
        finally:
            await raw.aclose()
    with pytest.raises(ValueError):
        await provider().collect(assets=("BTC", "BTC"), bars=2)


@pytest.mark.asyncio
async def test_wrong_contract_metadata_never_passes():
    def wrong_okx(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/instruments"):
            return httpx.Response(200, json={"code": "0", "data": [{
                "instId": "BTC-USDT-SWAP", "instType": "SWAP", "state": "live",
                "ctType": "inverse", "ctValCcy": "BTC", "settleCcy": "USDT",
                "ctVal": "0.01", "ctMult": "1",
            }]})
        return okx_response(req)

    def wrong_kucoin(req: httpx.Request) -> httpx.Response:
        if "/contracts/" in req.url.path:
            return httpx.Response(200, json={"code": "200000", "data": {
                "symbol": "XBTUSDTM", "baseCurrency": "XBT",
                "quoteCurrency": "USDT", "settleCurrency": "USDT",
                "status": "Open", "expireDate": None, "isInverse": True,
                "multiplier": "0.001",
            }})
        return kucoin_response(req)

    for provider, mock in (
        (OkxMarketClient, wrong_okx), (KucoinMarketClient, wrong_kucoin),
    ):
        raw = httpx.AsyncClient(transport=httpx.MockTransport(mock))
        try:
            with pytest.raises(MarketDataError, match="contract|perpetual"):
                await provider(raw).collect(assets=("BTC",), bars=2)
        finally:
            await raw.aclose()


@pytest.mark.asyncio
async def test_kucoin_btc_market_screen_reads_actual_xbt_contract(tmp_path):
    raw = httpx.AsyncClient(transport=httpx.MockTransport(kucoin_response))
    store = SQLiteHotStore(tmp_path / "market.sqlite")
    try:
        market = PerpetualMarketStore(store)
        batch = await KucoinMarketClient(raw).collect(assets=("BTC",), bars=2)
        assert market.ingest(batch).inserted_candles == 2
        dashboard = MarketDataDashboard(
            owner_id=42, market=market, venues=("kucoin",), assets=("BTC",),
            interval_seconds=300, enabled=True,
        )
        dashboard.screen = "markets"
        result = await dashboard.render()
        assert "XBTUSDTM" in result.fields[0].name
        assert "105.0000 USDT" in result.fields[0].value
    finally:
        await raw.aclose()
        store.close()
