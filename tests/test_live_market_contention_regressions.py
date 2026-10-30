"""Regression coverage for PostgreSQL startup DDL and batched candle writes."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.autonomous import AutonomousResearch
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.database import PostgresDatabase, _Connection, _postgres_sql
from parlay_bot.storage.hot import SQLiteHotStore


class RecordingPostgres:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str, params: tuple = ()) -> None:
        self.statements.append(statement)


def test_postgres_schema_script_takes_transaction_lock_first() -> None:
    fake = RecordingPostgres()
    conn = _Connection(fake, "postgres")
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS watched (id INTEGER);"
        "CREATE INDEX IF NOT EXISTS watched_idx ON watched (id);"
    )
    assert fake.statements == [
        "SELECT pg_advisory_xact_lock(72649113001)",
        "CREATE TABLE IF NOT EXISTS watched (id INTEGER)",
        "CREATE INDEX IF NOT EXISTS watched_idx ON watched (id)",
    ]


def _batch(*, length: int, changed: bool = False) -> MarketBatch:
    observed = datetime(2026, 10, 30, 18, 0, tzinfo=UTC)
    candles = []
    for n in range(length):
        opened = observed - timedelta(minutes=5 * (length - n + 2))
        price = 100.0 + n
        if n == 0 and changed:
            price += 1.0
        candles.append(MarketCandle(
            venue="hyperliquid", contract="BTC", quote="USD",
            interval_minutes=5, opened_at=opened,
            closed_at=opened + timedelta(minutes=5) - timedelta(milliseconds=1),
            observed_at=observed, open_price=price, high_price=price + 2,
            low_price=price - 2, close_price=price, volume_base=1.0, trades=1,
        ))
    return MarketBatch((), tuple(candles), 5, observed)


def test_bulk_history_preserves_idempotence_and_immutable_conflicts(tmp_path) -> None:
    market = PerpetualMarketStore(SQLiteHotStore(tmp_path / "market.sqlite"))
    initial = market.ingest(_batch(length=235))
    assert initial.inserted_candles == 235
    assert initial.unchanged_candles == 0
    same = market.ingest(_batch(length=235))
    assert same.inserted_candles == 0
    assert same.unchanged_candles == 235
    changed = market.ingest(_batch(length=235, changed=True))
    assert changed.inserted_candles == 0
    assert changed.conflicted_candles == 1
    assert changed.unchanged_candles == 234
    prices = market.recent_candles(
        venue="hyperliquid", contract="BTC", quote="USD",
        interval_minutes=5, count=235,
    )
    assert len(prices) == 235
    assert prices[0]["open_price"] == 100.0


def test_postgres_adapter_escapes_literal_percent_before_binding() -> None:
    # The exact query used by Auto-Hunt research suppression formerly
    # raised psycopg.ProgrammingError when given a bound user id.
    sql = (
        "SELECT 1 FROM rival_hunt_delivery WHERE user_id=? "
        "AND forecast_key LIKE 'qualified|%'"
    )
    assert _postgres_sql(sql) == (
        "SELECT 1 FROM rival_hunt_delivery WHERE user_id=%s "
        "AND forecast_key LIKE 'qualified|%%'"
    )
    assert _postgres_sql("SELECT (?::int % ?::int)") == (
        "SELECT (%s::int %% %s::int)"
    )


def test_postgres_literal_percent_queries_execute_in_real_backend() -> None:
    dsn = os.environ.get("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("A disposable PostgreSQL test database is not configured")
    database = PostgresDatabase(dsn, max_size=1)
    try:
        with database.connect() as conn:
            match = conn.execute(
                "SELECT ? AS label WHERE ? LIKE 'qualified|%'",
                ("qualified", "qualified|abc"),
            ).fetchone()
            assert match["label"] == "qualified"
            mod = conn.execute(
                "SELECT (?::int % ?::int) AS remainder", (10, 3),
            ).fetchone()
            assert mod["remainder"] == 1
    finally:
        database.close()


def test_autonomous_schema_is_initialized_once_even_across_threads(tmp_path, monkeypatch):
    store = SQLiteHotStore(tmp_path / "autonomous.sqlite")
    market = PerpetualMarketStore(store)
    research = AutonomousResearch(
        market, ai=None, venues=("hyperliquid",), assets=("BTC",),
        auto_ai=False,
    )
    original_connect = store.connect
    calls = []

    def counted_connect():
        calls.append(1)
        return original_connect()

    monkeypatch.setattr(store, "connect", counted_connect)
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: research.initialize(), range(8)))
    first = len(calls)
    assert first > 0
    research.initialize()
    assert len(calls) == first
