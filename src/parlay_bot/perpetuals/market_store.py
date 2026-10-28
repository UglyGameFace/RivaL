"""Immutable, venue-specific perpetual observations in the existing RivaL store."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
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
                CREATE TABLE IF NOT EXISTS perp_ai_research (
                    asset TEXT NOT NULL,
                    model TEXT NOT NULL,
                    evidence_sha256 TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    thesis TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    source_count INTEGER NOT NULL,
                    PRIMARY KEY (asset,model,evidence_sha256)
                );
                CREATE TABLE IF NOT EXISTS perp_ai_quota (
                    model TEXT NOT NULL,
                    utc_day TEXT NOT NULL,
                    used INTEGER NOT NULL,
                    last_called_at TEXT NOT NULL,
                    blocked_until TEXT,
                    PRIMARY KEY (model,utc_day)
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
                or not isinstance(item.trades, int) or item.trades < -1
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
        as_of: datetime | None = None,
    ) -> list[dict]:
        """Retrieve a venue-specific history, optionally as it was actually known.

        Point-in-time callers must request `as_of`: a backfilled candle is not
        visible before its first real observation, even if its close is older.
        Filtering occurs BEFORE LIMIT, preventing backfills from displacing
        earlier usable rows.
        """
        if not 1 <= count <= 5000:
            raise ValueError("recent candle count out of bounds")
        self.initialize()
        with self.store.connect() as conn:
            if as_of is None:
                rows = conn.execute(
                    """SELECT * FROM perp_market_candles
                       WHERE venue=? AND contract=? AND quote=? AND interval_minutes=?
                       ORDER BY opened_at DESC LIMIT ?""",
                    (venue, contract, quote, interval_minutes, count),
                ).fetchall()
            else:
                instant = utc(as_of).isoformat()
                rows = conn.execute(
                    """SELECT * FROM perp_market_candles
                       WHERE venue=? AND contract=? AND quote=? AND interval_minutes=?
                         AND closed_at<? AND first_observed_at<=?
                       ORDER BY opened_at DESC LIMIT ?""",
                    (venue, contract, quote, interval_minutes, instant, instant, count),
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

    def cached_ai_research(
        self, *, asset: str, model: str, evidence_sha256: str,
    ) -> dict | None:
        """Only a previously saved response for the *exact same* evidence."""
        self.initialize()
        with self.store.connect() as conn:
            item = conn.execute(
                """SELECT * FROM perp_ai_research
                   WHERE asset=? AND model=? AND evidence_sha256=?""",
                (asset, model, evidence_sha256),
            ).fetchone()
        return dict(item) if item is not None else None

    def ai_request_status(
        self, *, model: str, now: datetime, daily_max: int,
        cooldown_seconds: int,
    ) -> dict:
        """Explain stored Gemini request controls without consuming quota.

        The limit here is RivaL's own conservative cap, not Google's project
        quota. The same persisted row is used by the atomic reservation path.
        """
        if not model or daily_max < 1 or cooldown_seconds < 60:
            raise ValueError("invalid AI budget policy")
        instant = utc(now)
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT used,last_called_at,blocked_until
                   FROM perp_ai_quota WHERE model=? AND utc_day=?""",
                (model, instant.date().isoformat()),
            ).fetchone()
            previous = conn.execute(
                """SELECT last_called_at,blocked_until
                   FROM perp_ai_quota WHERE model=? AND utc_day<?
                   ORDER BY utc_day DESC LIMIT 1""",
                (model, instant.date().isoformat()),
            ).fetchone()
        used = int(row["used"]) if row is not None else 0
        checks: list[tuple[str, datetime]] = []
        day_reset = (instant + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        # An error at 23:59 UTC must remain blocked at 00:00 UTC.
        # Today's request count resets, but the provider's pause does not.
        for record in (row, previous):
            if record is None:
                continue
            if record["blocked_until"]:
                blocked = utc(datetime.fromisoformat(str(record["blocked_until"])))
                if blocked > instant:
                    checks.append(("provider_error_pause", blocked))
            if record["last_called_at"]:
                last_call = utc(datetime.fromisoformat(str(record["last_called_at"])))
                next_call = last_call + timedelta(seconds=cooldown_seconds)
                if next_call > instant:
                    checks.append(("minimum_spacing", next_call))
        if used >= daily_max:
            checks.append(("daily_budget_exhausted", day_reset))
        # The earliest safe retry is after *all* active restrictions expire.
        retry_at = max((when for _, when in checks), default=None)
        return {
            "model": model,
            "requests_today": used,
            "rival_daily_limit": daily_max,
            "reasons": tuple(reason for reason, _ in checks),
            "retry_at": retry_at,
            "ready": not checks,
        }

    def reserve_ai_request(
        self, *, model: str, now: datetime, daily_max: int,
        cooldown_seconds: int,
    ) -> bool:
        """One atomic quota reservation across processes and bot restarts."""
        if daily_max < 1 or cooldown_seconds < 60:
            raise ValueError("invalid AI budget policy")
        instant = utc(now)
        stamp = instant.isoformat()
        cutoff = (instant - timedelta(seconds=cooldown_seconds)).isoformat()
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                """INSERT INTO perp_ai_quota
                   (model,utc_day,used,last_called_at,blocked_until)
                   SELECT ?,?,1,?,NULL
                   WHERE NOT EXISTS (
                       SELECT 1 FROM perp_ai_quota AS prior
                       WHERE prior.model=? AND prior.utc_day<?
                         AND (prior.last_called_at>? OR prior.blocked_until>?)
                   )
                   ON CONFLICT(model,utc_day) DO UPDATE SET
                     used=perp_ai_quota.used+1,
                     last_called_at=excluded.last_called_at
                   WHERE perp_ai_quota.used < ?
                     AND perp_ai_quota.last_called_at <= ?
                     AND (perp_ai_quota.blocked_until IS NULL OR
                          perp_ai_quota.blocked_until <= ?)
                     AND NOT EXISTS (
                       SELECT 1 FROM perp_ai_quota AS prior
                       WHERE prior.model=? AND prior.utc_day<?
                         AND (prior.last_called_at>? OR prior.blocked_until>?)
                     )
                   RETURNING used""",
                (
                    model, instant.date().isoformat(), stamp,
                    model, instant.date().isoformat(), cutoff, stamp,
                    daily_max, cutoff, stamp,
                    model, instant.date().isoformat(), cutoff, stamp,
                ),
            ).fetchone()
        return row is not None

    def pause_ai_requests(
        self, *, model: str, now: datetime, seconds: int = 3600,
    ) -> None:
        """Persist a model-wide circuit breaker for upstream quota denials."""
        if seconds < 60:
            raise ValueError("invalid AI cooldown")
        instant = utc(now)
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE perp_ai_quota SET blocked_until=?
                   WHERE model=? AND utc_day=?""",
                ((instant + timedelta(seconds=seconds)).isoformat(),
                 model, instant.date().isoformat()),
            )

    def save_ai_research(
        self, *, asset: str, model: str, evidence_sha256: str,
        issued_at: datetime, direction: str, confidence: str,
        thesis: str, risk: str, source_count: int,
    ) -> None:
        """Append-only Gemini paper trail; cannot rewrite existing analyses."""
        if (asset not in ("BTC", "ETH", "SOL")
            or direction not in ("up", "down", "uncertain")
            or confidence not in ("low", "medium", "high")
            or len(evidence_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in evidence_sha256)
            or source_count < 3 or not thesis or not risk):
            raise ValueError("invalid AI research")
        args = (
            asset, model, evidence_sha256, utc(issued_at).isoformat(),
            direction, confidence, thesis[:400], risk[:250], source_count,
        )
        self.initialize()
        with self.store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_ai_research
                   (asset,model,evidence_sha256,issued_at,direction,
                    confidence,thesis,risk,source_count)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(asset,model,evidence_sha256) DO NOTHING""",
                args,
            )
