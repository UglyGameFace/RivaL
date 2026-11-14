"""Verified partial candle partitions from the existing daily ledger backups.

These are retrospective ledger records, never provider originals, complete
calendar months, or prospective forecasts. No live database or Drive writes.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import itertools
import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from parlay_bot.research.free_history import HistoryImportError, _persist
from parlay_bot.research.source_native import SOURCE, STEP

INTEGRITY = "drive-daily-records-first-observed-at-v1"
MAX_EXPANDED_BYTES = 256 * 1024 * 1024


def _clock(value) -> datetime:
    stamp = datetime.fromisoformat(str(value))
    if stamp.utcoffset() is None:
        raise HistoryImportError("recorded candle clock is not timezone-aware")
    return stamp.astimezone(UTC)


def _original_rows(payload: bytes):
    expanded = 0
    with gzip.GzipFile(fileobj=io.BytesIO(payload)) as handle:
        while line := handle.readline(128 * 1024):
            expanded += len(line)
            if expanded > MAX_EXPANDED_BYTES or len(line) >= 128 * 1024:
                raise HistoryImportError("recorded day exceeds expanded byte/row budget")
            row = json.loads(line)
            if not isinstance(row, dict):
                raise HistoryImportError("recorded original row is not an object")
            yield row


def partition_coverage(rows: list[dict], *, month: str, through: datetime) -> dict:
    start = datetime.fromisoformat(month + "-01T00:00:00+00:00")
    end = (start.replace(year=start.year + 1, month=1) if start.month == 12
           else start.replace(month=start.month + 1))
    stop = min(end, through)
    if stop <= start or (stop - start) % STEP:
        raise HistoryImportError("invalid partial-month cutoff")
    stamps = [_clock(row["opened_at"]) for row in rows]
    if not stamps or any(t < start or t >= stop or (t - start) % STEP for t in stamps):
        raise HistoryImportError("recorded candles outside the declared partition/grid")
    if any(b <= a for a, b in itertools.pairwise(stamps)):
        raise HistoryImportError("conflicting or unordered recorded candle keys")
    gaps, longest, previous = 0, 0, start - STEP
    for stamp in [*stamps, stop]:
        missing = int((stamp - previous) / STEP) - 1
        gaps += missing
        longest = max(longest, missing)
        previous = stamp
    return {"month": month, "start": start.isoformat(), "through_exclusive": stop.isoformat(),
            "first_opened_at": stamps[0].isoformat(), "last_opened_at": stamps[-1].isoformat(),
            "rows": len(rows), "expected_elapsed_rows": int((stop - start) / STEP),
            "missing_elapsed_rows": gaps, "longest_missing_segment": longest,
            "internal_missing_rows": int((stamps[-1] - stamps[0]) / STEP) + 1 - len(rows),
            "full_calendar_month_verified": False}


def _normalize(row: dict, *, day: date, now: datetime) -> dict:
    opened, closed, observed = (_clock(row[k]) for k in
                                ("opened_at", "closed_at", "first_observed_at"))
    if (opened.timestamp() % 300 or closed != opened + STEP - timedelta(milliseconds=1)
            or not closed <= observed <= now or observed.date() != day):
        raise HistoryImportError("recorded candle grid/observation clock mismatch")
    prices = [float(row[k]) for k in ("open_price", "high_price", "low_price", "close_price")]
    volume, trades = float(row["volume_base"]), row["trades"]
    if (not all(math.isfinite(p) and p > 0 for p in prices)
            or prices[2] > min(prices[0], prices[3])
            or prices[1] < max(prices[0], prices[3])):
        raise HistoryImportError("invalid recorded candle price geometry")
    if not math.isfinite(volume) or volume < 0:
        raise HistoryImportError("invalid recorded candle volume")
    # PerpetualMarketStore.ingest accepts -1 as the ORIGINAL source's
    # unknown trade-count sentinel. Preserve it verbatim; replacing it with
    # zero would turn missing data into a fabricated observation.
    if type(trades) is not int or trades < -1:
        raise HistoryImportError("invalid recorded candle trade count")
    return {"venue": row["venue"], "contract": row["contract"], "quote": row["quote"],
            "interval_minutes": 5, "opened_at": opened.isoformat(), "closed_at": closed.isoformat(),
            "first_observed_at": observed.isoformat(), "open": prices[0], "high": prices[1],
            "low": prices[2], "close": prices[3], "volume_base": volume, "trades": trades}


def validate_partition(meta: dict, rows: list[dict], folder: Path) -> None:
    """Re-prove retained originals, row identity, clocks and measured gaps."""
    try:
        if (meta.get("provider_signed_checksum") is not False
                or meta.get("full_calendar_month_verified") is not False
                or meta.get("source_original_drive_readback_checked") is not True):
            raise HistoryImportError("invalid partial-month integrity claims")
        receipts = meta["recorded_source_files"]
        if not isinstance(receipts, list) or not 1 <= len(receipts) <= 120:
            raise HistoryImportError("partial partition lacks bounded source receipts")
        days = set()
        derived = {}
        restored_from = date.fromisoformat(meta["restored_from_day"])
        restored_through = date.fromisoformat(meta["restored_through_day"])
        for receipt in receipts:
            name = receipt["file"]
            if (Path(name).name != name or receipt.get("drive_sha256_verified") is not True
                    or name != f"rival-records__perp_market_candles__{receipt['day']}.ndjson.gz"):
                raise HistoryImportError("invalid recorded source identity")
            raw = (folder.parent / "record-originals" / "records" / name).read_bytes()
            if (len(raw) != receipt["size_bytes"]
                    or hashlib.sha256(raw).hexdigest() != receipt["sha256"]
                    or hashlib.md5(raw, usedforsecurity=False).hexdigest() != receipt["md5"]):
                raise HistoryImportError("recorded original source hash mismatch")
            days.add(date.fromisoformat(receipt["day"]))
            for row in _original_rows(raw):
                if (any(row.get(k) != meta[k] for k in ("venue", "contract", "quote"))
                        or row.get("interval_minutes") != 5):
                    continue
                normalized = _normalize(row, day=date.fromisoformat(receipt["day"]),
                                        now=_clock(receipt["day"] + "T23:59:59.999999+00:00"))
                opened = _clock(normalized["opened_at"])
                if (opened.strftime("%Y-%m") != meta["utc_month"]
                        or not restored_from <= opened.date() <= restored_through):
                    continue
                if normalized["opened_at"] in derived and derived[normalized["opened_at"]] != normalized:
                    raise HistoryImportError("conflicting original candle observations")
                derived[normalized["opened_at"]] = normalized
        if rows != [derived[t] for t in sorted(derived)]:
            raise HistoryImportError("partial candle rows differ from verified original bytes")
        # Legacy partials without this field predate the unknown-trade
        # sentinel admission and therefore must contain zero such rows.
        unknown = meta.get("unknown_trade_count", 0)
        if (type(unknown) is not int or unknown < 0
                or unknown != sum(row["trades"] == -1 for row in rows)):
            raise HistoryImportError("recorded unknown trade-count evidence mismatch")
        through = _clock(meta["partition_coverage"]["through_exclusive"])
        observed_through = max(days) + timedelta(days=1)
        for row in rows:
            if any(row[k] != meta[k] for k in ("venue", "contract", "quote", "interval_minutes")):
                raise HistoryImportError("partial-month source identity mismatch")
            observed = _clock(row["first_observed_at"])
            if observed.date() not in days:
                raise HistoryImportError("recorded observation lacks an original day receipt")
            _normalize({**row, **{f"{k}_price": row[k] for k in ("open", "high", "low", "close")}},
                       day=observed.date(), now=datetime.combine(observed_through, datetime.min.time(), UTC))
        coverage = partition_coverage(rows, month=meta["utc_month"], through=through)
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":"),
                                           allow_nan=False).encode()).hexdigest()
        if (coverage != meta["partition_coverage"] or meta["gaps"] != coverage["missing_elapsed_rows"]
                or digest != meta["source_sha256"]):
            raise HistoryImportError("partial-month coverage/source digest mismatch")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise HistoryImportError(f"unverified recorded partial partition: {exc}") from exc


def latest_recorded_snapshots(paths: list[Path]) -> list[Path]:
    """Select an unchanged superset after recovery, retaining older files.

    Only this explicit partial-ledger format may supersede its own snapshot.
    Conflicting corrections and ordinary duplicate monthly sources still fail.
    """
    from parlay_bot.research.learning import _rows

    ordinary, current = [], {}
    for path in paths:
        meta = json.loads(path.read_text())
        if meta.get("source_integrity") != INTEGRITY:
            ordinary.append(path)
            continue
        meta, rows = _rows(path)
        key = (meta["venue"], meta["contract"], meta["quote"], meta["utc_month"])
        indexed = {row["opened_at"]: row for row in rows}
        if key in current:
            _, previous = current[key]
            if all(indexed.get(t) == row for t, row in previous.items()):
                current[key] = path, indexed
            elif not all(previous.get(t) == row for t, row in indexed.items()):
                raise HistoryImportError("conflicting partial recovery snapshots")
        else:
            current[key] = path, indexed
    return sorted([*ordinary, *(path for path, _ in current.values())])


async def restore_recorded_partitions(drive, *, asset: str, root: Path,
                                      first_day: date, last_day: date,
                                      now: datetime) -> dict:
    from parlay_bot.research.audit import audit_partition
    from parlay_bot.research.training_warehouse import TrainingWarehouse

    if (asset not in {"BTC", "ETH", "SOL", "XRP"} or now.utcoffset() is None
            or not 0 <= (last_day - first_day).days < 120 or last_day >= now.date()):
        raise HistoryImportError("invalid recorded candle restoration window")
    # A verified complete monthly source wins. Never duplicate or replace it
    # with a ledger-derived partial partition for the same instrument/month.
    complete = set()
    for path in Path(root).rglob("*.manifest.json"):
        audit = audit_partition(path)
        if audit["full_calendar_month_verified"]:
            complete.add((audit["venue"], audit["contract"], audit["quote"], audit["month"]))
    warehouse = TrainingWarehouse(drive, Path(root) / "record-originals")
    payloads = await warehouse.records("perp_market_candles", max_files=120,
                                      first_day=first_day, last_day=last_day, require_sha256=True)
    receipts = warehouse.record_provenance["perp_market_candles"]
    stop = datetime.combine(last_day + timedelta(days=1), datetime.min.time(), UTC)
    coins = (asset,) if asset == "BTC" else (asset, "BTC")
    scopes = {(venue, contract, "USDT") for coin in coins for venue, contract in
              (("bitget", f"{coin}USDT"), ("okx", f"{coin}-USDT-SWAP"), ("gate", f"{coin}_USDT"))}
    groups, lineage = {}, {}
    for payload, receipt in zip(payloads, receipts, strict=True):
        try:
            for raw in _original_rows(payload):
                identity = (raw.get("venue"), raw.get("contract"), raw.get("quote"))
                if identity not in scopes or raw.get("interval_minutes") != 5:
                    continue
                row = _normalize(raw, day=date.fromisoformat(receipt["day"]), now=now)
                opened = _clock(row["opened_at"])
                scope = (*identity, opened.strftime("%Y-%m"))
                if scope in complete or not first_day <= opened.date() <= last_day:
                    continue
                records = groups.setdefault(scope, {})
                if row["opened_at"] in records and records[row["opened_at"]] != row:
                    raise HistoryImportError("conflicting recorded candle observations")
                records[row["opened_at"]] = row
                lineage.setdefault(scope, {})[receipt["file"]] = receipt
        except (OSError, EOFError, KeyError, TypeError, ValueError) as exc:
            raise HistoryImportError(f"invalid recorded candle original: {exc}") from exc
    restored, partitions = [], []
    for (venue, contract, quote, month), records in sorted(groups.items()):
        rows = [records[t] for t in sorted(records)]
        unknown_trades = sum(row["trades"] == -1 for row in rows)
        coverage = partition_coverage(rows, month=month, through=stop)
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":"),
                                           allow_nan=False).encode()).hexdigest()
        result = _persist(source=SOURCE,
                          key=f"{venue}-{contract}-{quote}-5m-{month}-partial-{first_day}-{last_day}",
                          records=rows, raw_sha=digest, url="RivaL verified private daily ledger backups",
                          out_dir=Path(root), gaps=coverage["missing_elapsed_rows"], extra={
                              "venue": venue, "contract": contract, "quote": quote,
                              "interval_minutes": 5, "utc_month": month,
                              "restored_from_day": first_day.isoformat(),
                              "restored_through_day": last_day.isoformat(),
                              "checksum_verified": True, "source_integrity": INTEGRITY,
                              "provider_signed_checksum": False, "full_calendar_month_verified": False,
                              "source_original_drive_readback_checked": True,
                              "time_reference": "exchange_candle_open_not_first_observed_at",
                              "partition_coverage": coverage,
                              "unknown_trade_count": unknown_trades,
                              "recorded_source_files": list(lineage[(venue, contract, quote, month)].values())})
        restored.append(str(result.manifest_path))
        partitions.append({"venue": venue, "contract": contract, "quote": quote,
                           "unknown_trade_count": unknown_trades, **coverage})
    available = {r["day"] for r in receipts}
    missing_days = [(first_day + timedelta(days=i)).isoformat()
                    for i in range((last_day - first_day).days + 1)
                    if (first_day + timedelta(days=i)).isoformat() not in available]
    return {"restored": restored, "partitions": partitions, "source_files": receipts,
            "missing_backup_days": missing_days,
            "status": "restored_partial_partitions" if restored else "no_eligible_recorded_candles",
            "provider_originals": False, "live_database_modified": False}
