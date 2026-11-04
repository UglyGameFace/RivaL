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
ASSETS = ("BTC", "ETH", "SOL", "XRP")
HORIZONS = (5, 15, 60)
_BY_ASSET_HORIZON = tuple(("asset=? AND horizon_minutes=?", (a, h))
                          for a in ASSETS for h in HORIZONS)
# (table, time column, scopes). Each scope is an extra indexed filter so a
# day's export never scans a whole large table.
TABLES = (
    ("perp_liquidity_5m", "bucket_start", (("", ()),)),
    # idx_perp_market_candles_observed (interval_minutes, first_observed_at)
    ("perp_market_candles", "first_observed_at", (("interval_minutes=?", (5,)),)),
    ("perp_market_contexts", "observed_at", (("", ()),)),
    # idx_perp_forward_asset_horizon (asset, horizon_minutes, issued_at)
    ("perp_forward_audit", "issued_at", _BY_ASSET_HORIZON),
    # idx_rival_model_shadow_score (asset, horizon_minutes, family, issued_at)
    ("rival_model_shadow_forecasts", "issued_at", _BY_ASSET_HORIZON),
    ("rival_intelligence_calls", "issued_at", (("", ()),)),
    ("rival_polymarket_observations", "learned_at", (("", ()),)),
    ("rival_polymarket_model_forward", "issued_at", (("", ()),)),
    ("rival_poly_oracle_observations", "observed_at", (("", ()),)),
    ("rival_kalshi_updown", "issued_at", (("", ()),)),
)
SETTLE_DELAY = timedelta(hours=3)  # 60-minute targets settle well inside this
MAX_DAYS_PER_RUN = 3  # gentle on the database: about 70 day-files per day
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
        first_day = last_day - timedelta(days=MAX_BACKFILL_DAYS)
        with self.store.connect() as conn:
            done = {(str(r["table_name"]), str(r["day"])) for r in conn.execute(
                "SELECT table_name, day FROM rival_records_backup WHERE day>=?",
                (first_day.isoformat(),)).fetchall()}
        out = []
        for table, column, _ in TABLES:
            todo = []
            day = last_day
            while day >= first_day and len(todo) < MAX_DAYS_PER_RUN:
                if (table, day.isoformat()) not in done:
                    todo.append((table, column, day))
                day -= timedelta(days=1)
            out += todo  # newest first, so recent days are safe soonest
        return out

    def _write(self, table: str, column: str, day: date, path: Path) -> int:
        """Stream one day to a gzip file, ONE HOUR at a time.

        Never holds a whole day of a large table in memory (the bot's host
        has a 512 MB limit); each hour's rows are written and released.
        """
        start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        scopes = next(s for t, _, s in TABLES if t == table)
        count = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            for hour in range(24):
                lo, hi = start + timedelta(hours=hour), start + timedelta(hours=hour + 1)
                for where, params in scopes:
                    extra = f"{where} AND " if where else ""
                    with self.store.connect() as conn:
                        rows = conn.execute(
                            f"SELECT * FROM {table} WHERE {extra}{column}>=? AND {column}<?",
                            (*params, lo.isoformat(), hi.isoformat()),
                        ).fetchall()
                    for row in rows:
                        handle.write(json.dumps(dict(row), default=str, sort_keys=True) + "\n")
                    count += len(rows)
                    del rows
        return count

    def _table_exists(self, table: str) -> bool:
        try:
            with self.store.connect() as conn:
                conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchall()
            return True
        except Exception:  # noqa: BLE001
            return False

    def _mark(self, table: str, day: date, rows: int, sha256: str, file_id: str) -> None:
        with self.store.connect() as conn:
            conn.execute(
                """INSERT INTO rival_records_backup
                   (table_name,day,rows,sha256,drive_file_id,saved_at)
                   VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                (table, day.isoformat(), rows, sha256, file_id,
                 datetime.now(UTC).isoformat()))

    async def save_day(self, table: str, column: str, day: date) -> int:
        import asyncio

        from parlay_bot.research.publish import _artifact

        path = self.workdir / file_name(table, day)
        try:
            try:
                rows = await asyncio.to_thread(self._write, table, column, day, path)
            except Exception as exc:
                if self._table_exists(table):
                    # A failed read is NOT an empty day: retry on the next run.
                    raise RuntimeError(f"records read failed for {table} {day}") from exc
                rows = 0  # table never created on this host: genuinely nothing
            if not rows:
                await asyncio.to_thread(self._mark, table, day, 0, "", "")
                return 0
            artifact = _artifact(path, rows)
            stored = await self.drive.ensure_history_file(
                artifact=artifact, batch_id=f"records-{table}-{day.isoformat()}",
                mime_type="application/gzip",
            )
            await asyncio.to_thread(self._mark, table, day, rows, artifact.sha256,
                                    stored.file_id)
            return rows
        finally:
            path.unlink(missing_ok=True)

    async def run_once(self, now: datetime | None = None) -> dict:
        import asyncio

        stamp = now or datetime.now(UTC)
        saved, rows, failed = 0, 0, 0
        from parlay_bot.perpetuals import host_memory

        for table, column, day in await asyncio.to_thread(self.pending, stamp):
            if host_memory.snapshot().pressure:
                _LOG.info("Records backup paused: host memory pressure")
                break
            try:
                count = await self.save_day(table, column, day)
                rows += count
                saved += count > 0
            except Exception:  # noqa: BLE001 - retried next run
                failed += 1
                _LOG.warning("Records backup failed for %s %s", table, day)
        if saved or failed:
            _LOG.info("Records backup saved %d day-files (%d rows), %d failed",
                      saved, rows, failed)
        return {"saved": saved, "rows": rows, "failed": failed}
