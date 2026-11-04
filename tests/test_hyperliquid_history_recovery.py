"""Hyperliquid six-month historical recovery: real units, durable cursors.

Hyperliquid is the primary signal venue but previously had no backfill
path: its history only grew forward from first collection, so the
six-month auditor could never certify it. The recovery seeds six months
back and pages forward through candleSnapshot, storing bars with the
exact venue/contract/quote the auditor queries.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.perpetuals.history_inventory import six_complete_months_start
from parlay_bot.perpetuals.hyperliquid_history_recovery import (
    HyperliquidHistoryRecovery,
)
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
STEP = timedelta(minutes=5)


def response(req: httpx.Request) -> httpx.Response:
    assert req.url.host == "api.hyperliquid.xyz"
    assert "authorization" not in req.headers
    assert req.url.path == "/info"
    payload = json.loads(req.content)
    assert payload["type"] == "candleSnapshot"
    assert payload["req"]["coin"] == "BTC"
    assert payload["req"]["interval"] == "5m"
    start = payload["req"]["startTime"]
    end = payload["req"]["endTime"]
    assert start % 300_000 == 0 and end % 300_000 == 0
    assert end - start == 12 * 300_000
    return httpx.Response(200, json=[
        {
            "s": "BTC", "i": "5m", "t": start + i * 300_000,
            "T": start + i * 300_000 + 299_999,
            "o": "100", "h": "110", "l": "90", "c": "102",
            "v": "2.5", "n": 7,
        }
        for i in range(12)
    ])


@pytest.mark.asyncio
async def test_hyperliquid_six_month_history_recovers_restart_safe(tmp_path):
    store = SQLiteHotStore(tmp_path / "hl.sqlite")
    market = PerpetualMarketStore(store)
    raw = httpx.AsyncClient(transport=httpx.MockTransport(response))
    worker = HyperliquidHistoryRecovery(
        market, assets=("BTC",), page_bars=12, client=raw,
    )
    try:
        assert await worker.run_once(now=NOW) == 12
        first = worker.progress()[0]
        assert first["venue"] == "hyperliquid"
        assert first["status"] == "progress"
        assert first["inserted_bars"] == 12
        seed = six_complete_months_start(NOW).replace(
            minute=six_complete_months_start(NOW).minute // 5 * 5,
            second=0, microsecond=0)
        assert first["next_opened_at"] == (seed + 12 * STEP).isoformat()
        assert await worker.run_once(now=NOW + timedelta(minutes=5)) == 12
        rows = market.recent_candles(
            venue="hyperliquid", contract="BTC", quote="USDC",
            interval_minutes=5, count=24,
        )
        assert len(rows) == 24
        # Historical candles imported in October do NOT appear in an old
        # point-in-time May 2026 training set.
        assert market.recent_candles(
            venue="hyperliquid", contract="BTC", quote="USDC",
            interval_minutes=5, as_of=datetime(2026, 5, 1, tzinfo=UTC),
        ) == []
        restarted = HyperliquidHistoryRecovery(
            market, assets=("BTC",), page_bars=12, client=raw,
        )
        try:
            assert restarted.progress()[0]["inserted_bars"] == 24
            assert await restarted.run_once(now=NOW + timedelta(minutes=10)) == 12
        finally:
            await restarted.aclose()
    finally:
        await worker.aclose()


def empty_response(req: httpx.Request) -> httpx.Response:
    assert req.url.host == "api.hyperliquid.xyz"
    return httpx.Response(200, json=[])


@pytest.mark.asyncio
async def test_hyperliquid_empty_page_does_not_advance_cursor(tmp_path):
    store = SQLiteHotStore(tmp_path / "hl-empty.sqlite")
    market = PerpetualMarketStore(store)
    raw = httpx.AsyncClient(transport=httpx.MockTransport(empty_response))
    worker = HyperliquidHistoryRecovery(
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
async def test_old_hyperliquid_pages_use_verified_recent_window_without_false_completion(tmp_path):
    """Official 5,000-candle retention must not stall every six-month cursor."""
    threshold = NOW - 5000 * STEP
    seen = []

    def capped(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        start_ms = payload["req"]["startTime"]
        start = datetime.fromtimestamp(start_ms / 1000, UTC)
        seen.append(start)
        if start < threshold:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[
            {"s": "XRP", "i": "5m", "t": start_ms + i * 300_000,
             "o": "1", "h": "2", "l": "1", "c": "1.5",
             "v": "100", "n": 10}
            for i in range(12)
        ])

    store = SQLiteHotStore(tmp_path / "hl-retention.sqlite")
    market = PerpetualMarketStore(store)
    raw = httpx.AsyncClient(transport=httpx.MockTransport(capped))
    worker = HyperliquidHistoryRecovery(
        market, assets=("XRP",), page_bars=12, client=raw,
    )
    try:
        assert await worker.run_once(now=NOW) == 0
        row = worker.progress()[0]
        assert row["status"] == "api_retention_limited"
        assert row["retention_reason"] == "candleSnapshot_latest_5000"
        assert row["retention_requested_from"] == row["started_from"]
        assert row["retention_resumed_at"] == row["next_opened_at"]
        assert datetime.fromisoformat(row["next_opened_at"]) > threshold
        # The skipped months have no fabricated candle records.
        assert market.recent_candles(
            venue="hyperliquid", contract="XRP", quote="USDC",
            interval_minutes=5,
        ) == []
        assert await worker.run_once(now=NOW + timedelta(minutes=5)) == 12
        after = worker.progress()[0]
        assert after["inserted_bars"] == 12
        assert after["retention_reason"] == "candleSnapshot_latest_5000"
        assert len(seen) >= 3
    finally:
        await worker.aclose()
        await raw.aclose()
        store.close()
