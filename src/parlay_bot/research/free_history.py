"""Verified, research-only intake of free public historical market/odds files.

This never modifies RivaL's live collectors, operational PostgreSQL candle
tables, forecast ledger, or model governance. Archives are *retrospective*.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

SCHEMA = "rival-research-history-v1"
BASE_BINANCE = "https://data.binance.vision/data/futures/um/monthly/klines"
BASE_FOOTBALL = "https://www.football-data.co.uk/mmz4281"
INTERVALS = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
ASSETS = {"BTC", "ETH", "SOL"}
LEAGUES = {"E0", "E1", "D1", "I1", "SP1", "F1"}
MAX_DOWNLOAD = 32 * 1024 * 1024
MAX_UNCOMPRESSED = 64 * 1024 * 1024
MAX_ROWS = 100_000


class HistoryImportError(ValueError):
    """Untrusted, incomplete, incompatible or unverifiable historical data."""


@dataclass(frozen=True)
class IntakeResult:
    source: str
    records: int
    data_path: Path
    manifest_path: Path
    duplicates: int
    gaps: int


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _number(raw: str, *, positive: bool = False) -> float:
    try:
        number = float(raw)
    except (ValueError, TypeError, OverflowError) as exc:
        raise HistoryImportError("invalid source numeric field") from exc
    if not math.isfinite(number) or number < 0 or positive and number == 0:
        raise HistoryImportError("nonfinite, negative or zero required numeric field")
    return number


def _fetch(client: httpx.Client, url: str, *, limit: int = MAX_DOWNLOAD) -> bytes:
    # URL construction is controlled by validated enumerations, never user URLs.
    try:
        with client.stream("GET", url, follow_redirects=False, timeout=35) as response:
            response.raise_for_status()
            if response.is_redirect:
                raise HistoryImportError("source redirected unexpectedly")
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > limit:
                    raise HistoryImportError("source exceeds download budget")
                chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise HistoryImportError("official source unavailable") from exc
    return b"".join(chunks)


def _month_key(raw: str) -> str:
    if not re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", raw):
        raise HistoryImportError("month must be YYYY-MM")
    year, month = map(int, raw.split("-"))
    if (year, month) >= (datetime.now(UTC).year, datetime.now(UTC).month):
        raise HistoryImportError("only complete historical months are eligible")
    return raw


def binance_archive_url(*, asset: str, interval: str, month: str) -> str:
    if asset not in ASSETS or interval not in INTERVALS:
        raise HistoryImportError("unsupported asset or source interval")
    month = _month_key(month)
    symbol = f"{asset}USDT"
    return f"{BASE_BINANCE}/{symbol}/{interval}/{symbol}-{interval}-{month}.zip"


def _checked_zip(payload: bytes, name: str) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = archive.infolist()
            if len(members) != 1 or members[0].filename != name:
                raise HistoryImportError("archive contains unexpected entries")
            info = members[0]
            if info.is_dir() or info.file_size > MAX_UNCOMPRESSED:
                raise HistoryImportError("archive exceeds decompression budget")
            with archive.open(info) as handle:
                content = handle.read(MAX_UNCOMPRESSED + 1)
            if len(content) != info.file_size or len(content) > MAX_UNCOMPRESSED:
                raise HistoryImportError("invalid uncompressed file size")
            return content
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise HistoryImportError("corrupt or unreadable official archive") from exc


def _binance_rows(data: bytes, interval: str) -> tuple[list[dict[str, Any]], int]:
    try:
        source = io.StringIO(data.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise HistoryImportError("non-UTF8 market data") from exc
    reader = csv.reader(source)
    parsed: list[dict[str, Any]] = []
    seen: set[int] = set()
    step = INTERVALS[interval] * 60_000
    previous: int | None = None
    gaps = 0
    for line, values in enumerate(reader, 1):
        if not values or all(not x.strip() for x in values):
            continue
        if line == 1 and values[0].strip().lower() in ("open_time", "open time"):
            continue
        if len(values) != 12 or len(parsed) >= MAX_ROWS:
            raise HistoryImportError("invalid Binance column count or excessive rows")
        try:
            opened = int(values[0])
            closed = int(values[6])
            trades = int(values[8])
        except (ValueError, OverflowError) as exc:
            raise HistoryImportError("invalid Binance candle timestamps or trades") from exc
        if opened <= 0 or opened % step or closed != opened + step - 1:
            raise HistoryImportError("invalid or misaligned Binance candle times")
        if opened in seen or previous is not None and opened <= previous:
            raise HistoryImportError("duplicate or out-of-order Binance candles")
        if previous is not None and (opened - previous) % step:
            raise HistoryImportError("unaligned Binance gap")
        if previous is not None:
            gaps += (opened - previous) // step - 1
        seen.add(opened)
        previous = opened
        op, hi, lo, close = (_number(values[n], positive=True) for n in (1, 2, 3, 4))
        base_volume = _number(values[5])
        if lo > min(op, close) or hi < max(op, close) or trades < 0:
            raise HistoryImportError("invalid Binance OHLC/trades")
        _number(values[7])
        _number(values[9])
        _number(values[10])
        if len(values[11]) > 100:
            raise HistoryImportError("invalid ignored field")
        parsed.append({
            "opened_at": datetime.fromtimestamp(opened / 1000, UTC).isoformat(),
            "closed_at": datetime.fromtimestamp(closed / 1000, UTC).isoformat(),
            "open": op, "high": hi, "low": lo, "close": close,
            "volume_base": base_volume, "trades": trades,
        })
    if not parsed:
        raise HistoryImportError("empty official market archive")
    return parsed, gaps


def _football_rows(payload: bytes, league: str, season: str) -> tuple[list[dict[str, Any]], int]:
    try:
        source = io.StringIO(payload.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise HistoryImportError("non-UTF8 football source") from exc
    rows: list[dict[str, Any]] = []
    skipped = 0
    reader = csv.DictReader(source)
    columns = set(reader.fieldnames or ())
    required = {"Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR"}
    if not required <= columns:
        raise HistoryImportError("missing football settlement columns")
    for record in reader:
        if len(rows) + skipped >= MAX_ROWS:
            raise HistoryImportError("football source exceeds row budget")
        if not record.get("FTR") or not record.get("HomeTeam"):
            skipped += 1
            continue  # Future/blank fixtures are never treated as settled.
        if record["FTR"] not in ("H", "D", "A"):
            raise HistoryImportError("unknown football full-time result")
        try:
            when = next(
                datetime.strptime(record["Date"].strip(), fmt).date()
                for fmt in ("%d/%m/%Y", "%d/%m/%y", "%d/%m/%Y %H:%M")
                if _date_matches(record["Date"].strip(), fmt)
            )
            home_goals, away_goals = int(record["FTHG"]), int(record["FTAG"])
        except (ValueError, StopIteration, TypeError) as exc:
            raise HistoryImportError("invalid settled fixture record") from exc
        if home_goals < 0 or away_goals < 0 or when > datetime.now(UTC).date():
            raise HistoryImportError("invalid fixture goals or future outcome")
        expected = "H" if home_goals > away_goals else "A" if home_goals < away_goals else "D"
        if expected != record["FTR"]:
            raise HistoryImportError("football result does not match goals")
        book_odds = {}
        for book in ("B365", "PS", "WH", "BW", "BF"):
            opening = _odds_triplet(record, (book + "H", book + "D", book + "A"))
            closing = _odds_triplet(record, (book + "CH", book + "CD", book + "CA"))
            if opening or closing:
                book_odds[book] = {"opening": opening, "closing": closing}
        if not book_odds:
            skipped += 1
            continue  # No prices, no odds research.
        home, away = record["HomeTeam"].strip(), record["AwayTeam"].strip()
        if not home or not away or home == away or len(home) > 120 or len(away) > 120:
            raise HistoryImportError("invalid team identity")
        rows.append({
            "date": when.isoformat(), "date_precision": "day",
            "home": home, "away": away, "result": record["FTR"],
            "goals": [home_goals, away_goals], "books": book_odds,
            "odds_snapshot_time_verified": False,
        })
    if not rows:
        raise HistoryImportError("no settled matches with usable bookmaker odds")
    unique: set[tuple[str, str, str]] = set()
    for row in rows:
        key = (row["date"], row["home"], row["away"])
        if key in unique:
            raise HistoryImportError("duplicate football fixture")
        unique.add(key)
    return rows, skipped


def _date_matches(value: str, fmt: str) -> bool:
    try:
        datetime.strptime(value, fmt)
        return True
    except ValueError:
        return False


def _odds_triplet(row: dict[str, str], keys: tuple[str, ...]) -> list[float] | None:
    if not all(row.get(key) and row[key].strip() for key in keys):
        return None
    result = [_number(row[key], positive=True) for key in keys]
    if any(price <= 1.0 or price > 1000 for price in result):
        raise HistoryImportError("invalid bookmaker three-way price")
    return result


def _persist(
    *, source: str, key: str, records: list[dict[str, Any]],
    raw_sha: str, url: str, out_dir: Path, gaps: int = 0, duplicates: int = 0,
    extra: dict[str, Any] | None = None,
) -> IntakeResult:
    out_dir = out_dir.resolve()
    target = out_dir / source
    raw = "".join(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n"
                  for item in records).encode("utf-8")
    digest = _sha(raw)
    name = f"{key}--{digest[:16]}"
    data_path = target / (name + ".ndjson")
    manifest_path = target / (name + ".manifest.json")
    manifest = {
        "schema_version": SCHEMA, "type": "offline_historical_research_only",
        "source": source, "source_url": url, "source_sha256": raw_sha,
        "data_sha256": digest, "records": len(records), "gaps": gaps,
        "duplicates": duplicates, "file": data_path.name,
        "retrospective": True, "prospective_forecast": False,
        "live_trading_authorized": False, **(extra or {}),
    }
    manifest_data = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    target.mkdir(parents=True, exist_ok=True)
    for path, payload in ((data_path, raw), (manifest_path, manifest_data)):
        if path.exists():
            if path.read_bytes() != payload:
                raise HistoryImportError("existing research artifact digest conflict")
            continue
        temporary = path.with_name(path.name + ".tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return IntakeResult(source, len(records), data_path, manifest_path, duplicates, gaps)


def ingest_binance(
    client: httpx.Client, *, asset: str, interval: str, month: str, out_dir: Path,
) -> IntakeResult:
    url = binance_archive_url(asset=asset, interval=interval, month=month)
    filename = url.rsplit("/", 1)[-1]
    checksum = _fetch(client, url + ".CHECKSUM", limit=1024).decode("ascii").strip()
    match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?([A-Za-z0-9_.-]+)", checksum)
    if match is None or match[2] != filename:
        raise HistoryImportError("missing or mismatched Binance SHA-256 checksum")
    payload = _fetch(client, url)
    if _sha(payload) != match[1].lower():
        raise HistoryImportError("official Binance checksum verification failed")
    extracted = _checked_zip(payload, filename[:-4] + ".csv")
    rows, gaps = _binance_rows(extracted, interval)
    first_month = rows[0]["opened_at"][:7]
    last_month = rows[-1]["opened_at"][:7]
    if first_month != month or last_month != month:
        raise HistoryImportError("Binance archive contains candles outside declared month")
    return _persist(
        source="binance_um_klines", key=f"{asset}USDT-{interval}-{month}",
        records=rows, raw_sha=_sha(payload), url=url, out_dir=out_dir, gaps=gaps,
        extra={"venue": "binance", "market_type": "usd_m_linear_futures",
               "contract": f"{asset}USDT", "quote": "USDT",
               "interval_minutes": INTERVALS[interval], "checksum_verified": True,
               "time_reference": "exchange_candle_open_not_first_observed_at"},
    )


def ingest_football(
    client: httpx.Client, *, league: str, season: str, out_dir: Path,
) -> IntakeResult:
    if league not in LEAGUES or not re.fullmatch(r"\d{4}", season):
        raise HistoryImportError("invalid league or season")
    first = int(season[:2])
    second = int(season[2:])
    if second != (first + 1) % 100:
        raise HistoryImportError("invalid football season code")
    url = f"{BASE_FOOTBALL}/{season}/{league}.csv"
    payload = _fetch(client, url, limit=8 * 1024 * 1024)
    rows, skipped = _football_rows(payload, league, season)
    return _persist(
        source="football_data", key=f"{league}-{season}", records=rows,
        raw_sha=_sha(payload), url=url, out_dir=out_dir,
        extra={"league": league, "season": season, "rows_missing_prices_or_result": skipped,
               "opening_odds_have_verified_capture_time": False,
               "closing_odds_never_available_for_opening_prediction": True,
               "source_redistribution_rights_not_assumed": True},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RivaL verified offline historical intake")
    parser.add_argument("--out", type=Path, default=Path("data/research-history"))
    sub = parser.add_subparsers(dest="source", required=True)
    crypto = sub.add_parser("binance", help="Official checksum-verified USD-M futures month")
    crypto.add_argument("--asset", choices=sorted(ASSETS), required=True)
    crypto.add_argument("--interval", choices=sorted(INTERVALS), required=True)
    crypto.add_argument("--month", required=True, help="Complete YYYY-MM")
    football = sub.add_parser("football", help="Historical football match results and odds")
    football.add_argument("--league", choices=sorted(LEAGUES), required=True)
    football.add_argument("--season", required=True, help="e.g. 2526 for 2025/26")
    args = parser.parse_args(argv)
    try:
        with httpx.Client() as client:
            if args.source == "binance":
                result = ingest_binance(
                    client, asset=args.asset, interval=args.interval,
                    month=args.month, out_dir=args.out,
                )
            else:
                result = ingest_football(
                    client, league=args.league, season=args.season, out_dir=args.out,
                )
    except HistoryImportError as exc:
        parser.exit(status=1, message=f"History intake rejected: {exc}\n")
    print(json.dumps({
        "source": result.source, "records": result.records,
        "data": str(result.data_path), "manifest": str(result.manifest_path),
        "gaps": result.gaps,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
