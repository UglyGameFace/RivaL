from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from parlay_bot.archive.models import HistoryArchiveBatch
from parlay_bot.storage.hot import (
    PostgresHotStore,
    RelationalHotStore,
    SQLiteHotStore,
)


def _slug(value: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in value.strip())
    return "-".join(part for part in cleaned.split("-") if part) or "unknown"


class RelationalArchiveQueue:
    """Select only closed line states and mark them archived transactionally."""

    def __init__(self, store: RelationalHotStore) -> None:
        self.store = store

    def connect(self):
        return self.store.connect()

    def initialize(self) -> None:
        self.store.initialize()
        with self.connect() as connection:
            columns = self.store.database.column_names(connection, "odds_changes")
            if "archived_batch_id" not in columns:
                connection.execute(
                    "ALTER TABLE odds_changes ADD COLUMN archived_batch_id TEXT"
                )
            if "archived_at" not in columns:
                connection.execute("ALTER TABLE odds_changes ADD COLUMN archived_at TEXT")

            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_changes_archive_pending
                    ON odds_changes (archived_batch_id, last_seen, id);

                CREATE TABLE IF NOT EXISTS archive_batches (
                    batch_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    bookmaker TEXT NOT NULL,
                    sport TEXT NOT NULL,
                    partition_date TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    history_file_id TEXT,
                    manifest_file_id TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                """
            )

    @staticmethod
    def _batch_id(
        *,
        provider: str,
        bookmaker: str,
        sport: str,
        partition_date: str,
        row_ids: list[int],
    ) -> str:
        identity = "|".join(
            (
                "archive-v1",
                provider,
                bookmaker,
                sport,
                partition_date,
                ",".join(str(item) for item in row_ids),
            )
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]

    def next_batch(
        self,
        *,
        cutoff: datetime,
        max_rows: int,
    ) -> HistoryArchiveBatch | None:
        if cutoff.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware")
        if max_rows < 1:
            raise ValueError("max_rows must be positive")

        self.initialize()
        cutoff_iso = cutoff.astimezone(UTC).isoformat()

        with self.connect() as connection:
            first = connection.execute(
                """
                WITH latest AS (
                    SELECT selection_key, MAX(id) AS latest_id
                    FROM odds_changes
                    GROUP BY selection_key
                )
                SELECT
                    oc.provider,
                    oc.bookmaker,
                    COALESCE(f.tournament_name, f.sport_name, 'unknown') AS sport,
                    substr(oc.last_seen, 1, 10) AS partition_date
                FROM odds_changes AS oc
                JOIN fixtures AS f
                  ON f.provider = oc.provider
                 AND f.fixture_id = oc.fixture_id
                JOIN latest AS l
                  ON l.selection_key = oc.selection_key
                WHERE oc.archived_batch_id IS NULL
                  AND oc.id <> l.latest_id
                  AND oc.last_seen <= ?
                ORDER BY oc.last_seen, oc.id
                LIMIT 1
                """,
                (cutoff_iso,),
            ).fetchone()

            if first is None:
                return None

            rows = connection.execute(
                """
                WITH latest AS (
                    SELECT selection_key, MAX(id) AS latest_id
                    FROM odds_changes
                    GROUP BY selection_key
                )
                SELECT
                    oc.id,
                    oc.selection_key,
                    oc.provider,
                    oc.fixture_id,
                    oc.bookmaker,
                    COALESCE(f.sport_name, 'unknown') AS sport_name,
                    COALESCE(f.tournament_name, 'unknown') AS tournament_name,
                    oc.market_id,
                    oc.market_name,
                    oc.outcome_id,
                    oc.outcome_name,
                    oc.player_id,
                    oc.player_name,
                    oc.line_value,
                    oc.line_group_value,
                    oc.deeplink,
                    oc.active,
                    oc.main_line,
                    oc.price_decimal,
                    oc.price_american,
                    oc.price_fractional,
                    oc.bet_limit,
                    oc.provider_changed_at,
                    oc.bookmaker_changed_at,
                    oc.exchange_meta_json,
                    oc.state_fingerprint,
                    oc.first_seen,
                    oc.last_seen,
                    oc.occurrences
                FROM odds_changes AS oc
                JOIN fixtures AS f
                  ON f.provider = oc.provider
                 AND f.fixture_id = oc.fixture_id
                JOIN latest AS l
                  ON l.selection_key = oc.selection_key
                WHERE oc.archived_batch_id IS NULL
                  AND oc.id <> l.latest_id
                  AND oc.last_seen <= ?
                  AND oc.provider = ?
                  AND oc.bookmaker = ?
                  AND COALESCE(f.tournament_name, f.sport_name, 'unknown') = ?
                  AND substr(oc.last_seen, 1, 10) = ?
                ORDER BY oc.last_seen, oc.id
                LIMIT ?
                """,
                (
                    cutoff_iso,
                    first["provider"],
                    first["bookmaker"],
                    first["sport"],
                    first["partition_date"],
                    max_rows,
                ),
            ).fetchall()

        if not rows:
            return None

        dictionaries = [dict(row) for row in rows]
        row_ids = [int(row["id"]) for row in dictionaries]
        provider = str(first["provider"])
        bookmaker = str(first["bookmaker"])
        sport = str(first["sport"])
        partition_date = str(first["partition_date"])
        batch_id = self._batch_id(
            provider=provider,
            bookmaker=bookmaker,
            sport=sport,
            partition_date=partition_date,
            row_ids=row_ids,
        )

        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO archive_batches (
                    batch_id, provider, bookmaker, sport, partition_date,
                    row_count, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?)
                ON CONFLICT(batch_id) DO NOTHING
                """,
                (
                    batch_id,
                    provider,
                    bookmaker,
                    sport,
                    partition_date,
                    len(dictionaries),
                    datetime.now(UTC).isoformat(),
                ),
            )

        return HistoryArchiveBatch(
            batch_id=batch_id,
            provider=provider,
            bookmaker=bookmaker,
            sport=_slug(sport),
            partition_date=datetime.fromisoformat(partition_date).date(),
            row_ids=tuple(row_ids),
            rows=tuple(dictionaries),
        )

    def set_history_file(self, batch_id: str, file_id: str) -> None:
        self.initialize()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE archive_batches
                SET history_file_id = ?, status = 'history_uploaded'
                WHERE batch_id = ?
                """,
                (file_id, batch_id),
            )

    def set_manifest_file(self, batch_id: str, file_id: str) -> None:
        self.initialize()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE archive_batches
                SET manifest_file_id = ?, status = 'manifest_uploaded'
                WHERE batch_id = ?
                """,
                (file_id, batch_id),
            )

    def mark_complete(
        self,
        *,
        batch_id: str,
        row_ids: tuple[int, ...],
        completed_at: datetime,
    ) -> None:
        if completed_at.tzinfo is None:
            raise ValueError("completed_at must be timezone-aware")
        if not row_ids:
            raise ValueError("row_ids cannot be empty")

        archived_at = completed_at.astimezone(UTC).isoformat()
        updated = 0

        with self.connect() as connection:
            batch = connection.execute(
                """
                SELECT history_file_id, manifest_file_id
                FROM archive_batches
                WHERE batch_id = ?
                """,
                (batch_id,),
            ).fetchone()
            if (
                batch is None
                or not batch["history_file_id"]
                or not batch["manifest_file_id"]
            ):
                raise RuntimeError(
                    "archive batch cannot complete before history and manifest uploads"
                )

            for offset in range(0, len(row_ids), 500):
                chunk = row_ids[offset : offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                cursor = connection.execute(
                    f"""
                    UPDATE odds_changes
                    SET archived_batch_id = ?, archived_at = ?
                    WHERE archived_batch_id IS NULL
                      AND id IN ({placeholders})
                    """,
                    (batch_id, archived_at, *chunk),
                )
                updated += cursor.rowcount

            if updated != len(row_ids):
                raise RuntimeError(
                    f"archive row count changed before completion: "
                    f"expected {len(row_ids)}, updated {updated}"
                )

            connection.execute(
                """
                UPDATE archive_batches
                SET status = 'complete', completed_at = ?
                WHERE batch_id = ?
                """,
                (archived_at, batch_id),
            )

    def batch_record(self, batch_id: str) -> dict | None:
        self.initialize()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM archive_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
        return dict(row) if row is not None else None


class SQLiteArchiveQueue(RelationalArchiveQueue):
    """Local/test compatibility archive queue."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        super().__init__(SQLiteHotStore(self.path))


class PostgresArchiveQueue(RelationalArchiveQueue):
    """Standalone PostgreSQL archive queue compatibility helper."""

    def __init__(self, dsn: str) -> None:
        super().__init__(PostgresHotStore(dsn))
