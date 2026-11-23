"""The six-month audit's keyset page reads must have their covering index.

_scan_rows pages one candle stream in opened_at order reading exactly
(opened_at, closed_at, first_observed_at) behind the stream key. The
covering index is what lets each page be an index-only scan that stops
at the page limit instead of bitmap-scanning the whole remaining
window from the heap (44 very-slow operations in one window, 2026-11-23).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.history_quality import SixMonthQualityAudit
from parlay_bot.perpetuals.market_store import PerpetualMarketStore


def _index_exists(store, table: str, name: str) -> bool:
    with store.connect() as conn:
        if store.database.backend == "postgres":
            row = conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename=? AND indexname=?",
                (table, name),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? AND name=?",
                (table, name),
            ).fetchone()
    return row is not None


def test_audit_covering_index_declared(learning_store):
    PerpetualMarketStore(learning_store).initialize()
    assert _index_exists(
        learning_store, "perp_market_candles", "idx_perp_market_candles_audit")


def test_scan_rows_stream_order_unchanged(learning_store):
    market = PerpetualMarketStore(learning_store)
    market.initialize()
    start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    with learning_store.connect() as conn:
        for i in (2, 0, 1):  # inserted out of order on purpose
            opened = start + timedelta(minutes=5 * i)
            conn.execute(
                "INSERT INTO perp_market_candles "
                "(venue,contract,quote,interval_minutes,opened_at,closed_at,"
                "first_observed_at,open_price,high_price,low_price,close_price,"
                "volume_base,trades) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("gate", "BTC_USDT", "USDT", 5, opened.isoformat(),
                 (opened + timedelta(minutes=5, milliseconds=-1)).isoformat(),
                 (opened + timedelta(minutes=5)).isoformat(),
                 100, 110, 95, 102, 1.5, 3),
            )
    rows = list(SixMonthQualityAudit._scan_rows(
        learning_store, venue="gate", contract="BTC_USDT", quote="USDT",
        start=start.isoformat(),
        end=(start + timedelta(minutes=15)).isoformat(),
    ))
    assert [r["opened_at"] for r in rows] == [
        (start + timedelta(minutes=5 * i)).isoformat() for i in (0, 1, 2)]
