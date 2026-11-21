"""Immutable, venue-specific perpetual observations in the existing RivaL store."""
from __future__ import annotations

import logging
import math
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from parlay_bot.perpetuals.fusion import utc
from parlay_bot.perpetuals.hyperliquid import MarketBatch

if TYPE_CHECKING:
    from parlay_bot.storage.hot import RelationalHotStore


_LOG = logging.getLogger(__name__)


class CandleConflictError(RuntimeError):
    """Closed historical data changed unexpectedly.

    Retained for import compatibility. ingest no longer raises it: a
    conflicting bar is skipped and counted instead, so one revised bar
    cannot reject a whole batch every cycle (the observed kucoin-ETH /
    deribit-ETH+SOL pattern, which the settlement margin alone did not
    stop because exchanges revise bars beyond the newest one).
    """


@dataclass(frozen=True)
class MarketWriteResult:
    inserted_candles: int
    unchanged_candles: int
    context_count: int
    conflicted_candles: int = 0


class RequesterAIBudgetExhausted(Exception):
    """Internal control-flow rollback: do not spend shared quota on denied users."""


def _conflict_detail(fields, stored, incoming, *, closed, original_seen,
                     incoming_seen) -> str:
    """'close_price 101.2->101.4 ...; seen +12s vs +95s after close' for one bar."""
    def after_close(seen) -> str:
        try:
            delta = (utc(datetime.fromisoformat(str(seen)))
                     - utc(datetime.fromisoformat(str(closed)))).total_seconds()
            return f"+{delta:.0f}s"
        except (TypeError, ValueError, OverflowError):
            return "?"

    changed = " ".join(
        f"{name} {old}->{new}" for name, old, new in zip(fields, stored, incoming, strict=True)
        if old != new
    )
    return (f"{changed}; seen {after_close(original_seen)} (original) vs "
            f"{after_close(incoming_seen)} (incoming) after close")[:400]


