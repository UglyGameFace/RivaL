"""Offline exchange fixtures, timestamp integrity and relational-ingestion tests."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.perpetuals.candles import PerpCandleStore, collect_hyperliquid
from parlay_bot.perpetuals.hyperliquid import (
    HyperliquidPublicCandles,
    normalize_candle,
)
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 10, 27, 23, 5, tzinfo=UTC)


def row(*, minute: int = 0, coin: str = "BTC", interval: str = "5m"):
    start = int(datetime(2026, 10, 27, 23, minute, tzinfo=UTC).timestamp() * 1000)
    return {
        "s": coin, "i": interval, "t": start, "T": start + 5 * 60_000 - 1,
        "o": "100.0", "h": "105", "l": "98", "c": "103",
        "v": "8.25", "n": 42,
    }


def market_client(rows, *, status=200):
    requests = []

    def reply(req):
        requests.append(req)
        return httpx.Response(status, json=rows)

    client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    return client, requests


@pytest.mark.asyncio
async def test_public_endpoint_and_only_complete_candles():
    client, requests = market_client([row(minute=5), row(minute=0)])
    async with client:
        candles = await HyperliquidPublicCandles(client).fetch_closed(
            coin="BTC", count=2, requested_at=NOW
        )
    assert len(candles) == 1
    assert candles[0].close == 103
    assert candles[0].opened_at == datetime(2026, 10, 27, 23, tzinfo=UTC)
    assert requests[0].url == "https://api.hyperliquid.xyz/info"
    payload = json.loads(requests[0].content)
    assert payload["type"] == "candleSnapshot"
    assert payload["req"]["coin"] == "BTC"
    assert payload["req"]["interval"] == "5m"
    assert payload["req"]["endTime"] == int(NOW.timestamp() * 1000)


@pytest.mark.asyncio
async def test_invalid_api_response_or_region_block_never_succeeds():
    for body in ({"error": "invalid"}, [row(coin="ETH")], [dict(row(), h="99")]):
        client, _ = market_client(body)
        async with client:
            with pytest.raises((ValueError, TypeError)):
                await HyperliquidPublicCandles(client).fetch_closed(
                    coin="BTC", count=2, requested_at=NOW
                )
    client, _ = market_client({"error": "region denied"}, status=403)
    async with client:
        with pytest.raises(httpx.HTTPStatusError):
            await HyperliquidPublicCandles(client).fetch_closed(
                coin="BTC", count=2, requested_at=NOW
            )


@pytest.mark.asyncio
async def test_bounded_requests_and_duplicate_timestamp_detection():
    client, requests = market_client([row(), row()])
    async with client:
        with pytest.raises(ValueError, match="duplicate"):
            await HyperliquidPublicCandles(client).fetch_closed(
                coin="BTC", requested_at=NOW
            )
        with pytest.raises(ValueError):
            await HyperliquidPublicCandles(client).fetch_closed(
                coin="BTC", count=600, requested_at=NOW
            )
        with pytest.raises(ValueError):
            await HyperliquidPublicCandles(client).fetch_closed(
                coin="DOGE", requested_at=NOW
            )
        with pytest.raises(ValueError):
            await HyperliquidPublicCandles(client).fetch_closed(
                coin="BTC", requested_at=NOW + timedelta(days=2)
            )
    assert len(requests) == 1


def test_strict_candle_and_timestamp_validation():
    assert normalize_candle(row(minute=5), coin="BTC", interval="5m",
                            requested_at=NOW) is None
    with pytest.raises(ValueError):
        normalize_candle(dict(row(), T=0), coin="BTC", interval="5m",
                         requested_at=NOW)
    with pytest.raises(ValueError):
        normalize_candle(dict(row(), v="-1"), coin="BTC", interval="5m",
                         requested_at=NOW)
    with pytest.raises(ValueError):
        normalize_candle(dict(row(), c="nan"), coin="BTC", interval="5m",
                         requested_at=NOW)
    with pytest.raises(ValueError):
        normalize_candle(dict(row(), n=True), coin="BTC", interval="5m",
                         requested_at=NOW)


@pytest.mark.asyncio
async def test_collect_in_same_db_once_preserves_metadata_and_availability(tmp_path):
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    store.initialize()  # Old sports tables remain accessible and unchanged.
    client, _ = market_client([row()])
    async with client:
        first = await collect_hyperliquid(store=store, client=client, now=NOW)
        second = await collect_hyperliquid(store=store, client=client, now=NOW)
    assert first == (1, 1)
    assert second == (1, 0)
    candles = PerpCandleStore(store)
    identity = {
        "venue": "hyperliquid", "asset": "BTC", "quote": "USDC",
        "contract_kind": "linear_perpetual", "interval": "5m",
    }
    assert candles.recent(**identity, as_of=NOW - timedelta(milliseconds=1)) == []
    found = candles.recent(**identity, as_of=NOW)
    assert len(found) == 1
    assert found[0]["price_close"] == 103
    assert found[0]["first_ingested_at"] == NOW.isoformat()
    with store.connect() as conn:
        sports_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE name='user_jurisdictions'"
        ).fetchone()
    assert sports_table is not None
    store.close()


def test_candle_revision_and_future_bar_roll_back_transaction(tmp_path):
    store = SQLiteHotStore(tmp_path / "data.sqlite")
    candles = PerpCandleStore(store)
    candles.initialize()
    first = normalize_candle(row(), coin="BTC", interval="5m", requested_at=NOW)
    assert first is not None
    assert candles.ingest((first,), ingested_at=NOW) == 1
    with pytest.raises(ValueError, match="changed"):
        candles.ingest((replace(first, close=102),), ingested_at=NOW)
    with pytest.raises(ValueError, match="unfinished"):
        candles.ingest(
            (replace(first, opened_at=NOW, closed_at=NOW + timedelta(minutes=5)
                     - timedelta(milliseconds=1)),),
            ingested_at=NOW,
        )
    found = candles.recent(
        venue="hyperliquid", asset="BTC", quote="USDC",
        contract_kind="linear_perpetual", interval="5m", as_of=NOW,
    )
    assert [c["price_close"] for c in found] == [103]
    store.close()
