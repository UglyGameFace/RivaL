"""Immutable, provenance-preserving candles in RivaL's existing relational store."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from parlay_bot.perpetuals.fusion import utc
from parlay_bot.perpetuals.hyperliquid import PerpCandle

if TYPE_CHECKING:
    from parlay_bot.storage.hot import RelationalHotStore


class PerpCandleStore:
    def __init__(self, store: RelationalHotStore) -> None:
        self.store = store

    def initialize(self) -> None:
        with self.store.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS perp_candles (
                    venue TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    quote TEXT NOT NULL,
                    contract_kind TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT NOT NULL,
                    price_open DOUBLE PRECISION NOT NULL,
                    price_high DOUBLE PRECISION NOT NULL,
                    price_low DOUBLE PRECISION NOT NULL,
                    price_close DOUBLE PRECISION NOT NULL,
                    base_volume DOUBLE PRECISION NOT NULL,
                    trades INTEGER NOT NULL,
                    first_ingested_at TEXT NOT NULL,
                    PRIMARY KEY(venue, asset, quote, contract_kind, interval, opened_at)
                );
                CREATE INDEX IF NOT EXISTS perp_candles_history
                    ON perp_candles (asset, interval, closed_at);
                """
            )

    def ingest(
        self, candles: tuple[PerpCandle, ...], *, ingested_at: datetime | None = None
    ) -> int:
        """Insert once; fail loudly if an upstream silently revises old candle data."""
        observed = utc(ingested_at or datetime.now(UTC))
        if observed > datetime.now(UTC) + timedelta(seconds=5):
            raise ValueError("ingestion timestamp cannot be in the future")
        count = 0
        with self.store.connect() as conn:
            for candle in candles:
                if utc(candle.closed_at) >= observed:
                    raise ValueError("cannot ingest unfinished future candle")
                key = (
                    candle.venue, candle.asset, candle.quote, candle.contract_kind,
                    candle.interval, utc(candle.opened_at).isoformat()
                )
                data = (
                    utc(candle.closed_at).isoformat(),
                    candle.open, candle.high, candle.low, candle.close,
                    candle.base_volume, candle.trades,
                )
                existing = conn.execute(
                    """SELECT closed_at,price_open,price_high,price_low,price_close,
                              base_volume,trades
                       FROM perp_candles
                       WHERE venue=? AND asset=? AND quote=? AND contract_kind=?
                         AND interval=? AND opened_at=?""",
                    key,
                ).fetchone()
                if existing is not None:
                    old = (
                        existing["closed_at"], existing["price_open"],
                        existing["price_high"], existing["price_low"],
                        existing["price_close"], existing["base_volume"],
                        existing["trades"],
                    )
                    if old != data:
                        raise ValueError("upstream changed a previously stored candle")
                    continue
                written = conn.execute(
                    """INSERT INTO perp_candles(
                         venue,asset,quote,contract_kind,interval,opened_at,
                         closed_at,price_open,price_high,price_low,price_close,
                         base_volume,trades,first_ingested_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT (venue,asset,quote,contract_kind,interval,opened_at)
                       DO NOTHING""",
                    (*key, *data, observed.isoformat()),
                )
                count += written.rowcount
        return count

    def recent(
        self, *, venue: str, asset: str, quote: str,
        contract_kind: str, interval: str, limit: int = 100,
        as_of: datetime | None = None,
    ) -> list[dict]:
        if not 1 <= limit <= 500:
            raise ValueError("invalid history limit")
        cutoff = utc(as_of or datetime.now(UTC)).isoformat()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM perp_candles
                   WHERE venue=? AND asset=? AND quote=? AND contract_kind=?
                     AND interval=? AND closed_at<? AND first_ingested_at<=?
                   ORDER BY closed_at DESC LIMIT ?""",
                (venue, asset, quote, contract_kind, interval, cutoff, cutoff, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]


async def collect_hyperliquid(
    *, store: RelationalHotStore, coin: str = "BTC",
    interval: str = "5m", count: int = 48,
    client: object | None = None,
    now: datetime | None = None,
) -> tuple[int, int]:
    """Explicit opt-in collection entrypoint. Not wired to production startup."""
    from parlay_bot.perpetuals.hyperliquid import HyperliquidPublicCandles

    stamp = utc(now or datetime.now(UTC))
    adapter = HyperliquidPublicCandles(client=client)
    candles = await adapter.fetch_closed(
        coin=coin, interval=interval, count=count, requested_at=stamp
    )
    persistence = PerpCandleStore(store)
    persistence.initialize()
    written = persistence.ingest(candles, ingested_at=stamp)
    return len(candles), written
