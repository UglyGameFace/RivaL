"""Immutable, read-only DKeX (DraftKings Exchange) public daily report evidence.

DKeX publishes *daily* market and settlement CSVs. These are not live
DraftKings Predictions wallet quotes or permission to place orders. File
access can be supplied by an operator from the official DKeX report pages;
no internal endpoints, location bypass, or guessed contract IDs.
"""
from __future__ import annotations

import csv
import hashlib
import io
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urlsplit

OFFICIAL_REPORT_PAGES = {
    "market": "https://railbirdexchange.com/daily-market-reports",
    "settlement": "https://railbirdexchange.com/daily-settlement-reports",
}
MAX_REPORT_BYTES = 12 * 1024 * 1024
MAX_REPORT_ROWS = 150_000


@dataclass(frozen=True)
class DailyReport:
    day: date
    kind: str
    source_page: str
    sha256: str
    rows: tuple[tuple[str, float | None], ...]


def parse_daily_report(
    raw: bytes, *, day: date, kind: str, sha256: str,
    symbol_column: str, price_column: str | None = None,
) -> DailyReport:
    """Parse an exact-byte snapshot with explicit header mapping.

    Without official evidence of which column means settlement, caller
    must map it explicitly. A market CSV can only supply inventory, not
    an implied probability or a historical executable ask price.
    """
    if kind not in OFFICIAL_REPORT_PAGES:
        raise ValueError("unrecognized public DKeX report type")
    if not isinstance(day, date) or isinstance(day, datetime):
        raise TypeError("DKeX report needs a complete source date")
    if (
        not isinstance(raw, bytes) or not raw or len(raw) > MAX_REPORT_BYTES
        or not isinstance(sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        or hashlib.sha256(raw).hexdigest() != sha256
    ):
        raise ValueError("DKeX report bytes or checksum are not verified")
    if (
        not symbol_column or not isinstance(symbol_column, str)
        or kind == "settlement" and not price_column
        or price_column is not None and not isinstance(price_column, str)
    ):
        raise ValueError("explicit DKeX column mapping required")
    try:
        source = raw.decode("utf-8-sig", errors="strict")
    except UnicodeError as exc:
        raise ValueError("DKeX source must be UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(source))
    if (
        not reader.fieldnames or symbol_column not in reader.fieldnames
        or (kind == "settlement" and price_column not in reader.fieldnames)
    ):
        raise ValueError("published DKeX column names do not match configured mapping")
    parsed = []
    seen = set()
    for row in reader:
        if len(parsed) >= MAX_REPORT_ROWS:
            raise ValueError("DKeX report exceeds bounded import limit")
        symbol = (row.get(symbol_column) or "").strip()
        if not symbol or len(symbol) > 180 or any(ord(ch) < 32 for ch in symbol):
            raise ValueError("invalid official DKeX contract symbol")
        if symbol in seen:
            raise ValueError("ambiguous duplicate contract symbol in one daily report")
        seen.add(symbol)
        value = None
        if kind == "settlement":
            raw_price = (row.get(price_column) or "").strip()
            if not raw_price:
                raise ValueError("missing contract settlement value")
            try:
                value = float(raw_price)
            except (ValueError, OverflowError) as exc:
                raise ValueError("invalid contract settlement value") from exc
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("invalid contract settlement value")
        parsed.append((symbol, value))
    if not parsed:
        raise ValueError("empty DKeX daily report")
    return DailyReport(
        day=day, kind=kind, source_page=OFFICIAL_REPORT_PAGES[kind],
        sha256=sha256, rows=tuple(parsed),
    )


class DKeXPublicReports:
    """Append-only separate DKeX report inventory, never live prediction data."""

    def __init__(self, store):
        self.store = store

    def initialize(self) -> None:
        with self.store.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS rival_dkex_public_reports (
                    report_day TEXT NOT NULL,
                    report_kind TEXT NOT NULL,
                    original_sha256 TEXT NOT NULL,
                    source_page TEXT NOT NULL,
                    contract_count INTEGER NOT NULL,
                    imported_at TEXT NOT NULL,
                    independently_verified INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (report_day, report_kind)
                );
                CREATE TABLE IF NOT EXISTS rival_dkex_daily_contracts (
                    report_day TEXT NOT NULL,
                    report_kind TEXT NOT NULL,
                    contract_symbol TEXT NOT NULL,
                    reported_settlement DOUBLE PRECISION,
                    original_sha256 TEXT NOT NULL,
                    PRIMARY KEY (report_day, report_kind, contract_symbol)
                );
                CREATE INDEX IF NOT EXISTS idx_rival_dkex_daily_contract
                    ON rival_dkex_daily_contracts(contract_symbol, report_day);
            """)

    def ingest(self, report: DailyReport, *, imported_at: datetime) -> int:
        if imported_at.tzinfo is None or imported_at.utcoffset() is None:
            raise ValueError("import time must be timezone-aware")
        if not isinstance(report, DailyReport):
            raise TypeError("a parsed DKeX daily report is required")
        if report.source_page != OFFICIAL_REPORT_PAGES.get(report.kind):
            raise ValueError("report does not match the official DKeX source page")
        if report.day > imported_at.date():
            raise ValueError("future daily report cannot be published")
        self.initialize()
        key = (report.day.isoformat(), report.kind)
        with self.store.connect() as conn:
            saved = conn.execute(
                """SELECT original_sha256 FROM rival_dkex_public_reports
                   WHERE report_day=? AND report_kind=?""", key,
            ).fetchone()
            if saved is not None:
                if saved["original_sha256"] != report.sha256:
                    raise ValueError("conflicting report revision; preserve previous evidence")
                return 0
            conn.execute(
                """INSERT INTO rival_dkex_public_reports
                   (report_day,report_kind,original_sha256,source_page,
                    contract_count,imported_at,independently_verified)
                   VALUES (?,?,?,?,?,?,0)""",
                (*key, report.sha256, report.source_page,
                 len(report.rows), imported_at.isoformat()),
            )
            for symbol, price in report.rows:
                conn.execute(
                    """INSERT INTO rival_dkex_daily_contracts
                       (report_day,report_kind,contract_symbol,
                        reported_settlement,original_sha256)
                       VALUES (?,?,?,?,?)""",
                    (*key, symbol, price, report.sha256),
                )
        return len(report.rows)

    def summary(self) -> dict:
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS reports,COALESCE(SUM(contract_count),0) AS contracts,
                          MAX(report_day) AS latest_date
                   FROM rival_dkex_public_reports""",
            ).fetchone()
            settlements = conn.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN reported_settlement = 0.5
                              THEN 1 ELSE 0 END) AS voids
                   FROM rival_dkex_daily_contracts WHERE report_kind='settlement'""",
            ).fetchone()
        return {
            "daily_reports": row["reports"],
            "contract_rows": row["contracts"],
            "latest_report_day": row["latest_date"],
            "settled_contract_rows": settlements["total"],
            "void_or_half_settlements": settlements["voids"] or 0,
            "live_quotes_connected": False,
            "contract_specific_forward_win_rate": None,
            "official_source_independently_verified": False,
            "prediction_calls_enabled": False,
        }



def main(argv: list[str] | None = None) -> int:
    """Opt-in exact-byte local import, not automated DraftKings app scraping."""
    import argparse
    import json
    import os
    from datetime import UTC
    from pathlib import Path

    from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore

    parser = argparse.ArgumentParser(description="Inspect official DKeX daily report CSV")
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--kind", choices=sorted(OFFICIAL_REPORT_PAGES), required=True)
    parser.add_argument("--sha256", required=True,
                        help="SHA-256 obtained separately from the archived exact bytes")
    parser.add_argument("--symbol-column", required=True)
    parser.add_argument("--settlement-column")
    parser.add_argument("--sqlite-path", type=Path)
    parser.add_argument("--postgres-dsn-env",
                        help="Environment variable containing a PostgreSQL DSN")
    parser.add_argument("--write", action="store_true",
                        help="Append immutable research-only rows to a database")
    args = parser.parse_args(argv)
    if args.write and bool(args.sqlite_path) == bool(args.postgres_dsn_env):
        parser.error("choose exactly one database backend for --write")
    try:
        blob = args.file.read_bytes()
        report = parse_daily_report(
            blob, day=args.date, kind=args.kind,
            sha256=args.sha256, symbol_column=args.symbol_column,
            price_column=args.settlement_column,
        )
        inserted = 0
        if args.write:
            dsn = os.getenv(args.postgres_dsn_env) if args.postgres_dsn_env else None
            if args.postgres_dsn_env and not dsn:
                parser.error("configured PostgreSQL DSN variable is missing")
            store = (
                PostgresHotStore(dsn) if dsn is not None
                else SQLiteHotStore(args.sqlite_path)
            )
            try:
                inserted = DKeXPublicReports(store).ingest(
                    report, imported_at=datetime.now(UTC),
                )
            finally:
                store.close()
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(1, f"DKeX research report rejected: {exc}\n")
    print(json.dumps({
        "report_day": report.day.isoformat(), "report_kind": report.kind,
        "rows": len(report.rows), "sha256": report.sha256,
        "source_page": report.source_page, "new_rows": inserted,
        "independently_verified": False, "live_prices": False,
        "research_only": True, "trading_enabled": False,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
