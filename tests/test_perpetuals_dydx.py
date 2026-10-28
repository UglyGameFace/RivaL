"""dYdX v4 read-only candle adapter, provenance, failure and storage tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.perpetuals.dydx import DydxMarketClient
from parlay_bot.perpetuals.hyperliquid import MarketDataError
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 10, 28, 1, 13, tzinfo=UTC)


def item(ticker: str, minute: int, *, value: str = "101") -> dict:
    return {
        "ticker": ticker, "resolution": "5MINS",
        "startedAt": (NOW.replace(minute=minute, second=0)).isoformat(),
        "open": "100", "high": "110", "low": "90", "close": value,
        "baseTokenVolume": "8.75", "trades": "42",
    }


def responder(request: httpx.Request) -> httpx.Response:
    assert request.method == "GET"
    assert request.url.host == "indexer.dydx.trade"
    assert "authorization" not in request.headers
    if request.url.path == "/v4/perpetualMarkets":
        return httpx.Response(200, json={"markets": {
            f"{asset}-USD": {"ticker": f"{asset}-USD", "status": "ACTIVE"}
            for asset in ("BTC", "ETH", "SOL")
        }})
    assert request.url.path.startswith("/v4/candles/perpetualMarkets/")
    ticker = request.url.path.split("/")[-1]
    assert request.url.params["resolution"] == "5MINS"
    assert request.url.params["limit"] == "3"
    # dYdX returns newest first; adapter reorders and removes unfinished.
    return httpx.Response(200, json={"candles": [
        item(ticker, 10), item(ticker, 5), item(ticker, 0),
    ]})


@pytest.fixture
async def client():
    raw = httpx.AsyncClient(transport=httpx.MockTransport(responder))
    try:
        yield DydxMarketClient(raw)
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_dydx_produces_independent_usd_candles_without_inventing_contexts(client):
    batch = await client.collect(assets=("BTC", "ETH", "SOL"), bars=2, as_of=NOW)
    assert batch.contexts == ()
    assert batch.interval_minutes == 5
    assert len(batch.candles) == 6
    assert {c.contract for c in batch.candles} == {
        "BTC-USD", "ETH-USD", "SOL-USD",
    }
    assert all(c.venue == "dydx" and c.quote == "USD" for c in batch.candles)
    assert all(c.opened_at.minute in (0, 5) and c.trades == 42 for c in batch.candles)
    assert all(c.closed_at < NOW for c in batch.candles)


@pytest.mark.asyncio
async def test_dydx_candle_only_data_persists_and_replays(tmp_path, client):
    batch = await client.collect(assets=("BTC",), bars=2, as_of=NOW)
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    market = PerpetualMarketStore(store)
    first = market.ingest(batch)
    second = market.ingest(batch)
    assert first.inserted_candles == 2
    assert first.context_count == 0
    assert second.inserted_candles == 0
    assert second.unchanged_candles == 2
    assert market.recent_candles(
        venue="dydx", contract="BTC-USD", quote="USD", interval_minutes=5
    )[0]["first_observed_at"] == NOW.isoformat()
    with store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM perp_market_contexts"
        ).fetchone()["n"] == 0
    store.close()


@pytest.mark.asyncio
async def test_dydx_refuses_missing_market_and_invalid_headers(client):
    for assets in ((), ("BTC", "BTC"), ("BTC/USD",)):
        with pytest.raises(ValueError):
            await client.collect(assets=assets, bars=2, as_of=NOW)
    with pytest.raises(ValueError):
        await client.collect(assets=("BTC",), bars=200, as_of=NOW)
    with pytest.raises(ValueError):
        await client.collect(assets=("BTC",), interval_minutes=1, as_of=NOW)
    with pytest.raises(ValueError):
        await client.collect(assets=("BTC",), as_of=datetime(2026, 10, 28))

    raw = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"markets": {}})
    ))
    try:
        with pytest.raises(MarketDataError, match="unavailable"):
            await DydxMarketClient(raw).collect(assets=("BTC",), bars=2, as_of=NOW)
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_dydx_refuses_geoblock_rate_limit_and_malformed_json():
    for response in (
        httpx.Response(403), httpx.Response(429),
        httpx.Response(200, content=b"not-json"),
    ):
        raw = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _, result=response: result
        ))
        try:
            with pytest.raises(MarketDataError, match="request failed"):
                await DydxMarketClient(raw).collect(assets=("BTC",), bars=2, as_of=NOW)
        finally:
            await raw.aclose()


@pytest.mark.asyncio
async def test_dydx_rejects_wrong_ticker_duplicates_and_bad_prices():
    async def collect_with(replacement: list[dict]):
        def reply(request):
            if request.url.path == "/v4/perpetualMarkets":
                return responder(request)
            return httpx.Response(200, json={"candles": replacement})

        raw = httpx.AsyncClient(transport=httpx.MockTransport(reply))
        try:
            return await DydxMarketClient(raw).collect(
                assets=("BTC",), bars=2, as_of=NOW
            )
        finally:
            await raw.aclose()

    valid = item("BTC-USD", 5)
    for invalid, phrase in (
        ([item("ETH-USD", 5)], "mismatched"),
        ([valid, valid], "duplicate"),
        ([item("BTC-USD", 5, value="999")], "OHLC"),
        ([{**valid, "baseTokenVolume": "-1"}], "negative"),
        ([{**valid, "trades": "-4"}], "trade count"),
        ([{**valid, "startedAt": "yesterday"}], "timestamp"),
    ):
        with pytest.raises(MarketDataError, match=phrase):
            await collect_with(invalid)


@pytest.mark.asyncio
async def test_dydx_no_stale_or_future_candles_as_training_inputs():
    old = {
        **item("BTC-USD", 5),
        "startedAt": (NOW.replace(minute=5) - timedelta(hours=2)).isoformat(),
    }
    future = {
        **item("BTC-USD", 5),
        "startedAt": NOW.replace(minute=20).isoformat(),
    }

    def reply(request):
        if request.url.path == "/v4/perpetualMarkets":
            return responder(request)
        return httpx.Response(200, json={"candles": [old, future]})

    raw = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    try:
        with pytest.raises(MarketDataError, match="future-dated"):
            await DydxMarketClient(raw).collect(
                assets=("BTC",), bars=2, as_of=NOW
            )
    finally:
        await raw.aclose()
