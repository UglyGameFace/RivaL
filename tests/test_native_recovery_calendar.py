"""Six *completed* UTC months, including upgraded persisted recovery cursors.

Live learning never retroactively observes a recovered bar: only the research
history cursor is adjusted here, not stored OHLCV, first_observed_at, issued
forecasts or model promotion.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.bitget_history_recovery import BitgetHistoryRecovery
from parlay_bot.perpetuals.deribit_history_recovery import DeribitHistoryRecovery
from parlay_bot.perpetuals.dydx_history_recovery import DydxHistoryRecovery
from parlay_bot.perpetuals.history_inventory import (
    six_complete_months_start,
    six_months_prior,
)
from parlay_bot.perpetuals.history_recovery import GateHistoryRecovery
from parlay_bot.perpetuals.hyperliquid_history_recovery import HyperliquidHistoryRecovery
from parlay_bot.perpetuals.kucoin_history_recovery import KucoinHistoryRecovery
from parlay_bot.perpetuals.lighter_history_recovery import LighterHistoryRecovery
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.okx_history_recovery import OkxHistoryRecovery
from parlay_bot.perpetuals.phemex_history_recovery import PhemexHistoryRecovery
from parlay_bot.storage.hot import SQLiteHotStore

AT = datetime(2026, 10, 31, 12, tzinfo=UTC)
APRIL = datetime(2026, 4, 1, tzinfo=UTC)
MAY = datetime(2026, 5, 1, tzinfo=UTC)
VENUES = (
    ("gate", GateHistoryRecovery),
    ("hyperliquid", HyperliquidHistoryRecovery),
    ("dydx", DydxHistoryRecovery),
    ("bitget", BitgetHistoryRecovery),
    ("kucoin", KucoinHistoryRecovery),
    ("deribit", DeribitHistoryRecovery),
    ("lighter", LighterHistoryRecovery),
    ("phemex", PhemexHistoryRecovery),
)


def test_completed_month_boundary_preserves_separate_rolling_window():
    assert six_months_prior(AT) == datetime(2026, 4, 30, 12, tzinfo=UTC)
    assert six_complete_months_start(AT) == APRIL
    assert six_complete_months_start(
        datetime(2026, 11, 1, 0, 1, tzinfo=UTC)
    ) == MAY
    assert six_complete_months_start(
        datetime(2027, 1, 31, tzinfo=UTC)
    ) == datetime(2026, 7, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        six_complete_months_start(datetime(2026, 10, 31))


@pytest.mark.parametrize(("venue", "worker_type"), VENUES)
def test_new_forward_venue_starts_at_first_complete_month(tmp_path, venue, worker_type):
    store = SQLiteHotStore(tmp_path / f"new-{venue}.sqlite")
    market = PerpetualMarketStore(store)
    worker = worker_type(market, assets=("BTC",), page_bars=12)
    try:
        pos = worker._position("BTC", AT)
        assert pos["started_from"] == APRIL.isoformat()
        assert pos["next_opened_at"] == APRIL.isoformat()
    finally:
        store.close()


@pytest.mark.parametrize(("venue", "worker_type"), VENUES)
def test_legacy_caught_up_cursor_replays_missing_month_safely(tmp_path, venue, worker_type):
    store = SQLiteHotStore(tmp_path / f"legacy-{venue}.sqlite")
    market = PerpetualMarketStore(store)
    worker = worker_type(market, assets=("BTC",), page_bars=12)
    worker.initialize()
    legacy = datetime(2026, 4, 30, 12, tzinfo=UTC).isoformat()
    last = datetime(2026, 10, 30, tzinfo=UTC).isoformat()
    retry = (AT + timedelta(days=1)).isoformat()
    try:
        with store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_history_recovery
                   (venue,asset,started_from,next_opened_at,
                    inserted_bars,empty_windows,status,retry_after)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (venue, "BTC", legacy, last, 2021, 1, "caught_up", retry),
            )
            # Unrelated asset and venue must never be touched by the migration.
            conn.execute(
                """INSERT INTO perp_history_recovery
                   (venue,asset,started_from,next_opened_at,status)
                   VALUES (?,?,?,?,?)""",
                (venue, "ETH", legacy, last, "progress"),
            )
        fixed = worker._position("BTC", AT)
        assert fixed["started_from"] == APRIL.isoformat()
        assert fixed["next_opened_at"] == APRIL.isoformat()
        assert fixed["status"] == "progress"
        assert fixed["retry_after"] is None
        assert fixed["inserted_bars"] == 2021
        assert fixed["empty_windows"] == 1
        # Second run cannot keep rewinding and undoing the real backfill.
        with store.connect() as conn:
            next_page = (APRIL + timedelta(hours=2)).isoformat()
            conn.execute(
                """UPDATE perp_history_recovery SET next_opened_at=?
                   WHERE venue=? AND asset=?""",
                (next_page, venue, "BTC"),
            )
        same = worker._position("BTC", AT)
        assert same["next_opened_at"] == next_page
        with store.connect() as conn:
            other = conn.execute(
                "SELECT * FROM perp_history_recovery WHERE venue=? AND asset='ETH'",
                (venue,),
            ).fetchone()
            assert other["started_from"] == legacy
            assert other["next_opened_at"] == last
    finally:
        store.close()


@pytest.mark.parametrize(("venue", "worker_type"), VENUES)
def test_preexisting_earlier_complete_floor_is_never_rewound(tmp_path, venue, worker_type):
    store = SQLiteHotStore(tmp_path / f"older-{venue}.sqlite")
    market = PerpetualMarketStore(store)
    worker = worker_type(market, assets=("BTC",), page_bars=12)
    worker.initialize()
    original = datetime(2026, 3, 1, tzinfo=UTC).isoformat()
    cursor = datetime(2026, 9, 1, tzinfo=UTC).isoformat()
    try:
        with store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_history_recovery
                   (venue,asset,started_from,next_opened_at,status)
                   VALUES (?,?,?,?,?)""",
                (venue, "BTC", original, cursor, "progress"),
            )
        saved = worker._position("BTC", AT)
        assert saved["started_from"] == original
        assert saved["next_opened_at"] == cursor
    finally:
        store.close()


@pytest.mark.asyncio
async def test_okx_finished_rolling_lookback_must_keep_paging_to_month_start(tmp_path):
    store = SQLiteHotStore(tmp_path / "okx.sqlite")
    market = PerpetualMarketStore(store)
    worker = OkxHistoryRecovery(market, assets=("BTC",), page_bars=12)
    worker.initialize()
    observed = []
    async def multiplier(asset, contract, now):
        return 1.0

    async def public(path, params):
        observed.append((path, params))
        return {"code": "0", "data": []}
    worker._multiplier = multiplier
    worker._public = public
    try:
        old_boundary = datetime(2026, 4, 30, 12, tzinfo=UTC).isoformat()
        with store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_history_recovery
                   (venue,asset,started_from,next_opened_at,status)
                   VALUES (?,?,?,?,?)""",
                ("okx", "BTC", AT.isoformat(), old_boundary, "caught_up"),
            )
        assert await worker.fetch_asset("BTC", now=AT) == 0
        assert observed and observed[0][0] == "market/history-candles"
    finally:
        await worker.aclose()
        store.close()
