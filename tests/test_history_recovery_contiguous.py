"""Contiguous backfill cursors must never skip missing bars.

A recovery page that returns fewer bars than its window used to jump the
cursor to the window end, permanently gapping the stream so the six-month
auditor could never report it complete. The cursor now advances only past
the contiguous prefix (forward pagers) or the top-contiguous run
(backward pagers); genuine gaps are skipped exactly once and stay missing
in the audit, honestly, instead of stalling the cursor forever.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.perpetuals.history_inventory import six_complete_months_start
from parlay_bot.perpetuals.history_recovery import (
    GateHistoryRecovery,
    contiguous_backward_next,
    contiguous_forward_next,
)
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.okx_history_recovery import OkxHistoryRecovery
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
STEP = timedelta(minutes=5)


def test_contiguous_forward_next():
    cursor = datetime(2026, 4, 28, 0, 0, tzinfo=UTC)
    until = cursor + 12 * STEP
    full = [cursor + i * STEP for i in range(12)]
    assert contiguous_forward_next(cursor, until, full) == until
    gapped = [cursor + i * STEP for i in (0, 1, 2, 3, 4, 7, 8, 9, 10, 11)]
    assert contiguous_forward_next(cursor, until, gapped) == cursor + 5 * STEP
    leading_gap = [cursor + i * STEP for i in (3, 4, 5)]
    assert contiguous_forward_next(cursor, until, leading_gap) == cursor + 3 * STEP
    assert contiguous_forward_next(cursor, until, []) == until


def test_contiguous_backward_next():
    cursor = datetime(2026, 10, 28, 8, 0, tzinfo=UTC)
    full = [cursor - i * STEP for i in range(1, 13)]
    assert contiguous_backward_next(cursor, full) == cursor - 12 * STEP
    gapped = [cursor - i * STEP for i in (1, 2, 4, 5, 6)]
    assert contiguous_backward_next(cursor, gapped) == cursor - 2 * STEP
    top_gap = [cursor - i * STEP for i in (4, 5, 6)]
    assert contiguous_backward_next(cursor, top_gap) == cursor - 6 * STEP
    assert contiguous_backward_next(cursor, []) == cursor


def _gate_response_factory(seed):
    truth = {seed + i * STEP for i in range(200) if i not in (5, 6)}

    def handle(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "api.gateio.ws"
        if req.url.path.endswith("/contracts/BTC_USDT"):
            return httpx.Response(200, json={
                "name": "BTC_USDT", "type": "direct", "status": "trading",
                "in_delisting": False, "quanto_multiplier": "1",
            })
        assert req.url.path.endswith("/candlesticks")
        start = int(req.url.params["from"])
        end = int(req.url.params["to"])
        rows = []
        for opened in sorted(truth):
            stamp = int(opened.timestamp())
            if start <= stamp <= end:
                rows.append({
                    "t": stamp, "o": "100", "h": "110", "l": "90",
                    "c": "102", "v": "2",
                })
        return httpx.Response(200, json=rows)

    return handle


@pytest.mark.asyncio
async def test_gate_partial_page_advances_only_past_prefix(tmp_path):
    store = SQLiteHotStore(tmp_path / "gate.sqlite")
    market = PerpetualMarketStore(store)
    seed = six_complete_months_start(NOW)
    raw = httpx.AsyncClient(
        transport=httpx.MockTransport(_gate_response_factory(seed)))
    worker = GateHistoryRecovery(market, assets=("BTC",), page_bars=12, client=raw)
    try:
        assert await worker.run_once(now=NOW) == 10
        first = worker.progress()[0]
        assert first["next_opened_at"] == (seed + 5 * STEP).isoformat()
        assert first["status"] == "progress"
        # The gap bars are skipped exactly once, not stalled on.
        assert await worker.run_once(now=NOW + timedelta(minutes=5)) == 5
        second = worker.progress()[0]
        assert second["next_opened_at"] == (seed + 7 * STEP).isoformat()
        assert second["inserted_bars"] == 15
    finally:
        await worker.aclose()


def _okx_response(req: httpx.Request) -> httpx.Response:
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
    anchor = int(req.url.params["after"])
    offsets = [1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12]
    return httpx.Response(200, json={"code": "0", "data": [
        [str(anchor - 300_000 * i), "100", "110", "90", "102",
         "200", "2", "204", "1"]
        for i in offsets
    ]})


@pytest.mark.asyncio
async def test_okx_partial_page_advances_only_past_top_run(tmp_path):
    store = SQLiteHotStore(tmp_path / "okx.sqlite")
    market = PerpetualMarketStore(store)
    raw = httpx.AsyncClient(transport=httpx.MockTransport(_okx_response))
    worker = OkxHistoryRecovery(market, client=raw, assets=("BTC",), page_bars=12)
    try:
        assert await worker.run_once(now=NOW) == 11
        progress = worker.progress()[0]
        anchor = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=4)
        assert progress["next_opened_at"] == (anchor - 2 * STEP).isoformat()
        assert progress["status"] == "progress"
    finally:
        await worker.aclose()


def _gate_empty_response(req: httpx.Request) -> httpx.Response:
    assert req.url.host == "api.gateio.ws"
    if req.url.path.endswith("/contracts/BTC_USDT"):
        return httpx.Response(200, json={
            "name": "BTC_USDT", "type": "direct", "status": "trading",
            "in_delisting": False, "quanto_multiplier": "1",
        })
    assert req.url.path.endswith("/candlesticks")
    return httpx.Response(200, json=[])


@pytest.mark.asyncio
async def test_gate_empty_page_does_not_advance_cursor(tmp_path):
    store = SQLiteHotStore(tmp_path / "gate-empty.sqlite")
    market = PerpetualMarketStore(store)
    seed = six_complete_months_start(NOW)
    raw = httpx.AsyncClient(transport=httpx.MockTransport(_gate_empty_response))
    worker = GateHistoryRecovery(market, assets=("BTC",), page_bars=12, client=raw)
    try:
        assert await worker.run_once(now=NOW) == 0
        row = worker.progress()[0]
        assert row["next_opened_at"] == seed.isoformat()
        assert row["status"] == "upstream_empty"
        assert row["retry_after"] == (NOW + timedelta(hours=6)).isoformat()
        assert row["empty_windows"] == 1
        assert row["inserted_bars"] == 0
        # The 6h cooldown holds: no refetch, no cursor move.
        assert await worker.run_once(now=NOW + timedelta(minutes=5)) == 0
        assert worker.progress()[0]["next_opened_at"] == seed.isoformat()
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_gate_vacuous_caught_up_row_resets_to_seed(tmp_path):
    store = SQLiteHotStore(tmp_path / "gate-vacuous.sqlite")
    market = PerpetualMarketStore(store)
    seed = six_complete_months_start(NOW)
    seeder = GateHistoryRecovery(
        market, assets=("BTC",), page_bars=12,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(_gate_response_factory(seed))),
    )
    seeder.initialize()
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_history_recovery
               (venue,asset,started_from,next_opened_at,
                inserted_bars,empty_windows,status)
               VALUES ('gate','BTC',?,?,0,3,'caught_up')""",
            (seed.isoformat(), NOW.isoformat()),
        )
    raw = seeder._client
    worker = GateHistoryRecovery(market, assets=("BTC",), page_bars=12, client=raw)
    try:
        assert await worker.run_once(now=NOW) == 10
        row = worker.progress()[0]
        assert row["next_opened_at"] == (seed + 5 * STEP).isoformat()
        assert row["status"] == "progress"
    finally:
        await raw.aclose()


@pytest.mark.asyncio
async def test_gate_legitimate_caught_up_row_is_not_reset(tmp_path):
    store = SQLiteHotStore(tmp_path / "gate-legit.sqlite")
    market = PerpetualMarketStore(store)
    seed = six_complete_months_start(NOW)
    def _no_calls(req: httpx.Request) -> httpx.Response:
        raise AssertionError("legitimately caught-up row must not fetch")

    raw = httpx.AsyncClient(transport=httpx.MockTransport(_no_calls))
    seeder = GateHistoryRecovery(market, assets=("BTC",), page_bars=12, client=raw)
    seeder.initialize()
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_history_recovery
               (venue,asset,started_from,next_opened_at,
                inserted_bars,empty_windows,status)
               VALUES ('gate','BTC',?,?,100,0,'caught_up')""",
            (seed.isoformat(), NOW.isoformat()),
        )
    worker = GateHistoryRecovery(market, assets=("BTC",), page_bars=12, client=raw)
    try:
        assert await worker.run_once(now=NOW) == 0
        row = worker.progress()[0]
        assert row["next_opened_at"] == NOW.isoformat()
        assert row["status"] == "caught_up"
        assert row["inserted_bars"] == 100
    finally:
        await raw.aclose()
