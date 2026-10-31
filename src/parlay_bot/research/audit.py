"""Exact calendar-partition continuity and cross-resolution source audit."""
from __future__ import annotations

import argparse
import itertools
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.learning import _as_datetime, _rows


def _next_month(month: str) -> str:
    year, mon = map(int, month.split("-"))
    return f"{year + int(mon == 12):04d}-{mon % 12 + 1:02d}"


def audit_partition(path: Path) -> dict:
    """Full-month coverage, first/last boundaries, and source checksum."""
    meta, rows = _rows(path)
    # File names include '--<digest>'; derive declared period from first
    # candle and require EVERY row to remain inside that month.
    first = _as_datetime(rows[0]["opened_at"])
    month = first.strftime("%Y-%m")
    year, mm = map(int, month.split("-"))
    start = datetime(year, mm, 1, tzinfo=UTC)
    end = datetime.fromisoformat(_next_month(month) + "-01T00:00:00+00:00")
    interval = int(meta["interval_minutes"])
    step = timedelta(minutes=interval)
    expected = int((end - start) / step)
    if not 0 < expected <= 45_000:
        raise HistoryImportError("unsupported historical partition interval")
    stamps = [_as_datetime(r["opened_at"]) for r in rows]
    if any(t < start or t >= end for t in stamps):
        raise HistoryImportError("cross-month history partition")
    if any(b <= a or (b - a) % step for a, b in itertools.pairwise(stamps)):
        raise HistoryImportError("duplicate, unordered or misaligned partition")
    if len(rows) > expected:
        raise HistoryImportError("too many candles for complete month")
    missing = expected - len(rows)
    gaps = 0
    longest = 0
    previous = start - step
    for opened in stamps + [end]:
        gap = int((opened - previous) / step) - 1
        if gap < 0:
            raise HistoryImportError("invalid month continuity")
        longest = max(longest, gap)
        gaps += gap
        previous = opened
    if gaps != missing:
        raise HistoryImportError("history gap-count disagreement")
    return {
        "venue": meta["venue"], "contract": meta["contract"],
        "quote": meta["quote"], "interval_minutes": interval, "month": month,
        "source_sha256": meta["source_sha256"],
        "data_sha256": meta["data_sha256"],
        "rows": len(rows), "expected": expected,
        "missing": missing, "longest_missing_segment": longest,
        "coverage": round(len(rows) / expected, 8),
        "full_calendar_month_verified": missing == 0,
        "research_grade": missing / expected <= 0.005 and longest <= 12,
    }


def audit_catalog(
    root: Path, *, months: list[str],
    sources: tuple[str, ...] = ("binance_um_klines",),
) -> dict:
    if not months or len(months) != len(set(months)):
        raise ValueError("unique expected calendar months required")
    if any(_next_month(a) != b for a, b in itertools.pairwise(months)):
        raise ValueError("requested months are not contiguous")
    if not sources or len(sources) != len(set(sources)) or any(
        source not in {"binance_um_klines", "verified_venue_perp_klines"}
        for source in sources
    ):
        raise ValueError("invalid requested research source directories")
    items = []
    failures = []
    paths = [
        path for source in sources
        for path in sorted((root / source).glob("*.manifest.json"))
    ]
    for path in paths:
        try:
            item = audit_partition(path)
            if item["month"] in months:
                items.append(item)
        except (HistoryImportError, OSError, KeyError, ValueError, TypeError) as exc:
            failures.append({"file": path.name, "error": str(exc)})
    grouped: dict[tuple[str, int], dict[str, dict]] = {}
    for item in items:
        key = (item["venue"], item["contract"], item["quote"],
               item["interval_minutes"])
        slot = grouped.setdefault(key, {})
        if item["month"] in slot:
            failures.append({"file": str(key), "error": "duplicate monthly partition"})
        slot[item["month"]] = item
    streams = []
    for (venue, contract, quote, interval), segments in sorted(grouped.items()):
        missing_months = [m for m in months if m not in segments]
        expected = sum(segment["expected"] for segment in segments.values())
        saved = sum(segment["rows"] for segment in segments.values())
        quality = not missing_months and all(
            item["research_grade"] for item in segments.values()
        )
        streams.append({
            "venue": venue, "contract": contract, "quote": quote,
            "interval_minutes": interval,
            "months_present": len(segments), "months_requested": len(months),
            "missing_months": missing_months,
            "verified_rows": saved, "expected_for_present_months": expected,
            "research_grade": quality,
        })
    return {
        "schema_version": "rival-history-continuity-v1",
        "months": months, "partitions": items,
        "streams": streams, "validation_failures": failures,
        "all_examined_streams_qualified": (
            bool(streams) and not failures and all(s["research_grade"] for s in streams)
        ),
        "live_accuracy_changed": False,
    }


def main() -> int:
    from parlay_bot.research.batch import complete_months

    parser = argparse.ArgumentParser(description="Exact source continuity audit")
    parser.add_argument("--root", type=Path, default=Path("data/research-history"))
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    audit = audit_catalog(
        args.root, months=complete_months(now=datetime.now(UTC), count=args.months),
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(audit, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"streams": len(audit["streams"]),
                      "qualified": audit["all_examined_streams_qualified"]}))
    return 0 if audit["all_examined_streams_qualified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
