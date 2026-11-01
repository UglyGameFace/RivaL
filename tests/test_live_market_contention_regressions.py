"""Regression coverage for PostgreSQL startup DDL and batched candle writes."""
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.autonomous import AutonomousResearch
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.database import PostgresDatabase, _Connection, _postgres_sql
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore


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


def test_lighter_revision_is_aggregated_and_reports_real_changed_fields(
    tmp_path, caplog,
) -> None:
    market = PerpetualMarketStore(SQLiteHotStore(tmp_path / "lighter.sqlite"))
    original = _batch(length=6)
    market.ingest(original)
    # Same OHLC close, different later-exchange-reported volume. Never
    # overwrite immutable evidence, but don't log six misleading warnings.
    revised = MarketBatch(
        original.contexts,
        tuple(replace(c, volume_base=c.volume_base + 0.25)
              for c in original.candles),
        original.interval_minutes, original.observed_at,
    )
    with caplog.at_level(logging.WARNING):
        result = market.ingest(revised)
    assert result.inserted_candles == 0
    assert result.conflicted_candles == 6
    messages = [r.message for r in caplog.records
                if "immutable candle conflicts skipped" in r.message]
    assert len(messages) == 1
    assert "bars=6" in messages[0]
    assert "volume_base:6" in messages[0]
    assert "close_price" not in messages[0]
    stored = market.recent_candles(
        venue="hyperliquid", contract="BTC", quote="USD",
        interval_minutes=5, count=6,
    )
    assert len(stored) == 6
    assert all(float(c["volume_base"]) == 1.0 for c in stored)


def test_lighter_volume_revisions_are_separate_and_cannot_rewrite_causal_history(
    tmp_path, caplog,
) -> None:
    store = SQLiteHotStore(tmp_path / "lighter_revisions.sqlite")
    market = PerpetualMarketStore(store)
    original = _batch(length=3)
    original = MarketBatch(
        (),
        tuple(replace(c, venue="lighter", contract="BTC", quote="USD")
              for c in original.candles),
        5, original.observed_at,
    )
    assert market.ingest(original).inserted_candles == 3

    newer_observed = original.observed_at + timedelta(minutes=5)
    revised = MarketBatch(
        (),
        tuple(replace(c, volume_base=2.25, observed_at=newer_observed)
              for c in original.candles),
        5, newer_observed,
    )
    with caplog.at_level(logging.WARNING):
        result = market.ingest(revised)
    assert result.conflicted_candles == 3
    assert not any("immutable candle conflicts skipped" in x.message
                   for x in caplog.records)
    rows = market.recent_candles(
        venue="lighter", contract="BTC", quote="USD",
        interval_minutes=5, count=3,
    )
    assert all(row["volume_base"] == 1.0 for row in rows)
    with store.connect() as conn:
        evidence = conn.execute(
            "SELECT original_volume_base,last_volume_base,observation_count,"
            "last_observed_at FROM perp_lighter_volume_revisions "
            "ORDER BY opened_at"
        ).fetchall()
    assert len(evidence) == 3
    assert all(row["original_volume_base"] == 1.0 for row in evidence)
    assert all(row["last_volume_base"] == 2.25 for row in evidence)
    assert all(row["observation_count"] == 1 for row in evidence)

    later_observed = newer_observed + timedelta(minutes=5)
    revised_again = MarketBatch(
        (),
        tuple(replace(c, volume_base=2.75, observed_at=later_observed)
              for c in original.candles),
        5, later_observed,
    )
    assert market.ingest(revised_again).conflicted_candles == 3
    with store.connect() as conn:
        evidence = conn.execute(
            "SELECT last_volume_base,observation_count,last_observed_at "
            "FROM perp_lighter_volume_revisions"
        ).fetchall()
    assert all(row["last_volume_base"] == 2.75 for row in evidence)
    assert all(row["observation_count"] == 2 for row in evidence)
    assert all(row["last_observed_at"] == later_observed.isoformat()
               for row in evidence)
    assert all(row["volume_base"] == 1.0 for row in market.recent_candles(
        venue="lighter", contract="BTC", quote="USD", interval_minutes=5, count=3,
    ))


def test_lighter_price_revisions_remain_warnings_and_never_change_history(
    tmp_path, caplog,
) -> None:
    store = SQLiteHotStore(tmp_path / "lighter_price.sqlite")
    market = PerpetualMarketStore(store)
    original = _batch(length=2)
    original = MarketBatch(
        (),
        tuple(replace(c, venue="lighter", contract="BTC")
              for c in original.candles),
        5, original.observed_at,
    )
    market.ingest(original)
    first = original.candles[0]
    revised = MarketBatch(
        (), (replace(first, close_price=first.close_price + 0.25),),
        5, original.observed_at,
    )
    with caplog.at_level(logging.WARNING):
        result = market.ingest(revised)
    assert result.conflicted_candles == 1
    assert any("close_price:1" in x.message for x in caplog.records)
    with store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM perp_lighter_volume_revisions"
        ).fetchone()["n"] == 0


def test_postgres_lighter_volume_revision_bulk_upsert_preserves_original() -> None:
    dsn = os.environ.get("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Disposable PostgreSQL integration database unavailable")
    store = PostgresHotStore(dsn)
    market = PerpetualMarketStore(store)
    contract = "BTC-LIGHTER-VOLUME-CI"
    original = _batch(length=2)
    original = MarketBatch(
        (), tuple(replace(c, venue="lighter", contract=contract)
                  for c in original.candles),
        5, original.observed_at,
    )
    try:
        assert market.ingest(original).inserted_candles == 2
        later = original.observed_at + timedelta(minutes=5)
        incoming = MarketBatch(
            (), tuple(replace(c, volume_base=9.0, observed_at=later)
                      for c in original.candles),
            5, later,
        )
        assert market.ingest(incoming).conflicted_candles == 2
        with store.connect() as conn:
            revised = conn.execute(
                "SELECT original_volume_base,last_volume_base,"
                "observation_count FROM perp_lighter_volume_revisions "
                "WHERE venue='lighter' AND contract=?",
                (contract,),
            ).fetchall()
            saved = conn.execute(
                "SELECT volume_base FROM perp_market_candles "
                "WHERE venue='lighter' AND contract=?",
                (contract,),
            ).fetchall()
        assert len(revised) == len(saved) == 2
        assert all(row["original_volume_base"] == 1.0
                   and row["last_volume_base"] == 9.0
                   and row["observation_count"] == 1 for row in revised)
        assert all(row["volume_base"] == 1.0 for row in saved)
    finally:
        with store.connect() as conn:
            conn.execute(
                "DELETE FROM perp_lighter_volume_revisions "
                "WHERE venue='lighter' AND contract=?", (contract,),
            )
            conn.execute(
                "DELETE FROM perp_market_candles "
                "WHERE venue='lighter' AND contract=?", (contract,),
            )
        store.close()
