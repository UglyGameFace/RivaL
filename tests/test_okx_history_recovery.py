"""OKX public six-month backwards historical recovery: safe paging and provenance."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.discord_app.research_dashboard import ResearchDashboard
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.okx_history_recovery import OkxHistoryRecovery
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def response(req: httpx.Request) -> httpx.Response:
    assert req.url.host == "www.okx.com"
    assert "authorization" not in req.headers
    if req.url.path.endswith("/instruments"):
        contract = req.url.params["instId"]
        return httpx.Response(200, json={"code": "0", "data": [{
            "instId": contract, "instType": "SWAP", "state": "live",
            "ctType": "linear", "settleCcy": "USDT", "ctValCcy": "BTC",
            "ctVal": "0.01", "ctMult": "1",
        }]})
    assert req.url.path.endswith("/history-candles")
    assert req.url.params["bar"] == "5m"
    assert req.url.params["limit"] == "12"
    assert req.url.params["instId"] == "BTC-USDT-SWAP"
    anchor = int(req.url.params["after"])
    assert anchor % 300_000 == 0
    # OKX returns newest first and the 'after' cursor is exclusive.
    return httpx.Response(200, json={"code": "0", "data": [
        [str(anchor - 300_000 * i), "100", "110", "90", "102",
         "200", "2", "204", "1"]
        for i in range(1, 13)
    ]})


@pytest.mark.asyncio
async def test_okx_backward_cursor_restarts_and_preserves_true_observation(tmp_path):
    store = SQLiteHotStore(tmp_path / "okx.sqlite")
    market = PerpetualMarketStore(store)
    calls = []

    def handle(req):
        calls.append(str(req.url))
        return response(req)

    raw = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    first = OkxHistoryRecovery(
        market, client=raw, assets=("BTC",), page_bars=12,
    )
    try:
        assert await first.run_once(now=NOW) == 12
        progress = first.progress()[0]
        assert progress["venue"] == "okx"
        assert progress["inserted_bars"] == 12
        assert progress["status"] == "progress"
        rows = market.recent_candles(
            venue="okx", contract="BTC-USDT-SWAP", quote="USDT",
            interval_minutes=5, count=12,
        )
        assert len(rows) == 12
        assert rows[0]["volume_base"] == pytest.approx(2.0)
        # Historical candles imported in October do NOT appear in an old
        # point-in-time May 2026 training set.
        assert market.recent_candles(
            venue="okx", contract="BTC-USDT-SWAP", quote="USDT",
            interval_minutes=5, as_of=datetime(2026, 5, 1, tzinfo=UTC),
        ) == []
        assert await first.run_once(now=NOW) == 12
        assert len([x for x in calls if "/instruments" in x]) == 1
        restarted = OkxHistoryRecovery(
            market, client=raw, assets=("BTC",), page_bars=12,
        )
        assert await restarted.run_once(now=NOW) == 12
        assert restarted.progress()[0]["inserted_bars"] == 36
        assert len([x for x in calls if "/instruments" in x]) == 2
        assert datetime.fromisoformat(
            restarted.progress()[0]["next_opened_at"]
        ) < datetime.fromisoformat(progress["next_opened_at"])
    finally:
        await raw.aclose()
        store.close()


@pytest.mark.asyncio
async def test_okx_http_451_cools_down_without_moving_cursor(tmp_path):
    store = SQLiteHotStore(tmp_path / "blocked.sqlite")
    market = PerpetualMarketStore(store)
    requests = []

    def denied(req):
        requests.append(req)
        return httpx.Response(451)

    raw = httpx.AsyncClient(transport=httpx.MockTransport(denied))
    worker = OkxHistoryRecovery(market, client=raw, assets=("BTC",))
    try:
        assert await worker.run_once(now=NOW) == 0
        row = worker.progress()[0]
        assert row["status"] == "blocked"
        assert row["next_opened_at"] == row["started_from"]
        assert await worker.run_once(now=NOW + timedelta(minutes=5)) == 0
        assert len(requests) == 1
    finally:
        await raw.aclose()
        store.close()


@pytest.mark.asyncio
async def test_bad_okx_ohlc_or_unconfirmed_page_does_not_advance(tmp_path):
    for invalid in ("ohlc", "unconfirmed", "timestamp"):
        store = SQLiteHotStore(tmp_path / f"{invalid}.sqlite")
        market = PerpetualMarketStore(store)

        def broken(req, variant=invalid):
            if req.url.path.endswith("/instruments"):
                return response(req)
            payload = response(req).json()
            if variant == "ohlc":
                payload["data"][0][2] = "95"
            elif variant == "unconfirmed":
                payload["data"][0][8] = "0"
            else:
                payload["data"][0][0] = str(int(payload["data"][0][0]) + 1)
            return httpx.Response(200, json=payload)

        raw = httpx.AsyncClient(transport=httpx.MockTransport(broken))
        worker = OkxHistoryRecovery(
            market, client=raw, assets=("BTC",), page_bars=12,
        )
        try:
            assert await worker.run_once(now=NOW) == 0
            row = worker.progress()[0]
            assert row["status"] == "retry"
            assert row["next_opened_at"] == row["started_from"]
            assert row["inserted_bars"] == 0
        finally:
            await raw.aclose()
            store.close()


@pytest.mark.asyncio
async def test_okx_progress_on_private_research_menu(tmp_path):
    store = SQLiteHotStore(tmp_path / "menu.sqlite")
    market = PerpetualMarketStore(store)
    worker = OkxHistoryRecovery(market, assets=("BTC",), page_bars=12)
    view = ResearchDashboard(
        owner_id=42, market=market, ai=None,
        venues=("gate", "kucoin", "bitget", "okx"),
        shadow_enabled=False,
    )
    try:
        worker._position("BTC", NOW)
        view.screen = "math"
        view.section = "recovery"
        view._rebuild()
        fields = {x.name: x.value for x in (await view.render()).fields}
        assert "Historical recovery progress" in fields
        assert "**OKX**: pending" in fields["Historical recovery progress"]
        assert "Backfills are NOT past live predictions" in fields[
            "Historical recovery progress"
        ]
        assert len(fields) <= 3
    finally:
        await worker.aclose()
        view.stop()
        store.close()


def test_okx_recovery_honors_public_100_bar_limit(tmp_path):
    store = SQLiteHotStore(tmp_path / "bars.sqlite")
    try:
        market = PerpetualMarketStore(store)
        with pytest.raises(ValueError, match="100"):
            OkxHistoryRecovery(market, page_bars=101)
    finally:
        store.close()
