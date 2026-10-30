"""Lighter USD perpetual history recovery: real units, durable cursors."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.perpetuals.lighter_history_recovery import LighterHistoryRecovery
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def response(request: httpx.Request) -> httpx.Response:
    assert "authorization" not in request.headers
    assert request.url.host == "mainnet.zklighter.elliot.ai"
    if request.url.path.endswith("/orderBooks"):
        return httpx.Response(200, json={"code": 200, "order_books": [
            {"symbol": "BTC", "market_type": "perp", "status": "active",
             "market_id": 1},
            {"symbol": "ETH", "market_type": "perp", "status": "active",
             "market_id": 2},
        ]})
    assert request.url.path.endswith("/candles")
    assert request.url.params["market_id"] == "1"
    assert request.url.params["resolution"] == "5m"
    assert request.url.params["count_back"] == "12"
    start = int(request.url.params["start_timestamp"])
    end = int(request.url.params["end_timestamp"])
    assert end - start == 12 * 300
    first = start * 1000
    return httpx.Response(200, json={"code": 200, "r": "5m", "c": [
        {"t": first + i * 300_000, "o": "100", "h": "110",
         "l": "95", "c": "102", "v": "1.25"}
        for i in range(12)
    ]})


@pytest.mark.asyncio
async def test_lighter_history_recovers_restart_safe_and_live_provenance(tmp_path) -> None:
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    market = PerpetualMarketStore(store)
    calls = []

    def reply(req):
        calls.append(str(req.url))
        return response(req)

    raw = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    worker = LighterHistoryRecovery(
        market, assets=("BTC",), page_bars=12, client=raw,
    )
    try:
        assert await worker.run_once(now=NOW) == 12
        first = worker.progress()[0]
        assert first["venue"] == "lighter"
        assert first["status"] == "progress"
        assert first["inserted_bars"] == 12
        assert await worker.run_once(now=NOW + timedelta(minutes=5)) == 12
        assert len([x for x in calls if "orderBooks" in x]) == 1
        assert len([x for x in calls if "/candles" in x]) == 2
        restarted = LighterHistoryRecovery(
            market, assets=("BTC",), page_bars=12, client=raw,
        )
        assert await restarted.run_once(now=NOW + timedelta(minutes=5)) == 12
        assert restarted.progress()[0]["inserted_bars"] == 36
        saved = market.recent_candles(
            venue="lighter", contract="BTC",
            quote="USD", interval_minutes=5, count=50,
        )
        assert len(saved) == 36
        assert saved[0]["volume_base"] == pytest.approx(1.25)
        assert saved[0]["trades"] == -1
        assert datetime.fromisoformat(saved[0]["first_observed_at"]) > (
            datetime(2026, 5, 1, tzinfo=UTC)
        )
        assert market.recent_candles(
            venue="lighter", contract="BTC",
            quote="USD", interval_minutes=5, as_of=datetime(2026, 5, 1, tzinfo=UTC),
        ) == []
    finally:
        await raw.aclose()
        store.close()


@pytest.mark.asyncio
async def test_lighter_451_pauses_without_advancing_cursor(tmp_path) -> None:
    store = SQLiteHotStore(tmp_path / "blocked.sqlite")
    market = PerpetualMarketStore(store)
    calls = []
    raw = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: (calls.append(req), httpx.Response(451))[1]
    ))
    worker = LighterHistoryRecovery(
        market, assets=("BTC",), page_bars=12, client=raw,
    )
    try:
        assert await worker.run_once(now=NOW) == 0
        row = worker.progress()[0]
        assert row["status"] == "blocked"
        assert row["next_opened_at"] == row["started_from"]
        assert await worker.run_once(now=NOW + timedelta(hours=1)) == 0
        assert len(calls) == 1
    finally:
        await raw.aclose()
        store.close()


@pytest.mark.asyncio
async def test_invalid_lighter_rows_fail_closed(tmp_path) -> None:
    store = SQLiteHotStore(tmp_path / "invalid.sqlite")
    market = PerpetualMarketStore(store)

    def bad(req):
        if req.url.path.endswith("/orderBooks"):
            return response(req)
        start = int(req.url.params["start_timestamp"]) * 1000 + 1
        return httpx.Response(200, json={"code": 200, "r": "5m", "c": [
            {"t": start, "o": "100", "h": "110", "l": "95",
             "c": "102", "v": "1.25"},
        ]})

    raw = httpx.AsyncClient(transport=httpx.MockTransport(bad))
    worker = LighterHistoryRecovery(
        market, assets=("BTC",), page_bars=12, client=raw,
    )
    try:
        assert await worker.run_once(now=NOW) == 0
        row = worker.progress()[0]
        assert row["next_opened_at"] == row["started_from"]
        assert row["inserted_bars"] == 0
    finally:
        await raw.aclose()
        store.close()
