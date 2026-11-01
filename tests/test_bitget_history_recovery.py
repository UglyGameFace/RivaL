"""Bitget V3 six-month historical recovery: real units, durable cursors, safe limits."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.discord_app.research_dashboard import ResearchDashboard
from parlay_bot.perpetuals.bitget_history_recovery import BitgetHistoryRecovery
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def response(request: httpx.Request) -> httpx.Response:
    assert "authorization" not in request.headers
    assert request.url.host == "api.bitget.com"
    if request.url.path.endswith("/instruments"):
        asset = request.url.params["symbol"][:-4]
        return httpx.Response(200, json={"code": "00000", "data": [{
            "symbol": f"{asset}USDT", "category": "USDT-FUTURES",
            "type": "perpetual", "status": "online",
            "baseCoin": asset, "quoteCoin": "USDT",
        }]})
    assert request.url.path.endswith("/history-candles")
    assert request.url.params["category"] == "USDT-FUTURES"
    assert request.url.params["type"] == "market"
    assert request.url.params["interval"] == "5m"
    assert request.url.params["limit"] == "12"
    start = int(request.url.params["startTime"])
    end = int(request.url.params["endTime"])
    assert end - start == 12 * 300_000
    assert end % 300_000 == 299_999
    first = start + 1
    return httpx.Response(200, json={"code": "00000", "data": [
        [str(first + i * 300_000), "100", "110", "95", "102", "1.25", "127.50"]
        for i in range(12)
    ]})


@pytest.mark.asyncio
async def test_bitget_six_month_history_recovers_restart_safe_and_live_provenance(
    tmp_path,
):
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    market = PerpetualMarketStore(store)
    calls = []

    def reply(req):
        calls.append(str(req.url))
        return response(req)

    raw = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    worker = BitgetHistoryRecovery(
        market, assets=("BTC",), page_bars=12, client=raw,
    )
    try:
        assert await worker.run_once(now=NOW) == 12
        first = worker.progress()[0]
        assert first["venue"] == "bitget"
        assert first["status"] == "progress"
        assert first["inserted_bars"] == 12
        assert await worker.run_once(now=NOW + timedelta(minutes=5)) == 12
        # Static instrument metadata is cached, not historical candle responses.
        assert len([x for x in calls if "/instruments" in x]) == 1
        assert len([x for x in calls if "/history-candles" in x]) == 2
        restarted = BitgetHistoryRecovery(
            market, assets=("BTC",), page_bars=12, client=raw,
        )
        assert await restarted.run_once(now=NOW + timedelta(minutes=5)) == 12
        assert restarted.progress()[0]["inserted_bars"] == 36
        assert len([x for x in calls if "/instruments" in x]) == 2
        saved = market.recent_candles(
            venue="bitget", contract="BTCUSDT",
            quote="USDT", interval_minutes=5, count=50,
        )
        assert len(saved) == 36
        assert saved[0]["volume_base"] == pytest.approx(1.25)
        assert saved[0]["trades"] == -1
        assert datetime.fromisoformat(saved[0]["first_observed_at"]) > (
            datetime(2026, 5, 1, tzinfo=UTC)
        )
        assert market.recent_candles(
            venue="bitget", contract="BTCUSDT", quote="USDT",
            interval_minutes=5, as_of=datetime(2026, 5, 1, tzinfo=UTC),
        ) == []
    finally:
        await raw.aclose()
        store.close()


@pytest.mark.asyncio
async def test_bitget_451_pauses_only_history_without_advancing_cursor(tmp_path):
    store = SQLiteHotStore(tmp_path / "blocked.sqlite")
    market = PerpetualMarketStore(store)
    calls = []
    raw = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: (calls.append(req), httpx.Response(451))[1]
    ))
    worker = BitgetHistoryRecovery(
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
async def test_invalid_historical_bitget_rows_fail_closed(tmp_path):
    store = SQLiteHotStore(tmp_path / "invalid.sqlite")
    market = PerpetualMarketStore(store)

    def bad(req):
        if req.url.path.endswith("/instruments"):
            return response(req)
        start = int(req.url.params["startTime"]) + 1
        return httpx.Response(200, json={"code": "00000", "data": [
            [str(start), "100", "90", "95", "102", "1.25", "127"],
        ]})

    raw = httpx.AsyncClient(transport=httpx.MockTransport(bad))
    worker = BitgetHistoryRecovery(
        market, assets=("BTC",), page_bars=12, client=raw,
    )
    try:
        assert await worker.run_once(now=NOW) == 0
        status = worker.progress()[0]
        assert status["status"] == "retry"
        assert status["next_opened_at"] == status["started_from"]
        assert status["inserted_bars"] == 0
        assert await worker.run_once(now=NOW + timedelta(minutes=5)) == 0
    finally:
        await raw.aclose()
        store.close()


@pytest.mark.asyncio
async def test_bitget_history_page_bounds_and_compact_progress(tmp_path):
    store = SQLiteHotStore(tmp_path / "page.sqlite")
    market = PerpetualMarketStore(store)
    try:
        with pytest.raises(ValueError, match="100"):
            BitgetHistoryRecovery(market, assets=("BTC",), page_bars=101)
        worker = BitgetHistoryRecovery(
            market, assets=("BTC",), page_bars=12,
        )
        try:
            worker._position("BTC", NOW)
            view = ResearchDashboard(
                owner_id=42, market=market, ai=None,
                venues=("gate", "kucoin", "bitget"),
                shadow_enabled=False,
            )
            try:
                view.screen = "math"
                view.section = "recovery"
                view._rebuild()
                embed = await view.render()
                assert len(embed.fields) <= 3
                detail = next(
                    field.value for field in embed.fields
                    if field.name == "Historical recovery progress"
                )
                assert "**Bitget**: pending" in detail
                assert "**Gate**: not started" in detail
                assert "**KuCoin**: not started" in detail
                assert "NOT past live predictions" in detail
            finally:
                view.stop()
        finally:
            await worker.aclose()
    finally:
        store.close()


def empty_response(request: httpx.Request) -> httpx.Response:
    assert "authorization" not in request.headers
    assert request.url.host == "api.bitget.com"
    if request.url.path.endswith("/instruments"):
        asset = request.url.params["symbol"][:-4]
        return httpx.Response(200, json={"code": "00000", "data": [{
            "symbol": f"{asset}USDT", "category": "USDT-FUTURES",
            "type": "perpetual", "status": "online",
            "baseCoin": asset, "quoteCoin": "USDT",
        }]})
    assert request.url.path.endswith("/history-candles")
    return httpx.Response(200, json={"code": "00000", "data": []})


@pytest.mark.asyncio
async def test_bitget_empty_page_does_not_advance_cursor(tmp_path):
    from parlay_bot.perpetuals.history_inventory import six_complete_months_start

    store = SQLiteHotStore(tmp_path / "bitget-empty.sqlite")
    market = PerpetualMarketStore(store)
    raw = httpx.AsyncClient(transport=httpx.MockTransport(empty_response))
    worker = BitgetHistoryRecovery(
        market, assets=("BTC",), page_bars=12, client=raw,
    )
    try:
        assert await worker.run_once(now=NOW) == 0
        row = worker.progress()[0]
        seed = six_complete_months_start(NOW).replace(
            minute=six_complete_months_start(NOW).minute // 5 * 5,
            second=0, microsecond=0)
        assert row["next_opened_at"] == seed.isoformat()
        assert row["status"] == "upstream_empty"
        assert row["retry_after"] == (NOW + timedelta(hours=6)).isoformat()
        assert row["empty_windows"] == 1
        assert row["inserted_bars"] == 0
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_bitget_exact_slot_queue_repairs_scattered_audited_gaps_without_cursor_rewind(
    tmp_path, monkeypatch,
):
    """Bitget missing one-bar slots are fetched exactly, not filled from other venues."""
    import parlay_bot.perpetuals.history_quality as quality_module
    from parlay_bot.perpetuals.history_quality import SixMonthQualityAudit
    from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle

    start = NOW - timedelta(hours=6)
    monkeypatch.setattr(quality_module, "six_months_prior", lambda _: start)
    store = SQLiteHotStore(tmp_path / "bitget-scattered-gaps.sqlite")
    market = PerpetualMarketStore(store)
    skipped = {1, 4, 8}
    known = [
        MarketCandle(
            "bitget", "BTCUSDT", "USDT", 5,
            start + timedelta(minutes=5 * i),
            start + timedelta(minutes=5 * (i + 1), milliseconds=-1),
            NOW, 100, 110, 95, 102, 1.5, -1,
        )
        for i in range(72) if i not in skipped
    ]
    market.ingest(MarketBatch((), tuple(known), 5, NOW))
    audited = SixMonthQualityAudit(market).audit_stream(
        venue="bitget", asset="BTC", now=NOW,
    )
    assert audited["missing_bars"] == 3
    assert audited["gap_segments"] == 3
    with store.connect() as conn:
        queued = conn.execute(
            """SELECT opened_at FROM perp_history_gap_repair
               WHERE venue='bitget' AND asset='BTC' ORDER BY opened_at"""
        ).fetchall()
    assert [row["opened_at"] for row in queued] == [
        (start + timedelta(minutes=5 * i)).isoformat()
        for i in sorted(skipped)
    ]

    requested = []

    def exact(req):
        if req.url.path.endswith("/instruments"):
            return response(req)
        assert req.url.path.endswith("/history-candles")
        assert req.url.params["limit"] == "2"
        start_ms = int(req.url.params["startTime"]) + 1
        assert int(req.url.params["endTime"]) == start_ms + 300_000 - 1
        requested.append(start_ms)
        return httpx.Response(200, json={"code": "00000", "data": [
            [str(start_ms), "100", "110", "95", "102", "1.25", "127.50"],
        ]})

    raw = httpx.AsyncClient(transport=httpx.MockTransport(exact))
    worker = BitgetHistoryRecovery(
        market, assets=("BTC",), page_bars=12, client=raw,
    )
    try:
        worker._position("BTC", NOW)
        cursor = NOW - timedelta(hours=4)
        with store.connect() as conn:
            conn.execute(
                """UPDATE perp_history_recovery
                   SET next_opened_at=? WHERE venue='bitget' AND asset='BTC'""",
                (cursor.isoformat(),),
            )
        # The same scheduled Bitget turn can heal multiple isolated slots
        # without multiplying its normal long-range history request.
        assert await worker.run_once(now=NOW) == 3
        assert requested == [
            int((start + timedelta(minutes=5 * i)).timestamp() * 1000)
            for i in sorted(skipped)
        ]
        with store.connect() as conn:
            left = conn.execute(
                "SELECT COUNT(*) AS n FROM perp_history_gap_repair WHERE venue='bitget'"
            ).fetchone()
            total = conn.execute(
                """SELECT COUNT(*) AS n FROM perp_market_candles
                   WHERE venue='bitget' AND contract='BTCUSDT'""",
            ).fetchone()
        assert left["n"] == 0
        assert total["n"] == 72
        assert worker.progress()[0]["next_opened_at"] == cursor.isoformat()
        assert worker.progress()[0]["inserted_bars"] == 3
        # Nothing else is fetched or certified by queue processing.
        assert await worker.fetch_asset("BTC", now=NOW + timedelta(minutes=3)) == 0
        assert len(requested) == 3
        report = SixMonthQualityAudit(market).audit_stream(
            venue="bitget", asset="BTC", now=NOW,
        )
        assert report["missing_bars"] == 0
        # An actual future observed_at must not be reclassified as a
        # 2026-10-28 point-in-time signal by this synthetic test clock.
        assert report["status"] == "gaps_or_anomalies"
        assert report["invalid_observations"] == 3
    finally:
        await raw.aclose()
        store.close()


@pytest.mark.asyncio
async def test_bitget_unavailable_exact_slot_remains_queued_with_backoff(
    tmp_path, monkeypatch,
):
    import parlay_bot.perpetuals.history_quality as quality_module
    from parlay_bot.perpetuals.history_quality import SixMonthQualityAudit
    from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle

    start = NOW - timedelta(hours=6)
    monkeypatch.setattr(quality_module, "six_months_prior", lambda _: start)
    store = SQLiteHotStore(tmp_path / "bitget-missing-slot.sqlite")
    market = PerpetualMarketStore(store)
    candles = tuple(
        MarketCandle(
            "bitget", "BTCUSDT", "USDT", 5,
            start + timedelta(minutes=5 * i),
            start + timedelta(minutes=5 * (i + 1), milliseconds=-1),
            NOW, 100, 110, 95, 102, 1.5, -1,
        )
        for i in range(72) if i != 3
    )
    market.ingest(MarketBatch((), candles, 5, NOW))
    SixMonthQualityAudit(market).audit_stream(
        venue="bitget", asset="BTC", now=NOW,
    )
    calls = []

    def missing(req):
        if req.url.path.endswith("/instruments"):
            return response(req)
        calls.append(str(req.url))
        # An empty response is not permission to invent the missing candle.
        return httpx.Response(200, json={"code": "00000", "data": []})

    raw = httpx.AsyncClient(transport=httpx.MockTransport(missing))
    worker = BitgetHistoryRecovery(
        market, assets=("BTC",), page_bars=12, client=raw,
    )
    try:
        worker._position("BTC", NOW)
        with store.connect() as conn:
            conn.execute(
                """UPDATE perp_history_recovery SET next_opened_at=?
                   WHERE venue='bitget' AND asset='BTC'""",
                ((NOW - timedelta(hours=4)).isoformat(),),
            )
        assert await worker.fetch_asset("BTC", now=NOW) == 0
        assert worker.progress()[0]["status"] == "gaps_pending"
        assert await worker.fetch_asset("BTC", now=NOW + timedelta(minutes=1)) == 0
        assert len(calls) == 1
        with store.connect() as conn:
            queued = conn.execute(
                """SELECT opened_at,retry_after FROM perp_history_gap_repair
                   WHERE venue='bitget' AND asset='BTC'"""
            ).fetchone()
        assert queued["opened_at"] == (start + timedelta(minutes=15)).isoformat()
        assert datetime.fromisoformat(queued["retry_after"]) >= NOW + timedelta(hours=6)
        assert worker.progress()[0]["next_opened_at"] == (
            NOW - timedelta(hours=4)
        ).isoformat()
    finally:
        await raw.aclose()
        store.close()


def test_bitget_large_missing_month_is_not_misclassified_as_small_gap(
    tmp_path, monkeypatch,
):
    import parlay_bot.perpetuals.history_quality as quality_module
    from parlay_bot.perpetuals.history_quality import SixMonthQualityAudit

    monkeypatch.setattr(
        quality_module, "six_months_prior", lambda _: NOW - timedelta(days=3),
    )
    store = SQLiteHotStore(tmp_path / "bitget-large-absence.sqlite")
    market = PerpetualMarketStore(store)
    try:
        result = SixMonthQualityAudit(market).audit_stream(
            venue="bitget", asset="BTC", now=NOW,
        )
        assert result["missing_bars"] == 3 * 24 * 12
        assert result["status"] == "gaps_or_anomalies"
        with store.connect() as conn:
            count = conn.execute(
                """SELECT COUNT(*) AS n FROM perp_history_gap_repair
                   WHERE venue='bitget'"""
            ).fetchone()
        assert count["n"] == 0
    finally:
        store.close()
