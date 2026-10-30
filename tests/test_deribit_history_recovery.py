"""Deribit USDC linear perpetual history recovery: real units, durable cursors."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.perpetuals.deribit_history_recovery import DeribitHistoryRecovery
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def response(request: httpx.Request) -> httpx.Response:
    assert "authorization" not in request.headers
    assert request.url.host == "www.deribit.com"
    if request.url.path.endswith("/get_instrument"):
        assert request.url.params["instrument_name"] == "BTC_USDC-PERPETUAL"
        return httpx.Response(200, json={"result": {
            "instrument_name": "BTC_USDC-PERPETUAL", "kind": "future",
            "settlement_period": "perpetual", "instrument_type": "linear",
            "counter_currency": "USDC", "settlement_currency": "USDC",
            "is_active": True, "state": "open",
        }})
    assert request.url.path.endswith("/get_tradingview_chart_data")
    assert request.url.params["instrument_name"] == "BTC_USDC-PERPETUAL"
    assert request.url.params["resolution"] == "5"
    start = int(request.url.params["start_timestamp"])
    end = int(request.url.params["end_timestamp"])
    assert end - start == 12 * 300_000 - 1
    assert start % 300_000 == 0
    ticks = [start + i * 300_000 for i in range(12)]
    return httpx.Response(200, json={"result": {
        "status": "ok", "ticks": ticks,
        "open": [100.0] * 12, "high": [110.0] * 12,
        "low": [95.0] * 12, "close": [102.0] * 12,
        "volume": [1.25] * 12,
    }})


@pytest.mark.asyncio
async def test_deribit_history_recovers_restart_safe_and_live_provenance(tmp_path) -> None:
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    market = PerpetualMarketStore(store)
    calls = []

    def reply(req):
        calls.append(str(req.url))
        return response(req)

    raw = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    worker = DeribitHistoryRecovery(
        market, assets=("BTC",), page_bars=12, client=raw,
    )
    try:
        assert await worker.run_once(now=NOW) == 12
        first = worker.progress()[0]
        assert first["venue"] == "deribit"
        assert first["status"] == "progress"
        assert first["inserted_bars"] == 12
        assert await worker.run_once(now=NOW + timedelta(minutes=5)) == 12
        assert len([x for x in calls if "get_instrument" in x]) == 1
        assert len([x for x in calls if "get_tradingview_chart_data" in x]) == 2
        restarted = DeribitHistoryRecovery(
            market, assets=("BTC",), page_bars=12, client=raw,
        )
        assert await restarted.run_once(now=NOW + timedelta(minutes=5)) == 12
        assert restarted.progress()[0]["inserted_bars"] == 36
        saved = market.recent_candles(
            venue="deribit", contract="BTC_USDC-PERPETUAL",
            quote="USDC", interval_minutes=5, count=50,
        )
        assert len(saved) == 36
        assert saved[0]["volume_base"] == pytest.approx(1.25)
        assert saved[0]["trades"] == -1
        assert datetime.fromisoformat(saved[0]["first_observed_at"]) > (
            datetime(2026, 5, 1, tzinfo=UTC)
        )
        assert market.recent_candles(
            venue="deribit", contract="BTC_USDC-PERPETUAL",
            quote="USDC", interval_minutes=5, as_of=datetime(2026, 5, 1, tzinfo=UTC),
        ) == []
    finally:
        await raw.aclose()
        store.close()


@pytest.mark.asyncio
async def test_deribit_451_pauses_without_advancing_cursor(tmp_path) -> None:
    store = SQLiteHotStore(tmp_path / "blocked.sqlite")
    market = PerpetualMarketStore(store)
    calls = []
    raw = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: (calls.append(req), httpx.Response(451))[1]
    ))
    worker = DeribitHistoryRecovery(
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
async def test_invalid_deribit_rows_fail_closed(tmp_path) -> None:
    store = SQLiteHotStore(tmp_path / "invalid.sqlite")
    market = PerpetualMarketStore(store)

    def bad(req):
        if req.url.path.endswith("/get_instrument"):
            return response(req)
        start = int(req.url.params["start_timestamp"]) + 1
        return httpx.Response(200, json={"result": {
            "status": "ok", "ticks": [start],
            "open": [100.0], "high": [110.0], "low": [95.0],
            "close": [102.0], "volume": [1.25],
        }})

    raw = httpx.AsyncClient(transport=httpx.MockTransport(bad))
    worker = DeribitHistoryRecovery(
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
