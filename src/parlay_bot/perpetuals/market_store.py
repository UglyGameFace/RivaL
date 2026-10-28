"""Immutable, venue-specific perpetual observations in the existing RivaL store."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from parlay_bot.perpetuals.fusion import utc
from parlay_bot.perpetuals.hyperliquid import MarketBatch

if TYPE_CHECKING:
    from parlay_bot.storage.hot import RelationalHotStore


class CandleConflictError(RuntimeError):
    """Closed historical data changed unexpectedly; refuse a silent overwrite."""


@dataclass(frozen=True)
class MarketWriteResult:
    inserted_candles: int
    unchanged_candles: int
    context_count: int


class PerpetualMarketStore:
    def __init__(self, store: RelationalHotStore) -> None:
        self.store = store

    def initialize(self) -> None:
        # Isolated tables: no changes to the sports, archive, or prediction schemas.
        with self.store.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS perp_market_candles (
                    venue TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    quote TEXT NOT NULL,
                    interval_minutes INTEGER NOT NULL,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT NOT NULL,
                    first_observed_at TEXT NOT NULL,
                    open_price DOUBLE PRECISION NOT NULL,
                    high_price DOUBLE PRECISION NOT NULL,
                    low_price DOUBLE PRECISION NOT NULL,
                    close_price DOUBLE PRECISION NOT NULL,
                    volume_base DOUBLE PRECISION NOT NULL,
                    trades INTEGER NOT NULL,
                    PRIMARY KEY (venue,contract,quote,interval_minutes,opened_at)
                );
                CREATE INDEX IF NOT EXISTS idx_perp_market_candles_close
                    ON perp_market_candles
                       (venue,contract,quote,interval_minutes,closed_at);
                CREATE TABLE IF NOT EXISTS perp_feed_health (
                    venue TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_attempt_at TEXT NOT NULL,
                    last_success_at TEXT,
                    last_candle_close TEXT,
                    last_new_candles INTEGER NOT NULL DEFAULT 0,
                    total_new_candles INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (venue, asset)
                );
                CREATE TABLE IF NOT EXISTS perp_market_contexts (
                    venue TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    quote TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    mark_price DOUBLE PRECISION NOT NULL,
                    oracle_price DOUBLE PRECISION NOT NULL,
                    funding_rate DOUBLE PRECISION NOT NULL,
                    open_interest_base DOUBLE PRECISION NOT NULL,
                    PRIMARY KEY (venue,contract,quote,observed_at)
                );
                """
            )

    def ingest(self, batch: MarketBatch) -> MarketWriteResult:
        """Atomically insert closed candles; retries cannot rewrite historical prices."""
        if not batch.candles:
            raise ValueError("batch requires closed candles")
        keys = {(x.venue, x.contract, x.quote) for x in batch.contexts}
        if len(keys) != len(batch.contexts) or any(
            x.interval_minutes != batch.interval_minutes
            or (bool(keys) and (x.venue, x.contract, x.quote) not in keys)
            or utc(x.observed_at) != utc(batch.observed_at)
            or utc(x.opened_at) >= utc(x.closed_at)
            or utc(x.closed_at) >= utc(batch.observed_at)
            for x in batch.candles
        ):
            raise ValueError("inconsistent candle origin, timeframe, or timing")
        if any(utc(x.observed_at) != utc(batch.observed_at) for x in batch.contexts):
            raise ValueError("inconsistent market-context timestamp")
        for item in batch.candles:
            prices = (
                item.open_price, item.high_price, item.low_price, item.close_price,
            )
            if (
                not all(math.isfinite(x) and x > 0 for x in prices)
                or not math.isfinite(item.volume_base) or item.volume_base < 0
                or not isinstance(item.trades, int) or item.trades < 0
                or item.low_price > min(item.open_price, item.close_price)
                or item.high_price < max(item.open_price, item.close_price)
            ):
                raise ValueError("invalid candle OHLC, trade count, or base volume")
        for item in batch.contexts:
            if (
                not all(math.isfinite(x) for x in (
                    item.mark_price, item.oracle_price, item.funding_rate,
                    item.open_interest_base,
                ))
                or item.mark_price <= 0 or item.oracle_price <= 0
                or item.open_interest_base < 0
            ):
                raise ValueError("invalid market context")
        self.initialize()
        inserted = 0
        unchanged = 0
        with self.store.connect() as conn:
            for candle in batch.candles:
                values = (
                    candle.venue, candle.contract, candle.quote,
                    candle.interval_minutes, utc(candle.opened_at).isoformat(),
                    utc(candle.closed_at).isoformat(),
                    utc(candle.observed_at).isoformat(),
                    candle.open_price, candle.high_price, candle.low_price,
                    candle.close_price, candle.volume_base, candle.trades,
                )
                cursor = conn.execute(
                    """INSERT INTO perp_market_candles
                       (venue,contract,quote,interval_minutes,opened_at,
                        closed_at,first_observed_at,open_price,high_price,
                        low_price,close_price,volume_base,trades)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT (venue,contract,quote,interval_minutes,opened_at)
                       DO NOTHING""",
                    values,
                )
                if cursor.rowcount == 1:
                    inserted += 1
                    continue
                old = conn.execute(
                    """SELECT closed_at,open_price,high_price,low_price,
                              close_price,volume_base,trades
                       FROM perp_market_candles
                       WHERE venue=? AND contract=? AND quote=?
                         AND interval_minutes=? AND opened_at=?""",
                    values[:5],
                ).fetchone()
                if old is None or (
                    str(old["closed_at"]), float(old["open_price"]),
                    float(old["high_price"]), float(old["low_price"]),
                    float(old["close_price"]), float(old["volume_base"]),
                    int(old["trades"]),
                ) != (values[5], *values[7:]):
                    raise CandleConflictError(
                        "historical candle changed; manual investigation required"
                    )
                unchanged += 1
            for context in batch.contexts:
                conn.execute(
                    """INSERT INTO perp_market_contexts
                       (venue,contract,quote,observed_at,mark_price,
                        oracle_price,funding_rate,open_interest_base)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT (venue,contract,quote,observed_at)
                       DO NOTHING""",
                    (
                        context.venue, context.contract, context.quote,
                        utc(context.observed_at).isoformat(), context.mark_price,
                        context.oracle_price, context.funding_rate,
                        context.open_interest_base,
                    ),
                )
        return MarketWriteResult(inserted, unchanged, len(batch.contexts))

    def recent_candles(
        self, *, venue: str, contract: str, quote: str,
        interval_minutes: int, count: int = 200,
    ) -> list[dict]:
        if not 1 <= count <= 5000:
            raise ValueError("recent candle count out of bounds")
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM perp_market_candles
                   WHERE venue=? AND contract=? AND quote=? AND interval_minutes=?
                   ORDER BY opened_at DESC LIMIT ?""",
                (venue, contract, quote, interval_minutes, count),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def record_feed_health(
        self, *, venue: str, asset: str, status: str,
        attempted_at: datetime, new_candles: int, total_candles: int,
        last_candle_close: datetime | None = None,
    ) -> None:
        """Persist observable collection attempts without saving errors or secrets."""
        allowed = {
            "ok", "blocked", "rate_limited", "http_error", "timeout",
            "network_error", "invalid_market_data", "storage_error",
        }
        if (
            status not in allowed or not venue.strip() or not asset.strip()
            or min(new_candles, total_candles) < 0
            or new_candles > total_candles
        ):
            raise ValueError("invalid source health report")
        timestamp = utc(attempted_at).isoformat()
        if last_candle_close is not None and utc(last_candle_close) > utc(attempted_at):
            raise ValueError("future candle cannot be reported as completed")
        self.initialize()
        with self.store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_feed_health
                   (venue,asset,status,last_attempt_at,last_success_at,
                    last_candle_close,last_new_candles,total_new_candles,failure_count)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT (venue,asset) DO UPDATE SET
                     status=excluded.status,
                     last_attempt_at=excluded.last_attempt_at,
                     last_success_at=CASE WHEN excluded.status='ok'
                       THEN excluded.last_success_at
                       ELSE perp_feed_health.last_success_at END,
                     last_candle_close=CASE WHEN excluded.status='ok'
                       THEN excluded.last_candle_close
                       ELSE perp_feed_health.last_candle_close END,
                     last_new_candles=excluded.last_new_candles,
                     total_new_candles=perp_feed_health.total_new_candles
                       +excluded.last_new_candles,
                     failure_count=CASE WHEN excluded.status='ok'
                       THEN 0 ELSE perp_feed_health.failure_count+1 END""",
                (
                    venue, asset, status, timestamp,
                    timestamp if status == "ok" else None,
                    utc(last_candle_close).isoformat() if last_candle_close else None,
                    new_candles, new_candles, 0 if status == "ok" else 1,
                ),
            )

    def feed_health(self) -> list[dict]:
        """Summarize *actual* persisted ingestion for Android Discord inspection."""
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT venue,asset,status,last_attempt_at,last_success_at,
                          last_candle_close,last_new_candles,total_new_candles,
                          failure_count
                   FROM perp_feed_health ORDER BY venue,asset"""
            ).fetchall()
        return [dict(row) for row in rows]
