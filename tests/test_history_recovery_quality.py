"""Real continuity checks and bounded restart-safe Gate six-month backfill."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.perpetuals.history_quality import SixMonthQualityAudit
from parlay_bot.perpetuals.history_recovery import GateHistoryRecovery
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


AT = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def _candle(opened: datetime, observed: datetime, *, venue: str = "gate"):
    return MarketCandle(
        venue, "BTC_USDT", "USDT", 5, opened,
        opened + timedelta(minutes=5, milliseconds=-1), observed,
        100, 110, 95, 102, 1.5, -1,
    )


def test_six_month_audit_identifies_exact_missing_slots_and_rechecks(tmp_path):
    store = SQLiteHotStore(tmp_path / "six.sqlite")
    market = PerpetualMarketStore(store)
    quality = SixMonthQualityAudit(market)
    begin = datetime(2026, 4, 28, 12, tzinfo=UTC)
    try:
        market.ingest(MarketBatch((), (
            _candle(begin, AT), _candle(begin + timedelta(minutes=10), AT),
        ), 5, AT))
        first = quality.audit_stream(venue="gate", asset="BTC", now=AT)
        assert first["valid_bars"] == 2
        assert first["missing_bars"] == first["expected_bars"] - 2
        assert first["gap_segments"] == 2  # 5m hole plus trailing history
        assert first["longest_gap_bars"] >= 1
        assert first["status"] == "gaps_or_anomalies"
        assert quality.latest(asset="BTC", venues=("gate",))[0]["status"] == first["status"]
        market.ingest(MarketBatch((), (
            _candle(begin + timedelta(minutes=5), AT),
        ), 5, AT))
        second = quality.audit_stream(venue="gate", asset="BTC", now=AT)
        assert second["valid_bars"] == 3
        assert second["gap_segments"] == 1
        assert second["missing_bars"] == first["missing_bars"] - 1
    finally:
        store.close()


def test_gap_audit_flags_off_grid_candles_and_fake_observation_timing(tmp_path):
    store = SQLiteHotStore(tmp_path / "offgrid.sqlite")
    market = PerpetualMarketStore(store)
    begin = datetime(2026, 4, 28, 12, tzinfo=UTC)
    market.initialize()
    try:
        with store.connect() as conn:
            for stamp in (begin + timedelta(seconds=60), begin + timedelta(minutes=5)):
                conn.execute(
                    """INSERT INTO perp_market_candles
                       (venue,contract,quote,interval_minutes,opened_at,closed_at,
                        first_observed_at,open_price,high_price,low_price,close_price,
                        volume_base,trades) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("gate", "BTC_USDT", "USDT", 5, stamp.isoformat(),
                     (stamp + timedelta(minutes=5, milliseconds=-1)).isoformat(),
                     (stamp + timedelta(minutes=4)).isoformat(),
                     100, 110, 95, 102, 1, -1),
                )
        report = SixMonthQualityAudit(market).audit_stream(
            venue="gate", asset="BTC", now=AT,
        )
        assert report["off_grid_bars"] == 1
        assert report["invalid_observations"] == 1
        assert report["status"] == "gaps_or_anomalies"
    finally:
        store.close()


def _gate_response(request: httpx.Request) -> httpx.Response:
    assert "authorization" not in request.headers
    if "contracts/" in request.url.path:
        return httpx.Response(200, json={
            "name": request.url.path.rsplit("/", 1)[-1],
            "type": "direct", "status": "trading",
            "in_delisting": False, "quanto_multiplier": "0.01",
        })
    assert request.url.path.endswith("/candlesticks")
    assert request.url.params["interval"] == "5m"
    assert "limit" not in request.url.params
    start = int(request.url.params["from"])
    finish = int(request.url.params["to"])
    assert finish - start == 11 * 300
    return httpx.Response(200, json=[
        {"t": start + i * 300, "o": "100", "h": "110", "l": "95",
         "c": "102", "v": "200"}
        for i in range(12)
    ])


@pytest.mark.asyncio
async def test_backfill_observes_real_fetch_time_and_persists_cursor(tmp_path):
    store = SQLiteHotStore(tmp_path / "recover.sqlite")
    market = PerpetualMarketStore(store)
    raw = httpx.AsyncClient(transport=httpx.MockTransport(_gate_response))
    system = GateHistoryRecovery(market, assets=("BTC",), page_bars=12, client=raw)
    try:
        assert await system.run_once(now=AT) == 12
        state = system.progress()[0]
        assert state["inserted_bars"] == 12
        assert state["status"] == "progress"
        saved = market.recent_candles(
            venue="gate", contract="BTC_USDT", quote="USDT",
            interval_minutes=5, count=12,
        )
        assert len(saved) == 12
        assert saved[-1]["volume_base"] == 2
        # The historical opening is April; first-observed is October.
        assert datetime.fromisoformat(saved[0]["first_observed_at"]).month == 10
        assert market.recent_candles(
            venue="gate", contract="BTC_USDT", quote="USDT", interval_minutes=5,
            count=100, as_of=datetime(2026, 4, 29, tzinfo=UTC),
        ) == []
        # A brand-new worker resumes the persisted cursor instead of repeating.
        restarted = GateHistoryRecovery(market, assets=("BTC",), page_bars=12, client=raw)
        assert await restarted.run_once(now=AT) == 12
        assert restarted.progress()[0]["inserted_bars"] == 24
    finally:
        await raw.aclose()
        store.close()


@pytest.mark.asyncio
async def test_backfill_http_451_pauses_without_fake_rows(tmp_path):
    store = SQLiteHotStore(tmp_path / "blocked.sqlite")
    market = PerpetualMarketStore(store)
    calls = []
    def blocked(request):
        calls.append(request)
        return httpx.Response(451)
    raw = httpx.AsyncClient(transport=httpx.MockTransport(blocked))
    system = GateHistoryRecovery(market, client=raw, assets=("BTC",), page_bars=12)
    try:
        assert await system.run_once(now=AT) == 0
        assert system.progress()[0]["status"] == "blocked"
        assert len(calls) == 1
        assert await system.run_once(now=AT + timedelta(minutes=1)) == 0
        assert len(calls) == 1
    finally:
        await raw.aclose()
        store.close()


@pytest.mark.asyncio
async def test_backfill_refuses_out_of_window_data_without_advancing(tmp_path):
    store = SQLiteHotStore(tmp_path / "invalid.sqlite")
    market = PerpetualMarketStore(store)
    def invalid(request):
        if "contracts/" in request.url.path:
            return _gate_response(request)
        start = int(request.url.params["from"])
        return httpx.Response(200, json=[
            {"t": start - 300, "o": 100, "h": 110, "l": 95,
             "c": 102, "v": 200},
        ])
    raw = httpx.AsyncClient(transport=httpx.MockTransport(invalid))
    system = GateHistoryRecovery(market, assets=("BTC",), page_bars=12, client=raw)
    try:
        assert await system.run_once(now=AT) == 0
        state = system.progress()[0]
        assert state["status"] == "retry"
        assert state["next_opened_at"] == state["started_from"]
        assert state["inserted_bars"] == 0
    finally:
        await raw.aclose()
        store.close()
