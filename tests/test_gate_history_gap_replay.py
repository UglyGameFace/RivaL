"""Skipped historical 5-minute candles are durable, bounded, retryable gaps.

Never rewrite immutable observed bars, restart the full backfill or label
historical recovery as live prospective evidence.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parlay_bot.perpetuals.history_recovery import GateHistoryRecovery
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)
STEP = timedelta(minutes=5)


def _bar(stamp: int) -> dict:
    return {
        "t": stamp, "o": "100", "h": "110", "l": "90",
        "c": "102", "v": "2",
    }


@pytest.mark.asyncio
async def test_gap_queue_replays_leading_skipped_slots_without_rewinding(tmp_path):
    start = NOW - timedelta(days=2)
    request_starts: list[int] = []
    def respond(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/contracts/BTC_USDT"):
            return httpx.Response(200, json={
                "name": "BTC_USDT", "type": "direct", "status": "trading",
                "in_delisting": False, "quanto_multiplier": "1",
            })
        assert req.url.path.endswith("/candlesticks")
        first = int(req.url.params["from"])
        last = int(req.url.params["to"])
        request_starts.append(first)
        stamps = range(first, last + 1, 300)
        # The first broad page has two missing bars at its leading edge.
        if last > first and first == int(start.timestamp()):
            stamps = list(stamps)[2:]
        return httpx.Response(200, json=[_bar(stamp) for stamp in stamps])

    store = SQLiteHotStore(tmp_path / "gaps.sqlite")
    market = PerpetualMarketStore(store)
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    worker = GateHistoryRecovery(
        market, assets=("BTC",), page_bars=12, client=client,
    )
    try:
        worker._position("BTC", NOW)
        worker._advance("BTC", now=NOW, next_open=start)
        assert await worker.fetch_asset("BTC", now=NOW) == 10
        initial = worker.progress()[0]
        assert initial["next_opened_at"] == (start + 2 * STEP).isoformat()
        assert initial["pending_gap_bars"] == 2

        # Simulate continuing uninterrupted until the forward cursor
        # reaches the current provider history cutoff. Replays must not
        # reset/undo that progress.
        future = NOW + timedelta(hours=7)
        end = future - timedelta(hours=4)
        worker._advance("BTC", now=NOW, next_open=end)
        assert await worker.fetch_asset("BTC", now=future) == 1
        assert worker.progress()[0]["pending_gap_bars"] == 1
        assert await worker.fetch_asset("BTC", now=future + timedelta(minutes=1)) == 1
        complete = worker.progress()[0]
        assert complete["pending_gap_bars"] == 0
        assert complete["next_opened_at"] == end.isoformat()
        assert complete["inserted_bars"] == 12
        assert request_starts[-2:] == [
            int(start.timestamp()), int((start + STEP).timestamp()),
        ]
        assert await worker.fetch_asset(
            "BTC", now=future + timedelta(minutes=2),
        ) == 0
        assert worker.progress()[0]["status"] == "caught_up"
        saved = market.recent_candles(
            venue="gate", contract="BTC_USDT", quote="USDT",
            interval_minutes=5, count=12,
        )
        assert len(saved) == 12
        assert saved[0]["opened_at"] == start.isoformat()
    finally:
        await client.aclose()
        store.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_gap_queue_deduplicates_and_respects_durable_cooldown(tmp_path, backend):
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if backend == "postgres":
        if not dsn:
            pytest.skip("No PostgreSQL test database configured")
        store = PostgresHotStore(dsn)
    else:
        store = SQLiteHotStore(tmp_path / "gaps-restart.sqlite")
    market = PerpetualMarketStore(store)
    worker = GateHistoryRecovery(market, assets=("BTC",))
    start = NOW - timedelta(days=1)
    try:
        worker.initialize()
        with store.connect() as conn:
            conn.execute(
                "DELETE FROM perp_history_gap_repair WHERE venue='gate' AND asset='BTC'",
            )
        worker._record_skipped_gap(
            "BTC", start=start, first=start + 2 * STEP, now=NOW,
        )
        worker._record_skipped_gap(
            "BTC", start=start, first=start + 2 * STEP, now=NOW,
        )
        assert worker._pending_gaps("BTC", now=NOW) == 2
        assert worker._claim_due_gap(
            "BTC", now=NOW + timedelta(hours=5), end=NOW,
        ) is None
        claimed = worker._claim_due_gap(
            "BTC", now=NOW + timedelta(hours=7), end=NOW,
        )
        assert claimed == start
        assert worker._claim_due_gap(
            "BTC", now=NOW + timedelta(hours=7), end=NOW,
        ) == start + STEP
        assert worker._claim_due_gap(
            "BTC", now=NOW + timedelta(hours=7), end=NOW,
        ) is None
        worker._complete_gap("BTC", start)
        assert worker._pending_gaps("BTC", now=NOW) == 1
        assert worker._claim_due_gap(
            "BTC", now=NOW + timedelta(hours=8), end=NOW,
        ) is None
        with pytest.raises(ValueError, match="invalid bounded historical gap"):
            worker._record_skipped_gap(
                "BTC", start=start, first=start + 601 * STEP, now=NOW,
            )
    finally:
        with store.connect() as conn:
            conn.execute(
                "DELETE FROM perp_history_gap_repair WHERE venue='gate' AND asset='BTC'",
            )
        awaitable = worker.aclose()
        # HTTP clients are not used; close via normal asyncio runner in this sync test.
        import asyncio
        asyncio.run(awaitable)
        store.close()
