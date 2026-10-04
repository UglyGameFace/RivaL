from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from parlay_bot.ingestion.models import MarketObservation, NormalizedOddsBoard
from parlay_bot.storage.database import (
    ConnectionLike,
    PostgresDatabase,
    RelationalDatabase,
    SQLiteDatabase,
)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("database timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


@dataclass(frozen=True)
class IngestResult:
    observations_seen: int
    new_states: int
    repeated_states: int
    stale_states_ignored: int
    current_rows_written: int


class RelationalHotStore:
    """Operational store for current RivaL odds, line changes, and user state."""

    def __init__(self, database: RelationalDatabase) -> None:
        self.database = database

    def connect(self):
        return self.database.connect()

    def close(self) -> None:
        self.database.close()

    def initialize(self) -> None:
        auto_id = self.database.auto_increment_primary_key
        with self.connect() as connection:
            connection.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS fixtures (
                    provider TEXT NOT NULL,
                    fixture_id TEXT NOT NULL,
                    sport_id TEXT,
                    tournament_id TEXT,
                    status_id TEXT,
                    status_name TEXT,
                    start_time TEXT,
                    provider_updated_at TEXT,
                    participant1_name TEXT,
                    participant2_name TEXT,
                    sport_name TEXT,
                    tournament_name TEXT,
                    ingested_at TEXT NOT NULL,
                    PRIMARY KEY (provider, fixture_id)
                );

                CREATE TABLE IF NOT EXISTS current_odds (
                    selection_key TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    fixture_id TEXT NOT NULL,
                    bookmaker TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    market_name TEXT,
                    outcome_id TEXT NOT NULL,
                    outcome_name TEXT,
                    player_id TEXT NOT NULL,
                    player_name TEXT,
                    line_value DOUBLE PRECISION,
                    line_group_value DOUBLE PRECISION,
                    deeplink TEXT,
                    active INTEGER NOT NULL,
                    main_line INTEGER NOT NULL,
                    price_decimal DOUBLE PRECISION NOT NULL,
                    price_american TEXT,
                    price_fractional TEXT,
                    bet_limit DOUBLE PRECISION,
                    changed_at TEXT NOT NULL,
                    bookmaker_changed_at TEXT,
                    observed_at TEXT NOT NULL,
                    exchange_meta_json TEXT,
                    state_fingerprint TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_current_fixture_book
                    ON current_odds (fixture_id, bookmaker);

                CREATE INDEX IF NOT EXISTS idx_current_market
                    ON current_odds (fixture_id, market_id, outcome_id, player_id);

                CREATE TABLE IF NOT EXISTS odds_changes (
                    id {auto_id},
                    selection_key TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    fixture_id TEXT NOT NULL,
                    bookmaker TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    market_name TEXT,
                    outcome_id TEXT NOT NULL,
                    outcome_name TEXT,
                    player_id TEXT NOT NULL,
                    player_name TEXT,
                    line_value DOUBLE PRECISION,
                    line_group_value DOUBLE PRECISION,
                    deeplink TEXT,
                    active INTEGER NOT NULL,
                    main_line INTEGER NOT NULL,
                    price_decimal DOUBLE PRECISION NOT NULL,
                    price_american TEXT,
                    price_fractional TEXT,
                    bet_limit DOUBLE PRECISION,
                    provider_changed_at TEXT NOT NULL,
                    bookmaker_changed_at TEXT,
                    exchange_meta_json TEXT,
                    state_fingerprint TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    occurrences INTEGER NOT NULL DEFAULT 1
                );

                CREATE INDEX IF NOT EXISTS idx_changes_selection
                    ON odds_changes (selection_key, id);

                CREATE INDEX IF NOT EXISTS idx_changes_fixture_book
                    ON odds_changes (fixture_id, bookmaker, id);

                CREATE TABLE IF NOT EXISTS user_jurisdictions (
                    platform TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    state_code TEXT NOT NULL CHECK(length(state_code) = 2),
                    source TEXT NOT NULL CHECK(source IN ('state', 'zip')),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (platform, user_id)
                );

                CREATE TABLE IF NOT EXISTS runtime_state (
                    state_key TEXT PRIMARY KEY,
                    state_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._ensure_column(connection, "current_odds", "market_name", "TEXT")
            self._ensure_column(connection, "current_odds", "outcome_name", "TEXT")
            self._ensure_column(connection, "current_odds", "line_value", "DOUBLE PRECISION")
            self._ensure_column(connection, "current_odds", "line_group_value", "DOUBLE PRECISION")
            self._ensure_column(connection, "current_odds", "deeplink", "TEXT")
            self._ensure_column(connection, "odds_changes", "market_name", "TEXT")
            self._ensure_column(connection, "odds_changes", "outcome_name", "TEXT")
            self._ensure_column(connection, "odds_changes", "line_value", "DOUBLE PRECISION")
            self._ensure_column(connection, "odds_changes", "line_group_value", "DOUBLE PRECISION")
            self._ensure_column(connection, "odds_changes", "deeplink", "TEXT")

    def _ensure_column(
        self,
        connection: ConnectionLike,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        if column not in self.database.column_names(connection, table):
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _exchange_json(observation: MarketObservation) -> str | None:
        if observation.exchange_meta is None:
            return None
        return json.dumps(
            observation.exchange_meta,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    def _upsert_fixture(
        self,
        connection: ConnectionLike,
        board: NormalizedOddsBoard,
        *,
        ingested_at: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO fixtures (
                provider, fixture_id, sport_id, tournament_id, status_id, status_name,
                start_time, provider_updated_at, participant1_name, participant2_name,
                sport_name, tournament_name, ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, fixture_id) DO UPDATE SET
                sport_id = excluded.sport_id,
                tournament_id = excluded.tournament_id,
                status_id = excluded.status_id,
                status_name = excluded.status_name,
                start_time = excluded.start_time,
                provider_updated_at = excluded.provider_updated_at,
                participant1_name = excluded.participant1_name,
                participant2_name = excluded.participant2_name,
                sport_name = excluded.sport_name,
                tournament_name = excluded.tournament_name,
                ingested_at = excluded.ingested_at
            """,
            (
                board.provider,
                board.fixture_id,
                str(board.sport_id) if board.sport_id is not None else None,
                str(board.tournament_id) if board.tournament_id is not None else None,
                str(board.status_id) if board.status_id is not None else None,
                board.status_name,
                _iso(board.start_time),
                _iso(board.updated_at),
                board.participant1_name,
                board.participant2_name,
                board.sport_name,
                board.tournament_name,
                _iso(ingested_at),
            ),
        )

    def _record_change(
        self,
        connection: ConnectionLike,
        observation: MarketObservation,
    ) -> str:
        latest = connection.execute(
            """
            SELECT id, state_fingerprint, provider_changed_at, last_seen, occurrences
            FROM odds_changes
            WHERE selection_key = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (observation.selection_key,),
        ).fetchone()

        observed_at = _iso(observation.observed_at)
        if latest is not None and latest["state_fingerprint"] == observation.state_fingerprint:
            last_seen = max(str(latest["last_seen"]), str(observed_at))
            connection.execute(
                """
                UPDATE odds_changes
                SET last_seen = ?, occurrences = occurrences + 1
                WHERE id = ?
                """,
                (last_seen, latest["id"]),
            )
            return "repeated"

        provider_changed_at = _iso(observation.changed_at)
        if latest is not None and provider_changed_at < str(latest["provider_changed_at"]):
            return "stale"

        connection.execute(
            """
            INSERT INTO odds_changes (
                selection_key, provider, fixture_id, bookmaker, market_id, market_name,
                outcome_id, outcome_name, player_id, player_name, line_value,
                line_group_value, deeplink, active, main_line, price_decimal,
                price_american, price_fractional, bet_limit, provider_changed_at,
                bookmaker_changed_at, exchange_meta_json, state_fingerprint,
                first_seen, last_seen, occurrences
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                observation.selection_key,
                observation.provider,
                observation.fixture_id,
                observation.bookmaker,
                observation.market_id,
                observation.market_name,
                observation.outcome_id,
                observation.outcome_name,
                observation.player_id,
                observation.player_name,
                observation.line_value,
                observation.line_group_value,
                observation.deeplink,
                int(observation.active),
                int(observation.main_line),
                observation.price_decimal,
                observation.price_american,
                observation.price_fractional,
                observation.limit,
                _iso(observation.changed_at),
                _iso(observation.bookmaker_changed_at),
                self._exchange_json(observation),
                observation.state_fingerprint,
                observed_at,
                observed_at,
            ),
        )
        return "new"

    def _upsert_current(
        self,
        connection: ConnectionLike,
        observation: MarketObservation,
    ) -> bool:
        cursor = connection.execute(
            """
            INSERT INTO current_odds (
                selection_key, provider, fixture_id, bookmaker, market_id, market_name,
                outcome_id, outcome_name, player_id, player_name, line_value,
                line_group_value, deeplink, active, main_line, price_decimal,
                price_american, price_fractional, bet_limit, changed_at,
                bookmaker_changed_at, observed_at, exchange_meta_json, state_fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(selection_key) DO UPDATE SET
                market_name = excluded.market_name,
                outcome_name = excluded.outcome_name,
                player_name = excluded.player_name,
                line_value = excluded.line_value,
                line_group_value = excluded.line_group_value,
                deeplink = excluded.deeplink,
                active = excluded.active,
                main_line = excluded.main_line,
                price_decimal = excluded.price_decimal,
                price_american = excluded.price_american,
                price_fractional = excluded.price_fractional,
                bet_limit = excluded.bet_limit,
                changed_at = excluded.changed_at,
                bookmaker_changed_at = excluded.bookmaker_changed_at,
                observed_at = excluded.observed_at,
                exchange_meta_json = excluded.exchange_meta_json,
                state_fingerprint = excluded.state_fingerprint
            WHERE excluded.changed_at >= current_odds.changed_at
            """,
            (
                observation.selection_key,
                observation.provider,
                observation.fixture_id,
                observation.bookmaker,
                observation.market_id,
                observation.market_name,
                observation.outcome_id,
                observation.outcome_name,
                observation.player_id,
                observation.player_name,
                observation.line_value,
                observation.line_group_value,
                observation.deeplink,
                int(observation.active),
                int(observation.main_line),
                observation.price_decimal,
                observation.price_american,
                observation.price_fractional,
                observation.limit,
                _iso(observation.changed_at),
                _iso(observation.bookmaker_changed_at),
                _iso(observation.observed_at),
                self._exchange_json(observation),
                observation.state_fingerprint,
            ),
        )
        return cursor.rowcount > 0

    def ingest_board(self, board: NormalizedOddsBoard) -> IngestResult:
        self.initialize()
        new_states = 0
        repeated_states = 0
        stale_states_ignored = 0
        current_rows_written = 0
        ingested_at = datetime.now(UTC)

        with self.connect() as connection:
            self._upsert_fixture(connection, board, ingested_at=ingested_at)
            for observation in board.observations:
                change_result = self._record_change(connection, observation)
                if change_result == "new":
                    new_states += 1
                elif change_result == "repeated":
                    repeated_states += 1
                else:
                    stale_states_ignored += 1

                if self._upsert_current(connection, observation):
                    current_rows_written += 1

        return IngestResult(
            observations_seen=len(board.observations),
            new_states=new_states,
            repeated_states=repeated_states,
            stale_states_ignored=stale_states_ignored,
            current_rows_written=current_rows_written,
        )

    def set_runtime_state(self, key: str, value: str) -> None:
        if not key.strip():
            raise ValueError("runtime state key is required")
        self.initialize()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO runtime_state (state_key, state_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    state_value = excluded.state_value,
                    updated_at = excluded.updated_at
                """,
                (key.strip(), value, _iso(datetime.now(UTC))),
            )

    def get_runtime_state(self, key: str) -> str | None:
        self.initialize()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT state_value FROM runtime_state WHERE state_key = ?",
                (key.strip(),),
            ).fetchone()
        return str(row["state_value"]) if row is not None else None

    def set_user_jurisdiction(
        self,
        *,
        platform: str,
        user_id: str,
        state_code: str,
        source: str,
    ) -> None:
        if source not in {"state", "zip"}:
            raise ValueError("jurisdiction source must be 'state' or 'zip'")
        if not platform.strip() or not user_id.strip():
            raise ValueError("platform and user_id are required")

        code = state_code.strip().upper()
        if len(code) != 2:
            raise ValueError("state_code must be a two-letter code")

        self.initialize()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO user_jurisdictions (
                    platform, user_id, state_code, source, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(platform, user_id) DO UPDATE SET
                    state_code = excluded.state_code,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    platform.strip().lower(),
                    user_id.strip(),
                    code,
                    source,
                    _iso(datetime.now(UTC)),
                ),
            )

    def get_user_jurisdiction(
        self,
        *,
        platform: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT platform, user_id, state_code, source, updated_at
                FROM user_jurisdictions
                WHERE platform = ? AND user_id = ?
                """,
                (platform.strip().lower(), user_id.strip()),
            ).fetchone()
        return dict(row) if row is not None else None

    def current_for_fixture(self, fixture_id: str) -> list[dict[str, Any]]:
        self.initialize()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM current_odds
                WHERE fixture_id = ?
                ORDER BY bookmaker, market_id, outcome_id, player_id
                """,
                (fixture_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def changes_for_selection(self, selection_key: str) -> list[dict[str, Any]]:
        self.initialize()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM odds_changes
                WHERE selection_key = ?
                ORDER BY id
                """,
                (selection_key,),
            ).fetchall()
        return [dict(row) for row in rows]


class SQLiteHotStore(RelationalHotStore):
    """Local/test compatibility backend."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        super().__init__(SQLiteDatabase(self.path))


class PostgresHotStore(RelationalHotStore):
    """Managed PostgreSQL backend for Discloud production."""

    def __init__(self, dsn: str) -> None:
        super().__init__(PostgresDatabase(dsn))
