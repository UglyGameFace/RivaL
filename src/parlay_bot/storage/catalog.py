from __future__ import annotations

from pathlib import Path

from parlay_bot.domain.catalog import MarketDefinition
from parlay_bot.storage.database import RelationalDatabase, SQLiteDatabase
from parlay_bot.storage.hot import RelationalHotStore


class MarketCatalogStore:
    """Persist human-readable OddsPapi market and outcome metadata."""

    def __init__(
        self,
        database: RelationalDatabase | RelationalHotStore | str | Path,
    ) -> None:
        if isinstance(database, RelationalHotStore):
            self.database = database.database
        elif isinstance(database, RelationalDatabase):
            self.database = database
        else:
            self.database = SQLiteDatabase(database)

    def connect(self):
        return self.database.connect()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS market_catalog (
                    market_id TEXT PRIMARY KEY,
                    market_length INTEGER NOT NULL,
                    market_name TEXT NOT NULL,
                    player_prop INTEGER NOT NULL,
                    sport_id TEXT NOT NULL,
                    handicap DOUBLE PRECISION,
                    period TEXT,
                    market_type TEXT
                );

                CREATE TABLE IF NOT EXISTS outcome_catalog (
                    market_id TEXT NOT NULL,
                    outcome_id TEXT NOT NULL,
                    outcome_name TEXT NOT NULL,
                    PRIMARY KEY (market_id, outcome_id),
                    FOREIGN KEY (market_id) REFERENCES market_catalog(market_id)
                        ON DELETE CASCADE
                );
                """
            )

    def replace(self, markets: list[MarketDefinition]) -> None:
        self.initialize()
        with self.connect() as connection:
            for market in markets:
                connection.execute(
                    """
                    INSERT INTO market_catalog (
                        market_id, market_length, market_name, player_prop,
                        sport_id, handicap, period, market_type
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(market_id) DO UPDATE SET
                        market_length = excluded.market_length,
                        market_name = excluded.market_name,
                        player_prop = excluded.player_prop,
                        sport_id = excluded.sport_id,
                        handicap = excluded.handicap,
                        period = excluded.period,
                        market_type = excluded.market_type
                    """,
                    (
                        market.market_id,
                        market.market_length,
                        market.market_name,
                        int(market.player_prop),
                        str(market.sport_id),
                        market.handicap,
                        market.period,
                        market.market_type,
                    ),
                )
                for outcome in market.outcomes:
                    connection.execute(
                        """
                        INSERT INTO outcome_catalog (market_id, outcome_id, outcome_name)
                        VALUES (?, ?, ?)
                        ON CONFLICT(market_id, outcome_id) DO UPDATE SET
                            outcome_name = excluded.outcome_name
                        """,
                        (market.market_id, outcome.outcome_id, outcome.outcome_name),
                    )

    def metadata(self) -> dict[str, dict]:
        self.initialize()
        with self.connect() as connection:
            markets = connection.execute("SELECT * FROM market_catalog").fetchall()
            outcomes = connection.execute("SELECT * FROM outcome_catalog").fetchall()

        result = {str(row["market_id"]): dict(row) for row in markets}
        for row in outcomes:
            market = result.get(str(row["market_id"]))
            if market is not None:
                market.setdefault("outcomes", {})[str(row["outcome_id"])] = row["outcome_name"]
        return result
