"""Gate's recent-candle window is not its historical source archive.

A 400 recency wall should never erase an old cursor, fabricate 5m bars,
or cause one repeated failing source to interrupt other collectors.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.perpetuals.history_recovery import GateHistoryRecovery
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore

CLOCK = datetime(2026, 11, 1, 17, 7, tzinfo=UTC)
RECENT_LIMIT = {
    "label": "INVALID_PARAM_VALUE",
    "message": "Candlestick too long ago. Maximum 10000 points recently are allowed",
}


@pytest.mark.asyncio
async def test_gate_recent_history_limit_preserves_cursor_across_restart(tmp_path):
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if "/contracts/" in request.url.path:
            return httpx.Response(200, json={
                "name": "BTC_USDT", "type": "direct", "status": "trading",
                "in_delisting": False, "quanto_multiplier": "1",
            })
        assert request.url.path.endswith("/candlesticks")
        return httpx.Response(400, json=RECENT_LIMIT)

    store = SQLiteHotStore(tmp_path / "recent-gate.sqlite")
    market = PerpetualMarketStore(store)
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    worker = GateHistoryRecovery(market, assets=("BTC",), client=client)
    try:
        assert await worker.fetch_asset("BTC", now=CLOCK) == 0
        row = worker.progress()[0]
        assert row["status"] == "source_recent_limit"
        assert row["next_opened_at"] == row["started_from"]
        assert row["inserted_bars"] == 0
        assert datetime.fromisoformat(row["retry_after"]) == CLOCK + timedelta(days=1)
        assert len(requests) == 2
        # The receipt survives a process restart without expensive refetching.
        restarted = GateHistoryRecovery(market, assets=("BTC",), client=client)
        assert await restarted.fetch_asset("BTC", now=CLOCK + timedelta(hours=23)) == 0
        assert len(requests) == 2
        assert await restarted.fetch_asset("BTC", now=CLOCK + timedelta(days=1)) == 0
        assert len(requests) == 4
        with store.connect() as conn:
            actual = conn.execute(
                "SELECT COUNT(*) AS n FROM perp_market_candles "
                "WHERE venue='gate' AND contract='BTC_USDT'"
            ).fetchone()
        assert actual["n"] == 0
    finally:
        await client.aclose()
        store.close()


@pytest.mark.asyncio
async def test_gate_unknown_bad_request_is_retry_not_archive_limit(tmp_path):
    calls = []

    def respond(request):
        calls.append(request)
        if "/contracts/" in request.url.path:
            return httpx.Response(200, json={
                "name": "ETH_USDT", "type": "direct", "status": "trading",
                "in_delisting": False, "quanto_multiplier": "1",
            })
        return httpx.Response(400, json={
            "label": "INVALID_PARAM_VALUE", "message": "Unknown interval",
        })

    store = SQLiteHotStore(tmp_path / "bad-gate.sqlite")
    market = PerpetualMarketStore(store)
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    worker = GateHistoryRecovery(market, assets=("ETH",), client=client)
    try:
        assert await worker.fetch_asset("ETH", now=CLOCK) == 0
        row = worker.progress()[0]
        assert row["status"] == "retry"
        assert datetime.fromisoformat(row["retry_after"]) == CLOCK + timedelta(minutes=20)
        assert row["next_opened_at"] == row["started_from"]
        assert len(calls) == 2
    finally:
        await client.aclose()
        store.close()
