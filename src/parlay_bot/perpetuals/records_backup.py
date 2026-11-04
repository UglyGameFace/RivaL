"""Nightly backup of RivaL's own live records into the private Drive warehouse.

Each completed UTC day of each table becomes one gzipped NDJSON file
(`rival-records__<table>__<YYYY-MM-DD>.ndjson.gz`). These records exist
nowhere else: the live order-book/flow recordings, every exchange candle the
bot saw, the Polymarket prices and contract features it learned from, every
graded forecast and every RivaL Intelligence review. Idempotent: a day that
was saved is never rewritten. Read-only on the database.
"""
from __future__ import annotations

import gzip
import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

_LOG = logging.getLogger(__name__)
# (table, time column). Days are exported only once every row has settled.
TABLES = (
    ("perp_liquidity_5m", "bucket_start"),
    ("perp_market_candles", "opened_at"),
    ("perp_market_contexts", "observed_at"),
    ("perp_forward_audit", "issued_at"),
    ("rival_model_shadow_forecasts", "issued_at"),
    ("rival_intelligence_calls", "issued_at"),
    ("rival_polymarket_observations", "learned_at"),
    ("rival_polymarket_model_forward", "issued_at"),
    ("rival_poly_oracle_observations", "observed_at"),
)
SETTLE_DELAY = timedelta(hours=3)  # 60-minute targets settle well inside this
MAX_DAYS_PER_RUN = 14  # bounded backfill per night
MAX_BACKFILL_DAYS = 120


def file_name(table: str, day: date) -> str:
    return f"rival-records__{table}__{day.isoformat()}.ndjson.gz"


class RecordsBackup:
    def __init__(self, store, drive, workdir: Path) -> None:
        self.store = store
        self.drive = drive
        self.workdir = workdir
        self._ready = False

    def initialize(self) -> None:
        if self._ready:
            return
        with self.store.connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS rival_records_backup (
                    table_name TEXT NOT NULL, day TEXT NOT NULL,
                    rows INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    drive_file_id TEXT NOT NULL, saved_at TEXT NOT NULL,
                    PRIMARY KEY (table_name, day))"""
            )
        self._ready = True

    def pending(self, now: datetime) -> list[tuple[str, str, date]]:
        """(table, column, day) still to save, oldest first, bounded."""
        self.initialize()
        last_day = (now - SETTLE_DELAY).date() - timedelta(days=1)
        out = []
        for table, column in TABLES:
            try:
                with self.store.connect() as conn:
                    first = conn.execute(f"SELECT MIN({column}) FROM {table}").fetchone()[0]
                    done = {str(r["day"]) for r in conn.execute(
                        "SELECT day FROM rival_records_backup WHERE table_name=?",
                        (table,)).fetchall()}
            except Exception:  # noqa: BLE001 - table not created on this host
                continue
            if first is None:
                continue
            start = max(datetime.fromisoformat(str(first).replace(" ", "T", 1)).date(),
                        last_day - timedelta(days=MAX_BACKFILL_DAYS))
            day = start
            while day <= last_day:
                if day.isoformat() not in done:
                    out.append((table, column, day))
                day += timedelta(days=1)
        out.sort(key=lambda item: item[2])
        return out[:MAX_DAYS_PER_RUN * len(TABLES)]

    def _rows(self, table: str, column: str, day: date) -> list[dict]:
        lo = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        hi = lo + timedelta(days=1)
        with self.store.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE {column}>=? AND {column}<? ORDER BY {column}",
                (lo.isoformat(), hi.isoformat()),
            ).fetchall()
        return [dict(row) for row in rows]

    async def save_day(self, table: str, column: str, day: date) -> int:
        import asyncio

        from parlay_bot.research.publish import _artifact

        rows = await asyncio.to_thread(self._rows, table, column, day)
        path = self.workdir / file_name(table, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, default=str, sort_keys=True) + "\n")
        try:
            artifact = _artifact(path, len(rows))
            stored = await self.drive.ensure_history_file(
                artifact=artifact, batch_id=f"records-{table}-{day.isoformat()}",
                mime_type="application/gzip",
            )
            with self.store.connect() as conn:
                conn.execute(
                    """INSERT INTO rival_records_backup
                       (table_name,day,rows,sha256,drive_file_id,saved_at)
                       VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                    (table, day.isoformat(), len(rows), artifact.sha256,
                     stored.file_id, datetime.now(UTC).isoformat()),
                )
        finally:
            path.unlink(missing_ok=True)
        return len(rows)

    async def run_once(self, now: datetime | None = None) -> dict:
        import asyncio

        stamp = now or datetime.now(UTC)
        saved, rows, failed = 0, 0, 0
        for table, column, day in await asyncio.to_thread(self.pending, stamp):
            try:
                rows += await self.save_day(table, column, day)
                saved += 1
            except Exception:  # noqa: BLE001 - retried next run
                failed += 1
                _LOG.warning("Records backup failed for %s %s", table, day)
        if saved or failed:
            _LOG.info("Records backup saved %d day-files (%d rows), %d failed",
                      saved, rows, failed)
        return {"saved": saved, "rows": rows, "failed": failed}
