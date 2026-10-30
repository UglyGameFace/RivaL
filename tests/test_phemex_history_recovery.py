"""Phemex USDT perpetual history recovery: real units, durable cursors.

The documented range endpoint (/kline/list with aligned from/to) is exercised
exactly as the recovery sends it; whether the live API honors it is proven
by the first live cycle's received/inserted log line, not by this test.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.phemex_history_recovery import PhemexHistoryRecovery
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def response(request: httpx.Request) -> httpx.Response:
    assert "authorization" not in request.headers
    assert request.url.host == "api.phemex.com"
    if request.url.path.endswith("/public/products"):
        return httpx.Response(200, json={"code": 0, "data": {"perpProductsV2": [
            {"symbol": "BTCUSDT", "type": "PerpetualV2", "status": "Listed",
             "baseCurrency": "BTC", "quoteCurrency": "USDT",
             "settleCurrency": "USDT"},
        ]}})
    assert request.url.path.endswith("/md/v2/kline/list")
    assert request.url.params["symbol"] == "BTCUSDT"
    assert request.url.params["resolution"] == "300"
    start = int(request.url.params["from"])
    end = int(request.url.params["to"])
    assert end - start == 12 * 300
    assert start % 300 == end % 300 == 0
    return httpx.Response(200, json={"code": 0, "data": {"rows": [
        [start + i * 300, 300, 0, "100", "110", "95", "102", "1.25", "127.50"]
        for i in range(12)
    ]}})


@pytest.mark.asyncio
async def test_phemex_history_recovers_restart_safe_and_live_provenance(tmp_path) -> None:
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    market = PerpetualMarketStore(store)
    calls = []

    def reply(req):
        calls.append(str(req.url))
        return response(req)

    raw = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    worker = PhemexHistoryRecovery(
        market, assets=("BTC",), page_bars=12, client=raw,
    )
    try:
        assert await worker.run_once(now=NOW) == 12
        first = worker.progress()[0]
        assert first["venue"] == "phemex"
        assert first["status"] == "progress"
        assert first["inserted_bars"] == 12
        assert await worker.run_once(now=NOW + timedelta(minutes=5)) == 12
        assert len([x for x in calls if "products" in x]) == 1
        assert len([x for x in calls if "kline" in x]) == 2
        restarted = PhemexHistoryRecovery(
            market, assets=("BTC",), page_bars=12, client=raw,
        )
        assert await restarted.run_once(now=NOW + timedelta(minutes=5)) == 12
        assert restarted.progress()[0]["inserted_bars"] == 36
        saved = market.recent_candles(
            venue="phemex", contract="BTCUSDT",
            quote="USDT", interval_minutes=5, count=50,
        )
        assert len(saved) == 36
        assert saved[0]["volume_base"] == pytest.approx(1.25)
        assert saved[0]["trades"] == -1
        assert datetime.fromisoformat(saved[0]["first_observed_at"]) > (
            datetime(2026, 5, 1, tzinfo=UTC)
        )
        assert market.recent_candles(
            venue="phemex", contract="BTCUSDT",
            quote="USDT", interval_minutes=5, as_of=datetime(2026, 5, 1, tzinfo=UTC),
        ) == []
    finally:
        await raw.aclose()
        store.close()


@pytest.mark.asyncio
async def test_phemex_451_pauses_without_advancing_cursor(tmp_path) -> None:
    store = SQLiteHotStore(tmp_path / "blocked.sqlite")
    market = PerpetualMarketStore(store)
    calls = []
    raw = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: (calls.append(req), httpx.Response(451))[1]
    ))
    worker = PhemexHistoryRecovery(
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
async def test_invalid_phemex_rows_fail_closed(tmp_path) -> None:
    store = SQLiteHotStore(tmp_path / "invalid.sqlite")
    market = PerpetualMarketStore(store)

    def bad(req):
        if req.url.path.endswith("/public/products"):
            return response(req)
        start = int(req.url.params["from"]) + 1
        return httpx.Response(200, json={"code": 0, "data": {"rows": [
            [start, 300, 0, "100", "110", "95", "102", "1.25", "127.50"],
        ]}})

    raw = httpx.AsyncClient(transport=httpx.MockTransport(bad))
    worker = PhemexHistoryRecovery(
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
