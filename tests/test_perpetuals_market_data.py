"""Strict exchange response and immutable market-history regression tests."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from parlay_bot.perpetuals.hyperliquid import HyperliquidMarketClient, MarketDataError
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 10, 28, 0, 13, tzinfo=UTC)
ASSETS = ("BTC", "ETH", "SOL")


def candle(asset: str, opened: int) -> dict:
    return {
        "t": opened, "T": opened + 300_000 - 1, "s": asset, "i": "5m",
        "o": "100", "h": "110", "l": "90", "c": "101",
        "v": "18.25", "n": 42,
    }


def responder(request: httpx.Request) -> httpx.Response:
    assert request.method == "POST"
    assert request.url.host == "api.hyperliquid.xyz"
    assert request.url.path == "/info"
    assert "authorization" not in request.headers
    body = json.loads(request.content)
    if body["type"] == "metaAndAssetCtxs":
        return httpx.Response(200, json=[
            {"universe": [{"name": a} for a in ASSETS]},
            [{"markPx": "100.25", "oraclePx": "100",
              "funding": "-0.00001", "openInterest": "14.5"} for _ in ASSETS],
        ])
    assert body["type"] == "candleSnapshot"
    req = body["req"]
    assert req["interval"] == "5m"
    assert req["endTime"] - req["startTime"] == 600_000
    return httpx.Response(200, json=[
        candle(req["coin"], req["startTime"]),
        candle(req["coin"], req["startTime"] + 300_000),
        candle(req["coin"], req["endTime"]),  # Not closed yet.
    ])


@pytest.fixture
async def client():
    raw = httpx.AsyncClient(transport=httpx.MockTransport(responder))
    try:
        yield HyperliquidMarketClient(raw)
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_fetch_three_venues_complete_candles_and_contexts(client):
    batch = await client.collect(assets=ASSETS, bars=2, as_of=NOW)
    assert {x.contract for x in batch.contexts} == set(ASSETS)
    assert len(batch.candles) == 6
    assert all(x.closed_at < NOW and x.quote == "USDC" for x in batch.candles)
    assert all(x.volume_base == 18.25 and x.trades == 42 for x in batch.candles)
    assert batch.contexts[0].funding_rate == pytest.approx(-0.00001)
    assert batch.contexts[0].open_interest_base == pytest.approx(14.5)


@pytest.mark.asyncio
async def test_refuses_geoblocks_rate_limits_and_server_errors():
    for status in (403, 429, 500):
        raw = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _, code=status: httpx.Response(code)
        ))
        try:
            with pytest.raises(MarketDataError, match="request failed"):
                await HyperliquidMarketClient(raw).collect(assets=("BTC",), bars=2, as_of=NOW)
        finally:
            await raw.aclose()


@pytest.mark.asyncio
async def test_rejects_wrong_market_and_future_time(client):
    with pytest.raises(ValueError, match="timezone"):
        await client.collect(as_of=datetime(2026, 10, 28))
    for assets in (("BTC", "BTC"), ("ETH/USDT",), ()):
        with pytest.raises(ValueError):
            await client.collect(assets=assets, bars=2, as_of=NOW)
    with pytest.raises(ValueError):
        await client.collect(bars=5000, as_of=NOW)
    with pytest.raises(ValueError):
        await client.collect(interval_minutes=1, as_of=NOW)

    def incorrect(request):
        req = json.loads(request.content)
        if req["type"] == "candleSnapshot":
            return httpx.Response(200, json=[candle("ETH", req["req"]["startTime"])])
        return responder(request)

    raw = httpx.AsyncClient(transport=httpx.MockTransport(incorrect))
    try:
        with pytest.raises(MarketDataError, match="wrong candle contract"):
            await HyperliquidMarketClient(raw).collect(assets=("BTC",), bars=2, as_of=NOW)
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_store_idempotent_replay_and_conflicting_revisions(tmp_path, client):
    batch = await client.collect(assets=ASSETS, bars=2, as_of=NOW)
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    market = PerpetualMarketStore(store)
    assert market.ingest(batch).inserted_candles == 6
    second = market.ingest(batch)
    assert second.inserted_candles == 0
    assert second.unchanged_candles == 6
    rows = market.recent_candles(
        venue="hyperliquid", contract="BTC", quote="USDC", interval_minutes=5
    )
    assert len(rows) == 2
    assert rows[0]["opened_at"] < rows[1]["opened_at"]
    with store.connect() as conn:
        conn.execute(
            "UPDATE perp_market_candles SET close_price=105 WHERE contract='BTC'"
        )
    # A conflicting bar is skipped and counted, never overwriting stored
    # values and never rejecting the rest of the batch.
    third = market.ingest(batch)
    assert third.inserted_candles == 0
    assert third.conflicted_candles == 2
    assert third.unchanged_candles == 4
    with store.connect() as conn:
        assert conn.execute(
            "SELECT close_price FROM perp_market_candles WHERE contract='BTC' LIMIT 1"
        ).fetchone()["close_price"] == 105
    store.close()


@pytest.mark.asyncio
async def test_new_tables_do_not_modify_sports_schema(tmp_path, client):
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    store.initialize()
    with store.connect() as conn:
        original = store.database.column_names(conn, "user_jurisdictions")
    batch = await client.collect(assets=("BTC",), bars=2, as_of=NOW)
    market = PerpetualMarketStore(store)
    market.ingest(batch)
    with store.connect() as conn:
        assert original == store.database.column_names(conn, "user_jurisdictions")
        assert conn.execute(
            "SELECT funding_rate FROM perp_market_contexts"
        ).fetchone()["funding_rate"] == pytest.approx(-0.00001)
    store.close()
