"""Regression coverage for PostgreSQL startup DDL and batched candle writes."""
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.database import _Connection
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