class PerpetualMarketStore:
    def __init__(self, store: RelationalHotStore) -> None:
        self.store = store
        # Schema creation is a startup operation, not a per-candle read.
        # Shared by the collector, research worker, and Discord signal readers.
        self._schema_lock = threading.Lock()
        self._schema_ready = False
        self._forward_schema_lock = threading.Lock()
        self._forward_schema_ready = False
        self.live_model_checkpoints = None
        self.regime_live_checkpoints = None
        # The Model Lab (model_lab.ModelLab), shared by the forecast cycle
        # and the bot's review loop; created on first use.
        self.model_lab = None
        self.historical_challengers = None
        self.pooled_models = None  # set by ForwardAccuracyMonitor or the bot
        # Optional, read-only evidence dependencies installed by the bot.
        self.research_candidate_registry = None
        self.research_history_auditor = None
        self.research_forward_auditor = None
        self.research_spike_watcher = None

    def initialize(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
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
                    CREATE INDEX IF NOT EXISTS idx_perp_market_candles_observed
                        ON perp_market_candles
                           (interval_minutes,first_observed_at);
                    -- Lighter revises the base-volume value on settled
                    -- candles. Keep the original point-in-time source row
                    -- immutable. Retain a separate, bounded per-bar latest
                    -- revision for diagnosis, never backdate it to issuance.
                    CREATE TABLE IF NOT EXISTS perp_lighter_volume_revisions (
                        venue TEXT NOT NULL,
                        contract TEXT NOT NULL,
                        quote TEXT NOT NULL,
                        interval_minutes INTEGER NOT NULL,
                        opened_at TEXT NOT NULL,
                        original_volume_base DOUBLE PRECISION NOT NULL,
                        last_volume_base DOUBLE PRECISION NOT NULL,
                        last_observed_at TEXT NOT NULL,
                        observation_count INTEGER NOT NULL,
                        PRIMARY KEY (venue,contract,quote,interval_minutes,opened_at)
                    );
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
                    CREATE TABLE IF NOT EXISTS perp_ai_requester_quota (
                        model TEXT NOT NULL,
                        utc_day TEXT NOT NULL,
                        requester_sha256 TEXT NOT NULL,
                        used INTEGER NOT NULL,
                        PRIMARY KEY (model,utc_day,requester_sha256)
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
            # Failures leave the flag unset so a later attempt can retry.
            self._schema_ready = True

    def ingest(self, batch: MarketBatch) -> MarketWriteResult:
        """Atomically insert closed candles; retries cannot rewrite historical prices.

        A bar whose stored values differ from the batch is skipped, not
        overwritten, and counted in conflicted_candles: one exchange
        revision must not reject the other bars in the batch.
        """
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
        conflicted = 0
        # Retain all conflict evidence without flooding logs with one warning
        # per rejected Lighter candle every five minutes.
        conflict_fields: dict[tuple[str, str], Counter[str]] = {}
        conflict_counts: Counter[tuple[str, str]] = Counter()
        conflict_samples: dict[tuple[str, str], str] = {}
        # First conflicting bar per stream: original vs incoming values and
        # how long after the bar closed each copy was observed. Public market
        # numbers only; this is what tells a late exchange revision apart
        # from a parsing or second-collector difference.
        conflict_details: dict[tuple[str, str], str] = {}
        lighter_volume_only: Counter[tuple[str, str]] = Counter()
        # One latest receipt per candle, not a new row every five minutes.
        lighter_revisions: dict[tuple, tuple] = {}
        bounds: dict[tuple[str, str, str, int], tuple[str, str]] = {}
        for candle in batch.candles:
            group = (
                candle.venue, candle.contract, candle.quote,
                candle.interval_minutes,
            )
            opened_iso = utc(candle.opened_at).isoformat()
            if group not in bounds:
                bounds[group] = (opened_iso, opened_iso)
            else:
                low, high = bounds[group]
                bounds[group] = (min(low, opened_iso), max(high, opened_iso))
        with self.store.connect() as conn:
            existing: dict[tuple[str, str, str, int, str], dict] = {}
            for (venue, contract, quote, interval), (low, high) in bounds.items():
                rows = conn.execute(
                    """SELECT opened_at,closed_at,open_price,high_price,low_price,
                              close_price,volume_base,trades,first_observed_at
                       FROM perp_market_candles
                       WHERE venue=? AND contract=? AND quote=?
                         AND interval_minutes=? AND opened_at>=? AND opened_at<=?""",
                    (venue, contract, quote, interval, low, high),
                ).fetchall()
                for row in rows:
                    existing[
                        (venue, contract, quote, interval, str(row["opened_at"]))
                    ] = dict(row)
            # Group missing rows into bounded inserts. Hundreds of individual
            # network round trips held a PostgreSQL connection for a minute
            # during historical recovery, starving live signal readers.
            missing: dict[tuple, tuple] = {}

            fields = (
                "closed_at", "open_price", "high_price", "low_price",
                "close_price", "volume_base", "trades",
            )

            def stored_values(old: dict) -> tuple:
                return (
                    str(old["closed_at"]), float(old["open_price"]),
                    float(old["high_price"]), float(old["low_price"]),
                    float(old["close_price"]), float(old["volume_base"]),
                    int(old["trades"]),
                )

            def values_match(old: dict, values: tuple) -> bool:
                return stored_values(old) == (values[5], *values[7:])

            def report_conflict(values: tuple, old: dict | None) -> None:
                nonlocal conflicted
                conflicted += 1
                group = (str(values[0]), str(values[1]))
                conflict_counts[group] += 1
                reasons = (
                    (name for name, saved, incoming in zip(
                        fields, stored_values(old), (values[5], *values[7:]),
                        strict=True,
                    ) if saved != incoming)
                    if old is not None
                    else ("duplicate_in_batch",)
                )
                changed = tuple(reasons)
                conflict_fields.setdefault(group, Counter()).update(changed)
                conflict_samples.setdefault(group, str(values[4]))
                if old is not None and group not in conflict_details:
                    conflict_details[group] = _conflict_detail(
                        fields, stored_values(old), (values[5], *values[7:]),
                        closed=values[5], original_seen=old.get("first_observed_at"),
                        incoming_seen=values[6],
                    )
                if old is not None and values[0] == "lighter" and changed == ("volume_base",):
                    lighter_volume_only[group] += 1
                    lighter_revisions[values[:5]] = (
                        *values[:5], float(old["volume_base"]),
                        float(values[11]), str(values[6]), 1,
                    )

            for candle in batch.candles:
                values = (
                    candle.venue, candle.contract, candle.quote,
                    candle.interval_minutes, utc(candle.opened_at).isoformat(),
                    utc(candle.closed_at).isoformat(),
                    utc(candle.observed_at).isoformat(),
                    candle.open_price, candle.high_price, candle.low_price,
                    candle.close_price, candle.volume_base, candle.trades,
                )
                key = values[:5]
                old = existing.get(key)
                if old is not None:
                    if values_match(old, values):
                        unchanged += 1
                    else:
                        report_conflict(values, old)
                    continue
                if key in missing:
                    # A provider must not smuggle contradictory duplicates
                    # into a single page; preserve the first valid bar.
                    if missing[key] != values:
                        report_conflict(values, None)
                    else:
                        unchanged += 1
                    continue
                missing[key] = values

            # Both SQLite and PostgreSQL support INSERT ... RETURNING.
            # One short bounded statement replaces up to 100 round trips,
            # while the existing surrounding transaction remains atomic.
            pending = list(missing.values())
            for offset in range(0, len(pending), 100):
                chunk = pending[offset:offset + 100]
                placeholders = ", ".join(
                    "(" + ",".join("?" for _ in range(13)) + ")"
                    for _ in chunk
                )
                params = tuple(item for values in chunk for item in values)
                rows = conn.execute(
                    "INSERT INTO perp_market_candles "
                    "(venue,contract,quote,interval_minutes,opened_at,"
                    "closed_at,first_observed_at,open_price,high_price,"
                    "low_price,close_price,volume_base,trades) VALUES "
                    + placeholders
                    + " ON CONFLICT (venue,contract,quote,interval_minutes,opened_at)"
                    " DO NOTHING RETURNING venue,contract,quote,interval_minutes,opened_at",
                    params,
                ).fetchall()
                inserted_keys = {
                    (row["venue"], row["contract"], row["quote"],
                     row["interval_minutes"], row["opened_at"])
                    for row in rows
                }
                inserted += len(inserted_keys)
                # Another writer can win the conflict between pre-read and
                # insert. Compare its row rather than silently claiming a win.
                for values in chunk:
                    if values[:5] in inserted_keys:
                        continue
                    fetched = conn.execute(
                        "SELECT closed_at,open_price,high_price,low_price,"
                        "close_price,volume_base,trades FROM perp_market_candles "
                        "WHERE venue=? AND contract=? AND quote=? "
                        "AND interval_minutes=? AND opened_at=?",
                        values[:5],
                    ).fetchone()
                    if fetched is not None and values_match(fetched, values):
                        unchanged += 1
                    else:
                        report_conflict(values, dict(fetched) if fetched else None)
            # A current revised volume must not rewrite what the model
            # first saw. Bulk update a separate, explicitly retrospective
            # ledger, with no change to original candle or observation time.
            pending_revisions = list(lighter_revisions.values())
            for offset in range(0, len(pending_revisions), 100):
                chunk = pending_revisions[offset:offset + 100]
                placeholders = ", ".join("(" + ",".join("?" for _ in range(9)) + ")"
                                         for _ in chunk)
                conn.execute(
                    "INSERT INTO perp_lighter_volume_revisions "
                    "(venue,contract,quote,interval_minutes,opened_at,"
                    "original_volume_base,last_volume_base,last_observed_at,"
                    "observation_count) VALUES " + placeholders
                    + " ON CONFLICT (venue,contract,quote,interval_minutes,opened_at)"
                    " DO UPDATE SET last_volume_base=excluded.last_volume_base,"
                    " last_observed_at=excluded.last_observed_at,"
                    " observation_count=perp_lighter_volume_revisions.observation_count+1"
                    " WHERE perp_lighter_volume_revisions.last_observed_at"
                    "<=excluded.last_observed_at",
                    tuple(part for record in chunk for part in record),
                )
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
        # One grouped warning distinguishes revised volume from revised
        # prices; no original record is overwritten or silently relabeled.
        for (venue, contract), changed in conflict_fields.items():
            # Expected volume-only Lighter amendments are persisted as
            # observations, not reported as broken prices or a failed cycle.
            level = (
                logging.INFO if lighter_volume_only[(venue, contract)]
                == conflict_counts[(venue, contract)] else logging.WARNING
            )
            _LOG.log(
                level,
                "RivaL immutable candle conflicts skipped venue=%s "
                "contract=%s bars=%d changed_fields=%s "
                "example_opened_at=%s; original bars retained; "
                "lighter_volume_revisions=%d example=%s",
                venue, contract, conflict_counts[(venue, contract)],
                ",".join(
                    f"{name}:{count}" for name, count in sorted(changed.items())
                ),
                conflict_samples[(venue, contract)],
                lighter_volume_only[(venue, contract)],
                conflict_details.get((venue, contract), "duplicate_in_batch"),
            )
        return MarketWriteResult(
            inserted, unchanged, len(batch.contexts), conflicted,
        )

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
