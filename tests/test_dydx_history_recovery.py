"""dYdX indexer history recovery: real units, durable cursors.

The range params (fromISO/toISO) are exercised here through the mock exactly
as the recovery sends them; whether the live indexer honors them is proven
by the first live cycle's received/inserted log line, not by this test.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.perpetuals.dydx_history_recovery import DydxHistoryRecovery
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def response(request: httpx.Request) -> httpx.Response:
    assert "authorization" not in request.headers
    assert request.url.host == "indexer.dydx.trade"
    if request.url.path.endswith("/perpetualMarkets"):
        return httpx.Response(200, json={"markets": {
            "BTC-USD": {"ticker": "BTC-USD", "status": "ACTIVE"},
        }})
    assert "/candles/perpetualMarkets/BTC-USD" in request.url.path
    assert request.url.params["resolution"] == "5MINS"
    start = datetime.fromisoformat(request.url.params["fromISO"])
    end = datetime.fromisoformat(request.url.params["toISO"])
    assert (end - start) == timedelta(minutes=12 * 5)
    return httpx.Response(200, json={"candles": [
        {"ticker": "BTC-USD", "resolution": "5MINS",
         "startedAt": (start + timedelta(minutes=5 * i)).isoformat(),
         "open": "100", "high": "110", "low": "95", "close": "102",
         "baseTokenVolume": "1.25", "trades": "42"}
        for i in range(12)
    ]})


@pytest.mark.asyncio
async def test_dydx_history_recovers_restart_safe_and_live_provenance(tmp_path) -> None:
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    market = PerpetualMarketStore(store)
    calls = []

    def reply(req):
        calls.append(str(req.url))
        return response(req)

    raw = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    worker = DydxHistoryRecovery(
        market, assets=("BTC",), page_bars=12, client=raw,
    )
    try:
        assert await worker.run_once(now=NOW) == 12
        first = worker.progress()[0]
        assert first["venue"] == "dydx"
        assert first["status"] == "progress"
        assert first["inserted_bars"] == 12
        assert await worker.run_once(now=NOW + timedelta(minutes=5)) == 12
        assert len([x for x in calls if "perpetualMarkets" in x and "candles" not in x]) == 1
        assert len([x for x in calls if "candles" in x]) == 2
        restarted = DydxHistoryRecovery(
            market, assets=("BTC",), page_bars=12, client=raw,
        )
        assert await restarted.run_once(now=NOW + timedelta(minutes=5)) == 12
        assert restarted.progress()[0]["inserted_bars"] == 36
        saved = market.recent_candles(
            venue="dydx", contract="BTC-USD",
            quote="USD", interval_minutes=5, count=50,
        )
        assert len(saved) == 36
        assert saved[0]["volume_base"] == pytest.approx(1.25)
        assert saved[0]["trades"] == -1
        assert datetime.fromisoformat(saved[0]["first_observed_at"]) > (
            datetime(2026, 5, 1, tzinfo=UTC)
        )
        assert market.recent_candles(
            venue="dydx", contract="BTC-USD",
            quote="USD", interval_minutes=5, as_of=datetime(2026, 5, 1, tzinfo=UTC),
        ) == []
    finally:
        await raw.aclose()
        store.close()


@pytest.mark.asyncio
async def test_dydx_451_pauses_without_advancing_cursor(tmp_path) -> None:
    store = SQLiteHotStore(tmp_path / "blocked.sqlite")
    market = PerpetualMarketStore(store)
    calls = []
    raw = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: (calls.append(req), httpx.Response(451))[1]
    ))
    worker = DydxHistoryRecovery(
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
async def test_invalid_dydx_rows_fail_closed(tmp_path) -> None:
    store = SQLiteHotStore(tmp_path / "invalid.sqlite")
    market = PerpetualMarketStore(store)

    def bad(req):
        if req.url.path.endswith("/perpetualMarkets"):
            return response(req)
        start = datetime.fromisoformat(req.url.params["fromISO"])
        return httpx.Response(200, json={"candles": [
            {"ticker": "BTC-USD", "resolution": "5MINS",
             "startedAt": (start + timedelta(seconds=1)).isoformat(),
             "open": "100", "high": "110", "low": "95", "close": "102",
             "baseTokenVolume": "1.25", "trades": "42"},
        ]})

    raw = httpx.AsyncClient(transport=httpx.MockTransport(bad))
    worker = DydxHistoryRecovery(
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
