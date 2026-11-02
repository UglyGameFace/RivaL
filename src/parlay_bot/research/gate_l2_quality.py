"""Source-exact historical Gate L2 snapshot quality without trading claims.

Validated against an original BTC_USDT hourly Gate orderbooks_slice .gz
(2023-04-25 05 UTC; SHA a927b654025a97ea...). An intact gzip alone does
not imply the source contains valid bid and ask quotes or complete coverage.
"""
from __future__ import annotations

import gzip
import io
import json
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from parlay_bot.research.free_history import HistoryImportError

MAX_SNAPSHOTS = 200_000
MAX_LEVELS = 1000
MAX_LINE = 2_000_000
MAX_EXPANDED = 96 * 1024 * 1024


def _number(value: object) -> Decimal:
    if (isinstance(value, bool)
        or not isinstance(value, (str, int, float, Decimal))):
        raise HistoryImportError("invalid Gate historical L2 price or size")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise HistoryImportError("invalid Gate depth number") from exc
    if not result.is_finite() or result <= 0:
        raise HistoryImportError("nonpositive/nonfinite Gate depth")
    return result


def _timestamp(value: object, *, start: float, end: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise HistoryImportError("Gate snapshot source timestamp invalid")
    stamp = float(value)
    if not math.isfinite(stamp) or not start <= stamp < end:
        raise HistoryImportError("Gate source snapshot not in its claimed UTC hour")
    return stamp


def _side(value: object, *, reverse: bool) -> tuple[Decimal, int]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_LEVELS:
        raise HistoryImportError("Gate historical snapshot missing depth")
    previous = None
    best = None
    for row in value:
        if not isinstance(row, dict) or not {"p", "s"} <= row.keys():
            raise HistoryImportError("Gate historical snapshot level schema mismatch")
        px, size = _number(row["p"]), _number(row["s"])
        if previous is not None and (
            (reverse and px >= previous) or (not reverse and px <= previous)
        ):
            raise HistoryImportError("historical L2 prices duplicated or unordered")
        if best is None:
            best = px
        previous = px
    if best is None:
        raise HistoryImportError("unpopulated historical book")
    return best, len(value)


def inspect_snapshot_hour(raw_gzip: bytes, *, hour: str) -> dict:
    """Verify all observed source snapshots, not unobserved calendar ticks."""
    if not isinstance(hour, str) or len(hour) != 10 or not hour.isascii() or not hour.isdecimal():
        raise HistoryImportError("invalid Gate source hour")
    try:
        start = datetime.strptime(hour, "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError as exc:
        raise HistoryImportError("invalid Gate snapshot UTC hour") from exc
    end = start + timedelta(hours=1)
    low, high = start.timestamp(), end.timestamp()
    expanded = count = gaps_gt_five = 0
    first = last = last_id = max_gap = None
    min_bid_levels = min_ask_levels = MAX_LEVELS
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw_gzip)) as source:
            for line in source:
                expanded += len(line)
                count += 1
                if expanded > MAX_EXPANDED or count > MAX_SNAPSHOTS or len(line) > MAX_LINE:
                    raise HistoryImportError("Gate snapshot history exceeds parse budget")
                try:
                    record = json.loads(line)
                except (UnicodeError, ValueError) as exc:
                    raise HistoryImportError("invalid original Gate snapshot JSONL") from exc
                if not isinstance(record, dict):
                    raise HistoryImportError("Gate snapshot must be an object")
                now = _timestamp(record.get("current"), start=low, end=high)
                updated = _timestamp(record.get("update"), start=low, end=high)
                if updated > now:
                    raise HistoryImportError("Gate update timestamp is after observed snapshot")
                sid = record.get("id")
                if isinstance(sid, bool) or not isinstance(sid, int) or sid < 0:
                    raise HistoryImportError("invalid Gate source snapshot identifier")
                bid, bids = _side(record.get("bids"), reverse=True)
                ask, asks = _side(record.get("asks"), reverse=False)
                if bid >= ask:
                    raise HistoryImportError("crossed Gate historical order book")
                min_bid_levels = min(min_bid_levels, bids)
                min_ask_levels = min(min_ask_levels, asks)
                if last is not None:
                    delta = now - last
                    if delta <= 0 or sid <= last_id:
                        raise HistoryImportError("duplicate/retrograde Gate source snapshot")
                    max_gap = max(max_gap or 0., delta)
                    gaps_gt_five += int(delta > 5)
                else:
                    first = now
                last = now
                last_id = sid
    except (OSError, EOFError) as exc:
        raise HistoryImportError("invalid historical Gate snapshot gzip") from exc
    if not count or first is None or last is None:
        raise HistoryImportError("empty Gate historical snapshots")
    return {
        "source_jsonl_snapshot_schema_verified": True,
        "observed_snapshots": count,
        "first_source_ts_seconds": first,
        "last_source_ts_seconds": last,
        "first_source_offset_seconds": round(first - low, 4),
        "last_source_offset_seconds": round(last - low, 4),
        "max_observed_snapshot_gap_seconds": round(max_gap or 0., 4),
        "gaps_over_five_seconds": gaps_gt_five,
        "minimum_bid_depth_levels": min_bid_levels,
        "minimum_ask_depth_levels": min_ask_levels,
        "noncrossed_books_verified": True,
        "strict_source_time_and_id_increase": True,
        "snapshot_interpolation_performed": False,
        "full_hourly_source_continuity_verified": False,
        "full_month_source_continuity_verified": False,
        "genuine_future_quote_coverage_verified": False,
        "model_training_eligible": False,
    }
