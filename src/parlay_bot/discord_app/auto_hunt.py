"""Opt-in Auto-Hunt for newly issued, evidence-screened crypto research watches.

This is NOT a betting executor or a proof of profitable edge. The authoritative
forecast ledger and venue-specific order book are reused. DB claims are written
before each DM attempt, so a timeout never creates duplicate betting alerts.

Two alert tiers, kept strictly separate:

* EARLY_RESEARCH: fresh, future-issued forecasts without proven trading
  edge. Never presented as actionable wagers, never with market terms
  framed as a bet. This is what the original forecast path delivered.
* QUALIFIED_WATCH: only the existing qualified WATCH verdict from the
  tap-time signal (a venue's own directional forecast whose own 7-day
  record beats the baseline), plus a fresh matching-source quote and
  sufficient time remaining. Shown with its validated quote. It is
  always presented as a qualified research watch. A source-matched
  simulated trading return is NOT the edge of a betting contract.

Users toggle research, qualified, and Polymarket alerts independently.
Legacy single-source preferences migrate additively: a "forecasts"
subscriber keeps research alerts, a "polymarket" subscriber keeps
Polymarket alerts, and qualified alerts start OFF for everyone, so no
existing subscriber suddenly receives a new alert class.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import discord

from parlay_bot.discord_app.opportunities import (
    recent_multi_watches,
    recent_watches,
)
from parlay_bot.discord_app.polymarket_watch import PolymarketNotice, PolymarketReader
from parlay_bot.discord_app.signals import (
    MIN_REMAINING,
    SignalStageTimeout,
    _venue_book,
    _venue_proof,
    model_strength,
    signal_embed,
)
from parlay_bot.perpetuals.live_direction import LiveShiftNotice
from parlay_bot.perpetuals.live_quotes import MAX_QUOTE_AGE
from parlay_bot.perpetuals.markets import LEGACY_ALERT_ASSETS, LEGACY_ALERT_HORIZONS
from parlay_bot.perpetuals.promotion import multi_summary, promoted_engine
from parlay_bot.storage.hot import RelationalHotStore

_LOG = logging.getLogger(__name__)
# Existing subscriptions never silently expand to XRP/60m notifications.
ASSETS = LEGACY_ALERT_ASSETS
HORIZONS = LEGACY_ALERT_HORIZONS
RETRY_DELAY = timedelta(seconds=20)
MAX_SAFE_RETRIES = 3
BOOK_VENUES = ("hyperliquid", "binance", "okx", "bitget")
MAX_USERS = 100
COOLDOWN = timedelta(seconds=120)
MAX_ISSUE_AGE = timedelta(seconds=90)
MAX_SPREAD_BPS = 25.0
# New research alerts are deliberately distinct from qualified trade signals.
# Don't silently discard every forecast just because live P&L is unproven.
FORECAST_ALERT_MAX_AGE = timedelta(minutes=3)
# Per-tier, per-user daily budgets. Research and qualified budgets are
# separate, so a busy research day can never crowd out a qualified window.
DAILY_CAP_PER_TIER = 12


def _cap_reached(sent: int, limit: int) -> bool:
    """0 is explicitly unlimited; unrelated platform safeguards still apply."""
    return limit > 0 and sent >= limit
# Qualified delivery keys are prefixed so one forecast can yield a research
# record and a qualified record without either claiming the other. Research
# and Polymarket keys stay unprefixed, so pre-reconcile delivery rows keep
# deduplicating exactly as before.
QUALIFIED_KEY_PREFIX = "qualified|"
FLIP_KEY_PREFIX = "flip|"
# A change must cross BOTH sides of the neutral region on separate,
# prospectively issued 5-minute reference windows. This is not a tick
# fluctuation, and 0.62 is a model score, not a win probability.
FLIP_UP = 0.62
FLIP_DOWN = 0.38
FLIP_MAX_PREVIOUS_AGE = timedelta(minutes=35)
TIERS = ("research", "qualified", "polymarket")


def _instant(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError("unverified timestamp")
    return stamp.astimezone(UTC)


@dataclass(frozen=True)
class HuntCandidate:
    asset: str
    horizon: int
    venue: str
    issued: datetime
    expires: datetime
    probability_up: float
    bid: float | None
    ask: float | None
    quote_at: datetime | None
    engine: str
    model_version: str
    screened: bool = False
    # Source-matched after-cost evidence, when the signal's shadow ledger
    # could supply it. None means unknown, never zero, never an edge.
    after_cost_bps: float | None = None
    after_cost_status: str | None = None
    previous_probability: float | None = None
    previous_issued: datetime | None = None

    @property
    def direction(self) -> str:
        return "UP" if self.probability_up > 0.5 else "DOWN"

    @property
    def strength(self) -> float:
        return max(self.probability_up, 1 - self.probability_up)

    @property
    def key(self) -> str:
        identity = (
            f"{self.engine}|{self.model_version}|{self.asset}|"
            f"{self.horizon}|{self.venue}|{self.issued.isoformat()}|"
            f"{self.expires.isoformat()}"
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @property
    def window(self) -> tuple[str, int, datetime]:
        """The market window this forecast covers, across venues."""
        return (self.asset, self.horizon, self.expires)


class AutoHuntStore:
    """Shared SQLite/PostgreSQL opt-in and durable at-most-once DM claims."""

    def __init__(self, store: RelationalHotStore) -> None:
        self.store = store

    def initialize(self) -> None:
        with self.store.connect() as conn:
            conn.executescript(
                """CREATE TABLE IF NOT EXISTS rival_hunt_subscriptions (
                       user_id TEXT PRIMARY KEY,
                       enabled INTEGER NOT NULL,
                       since_at TEXT NOT NULL,
                       last_sent_at TEXT
                   );
                   CREATE TABLE IF NOT EXISTS rival_hunt_delivery (
                       user_id TEXT NOT NULL,
                       forecast_key TEXT NOT NULL,
                       asset TEXT NOT NULL,
                       horizon INTEGER NOT NULL,
                       venue TEXT NOT NULL,
                       issued_at TEXT NOT NULL,
                       expires_at TEXT NOT NULL,
                       attempted_at TEXT NOT NULL,
                       delivered_at TEXT,
                       message_id TEXT,
                       state TEXT NOT NULL,
                       reviewed_at TEXT,
                       PRIMARY KEY(user_id, forecast_key)
                   );
                   CREATE INDEX IF NOT EXISTS rival_hunt_delivery_user
                       ON rival_hunt_delivery(user_id, attempted_at);
                   CREATE TABLE IF NOT EXISTS rival_hunt_source (
                       user_id TEXT PRIMARY KEY,
                       selected_source TEXT NOT NULL
                   );
                   CREATE TABLE IF NOT EXISTS rival_hunt_preferences (
                       user_id TEXT PRIMARY KEY,
                       assets TEXT NOT NULL DEFAULT 'BTC,ETH,SOL',
                       horizons TEXT NOT NULL DEFAULT '5,15',
                       side TEXT NOT NULL DEFAULT 'both',
                       max_ask_pct INTEGER NOT NULL DEFAULT 100,
                       min_remaining_seconds INTEGER NOT NULL DEFAULT 90,
                       direction_changes INTEGER NOT NULL DEFAULT 0,
                       live_shifts INTEGER NOT NULL DEFAULT 0,
                       daily_limit INTEGER NOT NULL DEFAULT 12
                   );
                   CREATE TABLE IF NOT EXISTS rival_hunt_tiers (
                       user_id TEXT PRIMARY KEY,
                       research INTEGER NOT NULL DEFAULT 0,
                       qualified INTEGER NOT NULL DEFAULT 0,
                       polymarket INTEGER NOT NULL DEFAULT 0,
                       updated_at TEXT NOT NULL
                   );"""
            )
            prefs_columns = self.store.database.column_names(
                conn, "rival_hunt_preferences",
            )
            if "live_shifts" not in prefs_columns:
                conn.execute(
                    "ALTER TABLE rival_hunt_preferences ADD COLUMN "
                    "live_shifts INTEGER NOT NULL DEFAULT 0"
                )
            if "daily_limit" not in prefs_columns:
                conn.execute(
                    "ALTER TABLE rival_hunt_preferences ADD COLUMN "
                    "daily_limit INTEGER NOT NULL DEFAULT 12"
                )
            # Additive, SQLite/PostgreSQL compatible outbox metadata. Legacy
            # attempted rows remain at-most-once and are never re-sent.
            columns = self.store.database.column_names(conn, "rival_hunt_delivery")
            for column, definition in (
                ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
                ("next_attempt_at", "TEXT"),
            ):
                if column not in columns:
                    conn.execute(
                        f"ALTER TABLE rival_hunt_delivery ADD COLUMN {column} {definition}"
                    )
            # A process crash after claiming network dispatch is ambiguous.
            # Preserve the record rather than replaying a possibly sent DM.
            conn.execute(
                """UPDATE rival_hunt_delivery SET state='unknown'
                   WHERE state IN ('sending','in_flight','attempted')
                     AND attempted_at<?""",
                ((datetime.now(UTC)-timedelta(minutes=2)).isoformat(),),
            )
            # Additive migration, idempotent: legacy single-source rows
            # become independent flags. "forecasts" keeps research alerts,
            # "polymarket" keeps Polymarket alerts, qualified starts OFF
            # for everyone so no existing subscriber gains a new alert
            # class from the migration alone. Subscribers without a source
            # row defaulted to "forecasts" under the old source() reader.
            conn.execute(
                """INSERT INTO rival_hunt_tiers
                       (user_id, research, qualified, polymarket, updated_at)
                   SELECT s.user_id,
                          CASE WHEN COALESCE(p.selected_source, 'forecasts')
                               = 'forecasts' THEN 1 ELSE 0 END,
                          0,
                          CASE WHEN p.selected_source = 'polymarket'
                               THEN 1 ELSE 0 END,
                          ?
                   FROM rival_hunt_subscriptions s
                   LEFT JOIN rival_hunt_source p ON p.user_id = s.user_id
                   ON CONFLICT(user_id) DO NOTHING""",
                (datetime.now(UTC).isoformat(),),
            )

    def set_tier(self, user_id: str, tier: str, enabled: bool) -> bool:
        if tier not in TIERS:
            raise ValueError("Unsupported alert tier")
        # A first explicit toggle must not silently drop flags the user
        # currently holds through the legacy fallback: seed the new row
        # from the effective flags, then apply the one change.
        current = self.tiers(user_id)
        current[tier] = enabled
        stamp = datetime.now(UTC).isoformat()
        with self.store.connect() as conn:
            conn.execute(
                """INSERT INTO rival_hunt_tiers
                       (user_id, research, qualified, polymarket, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       research = excluded.research,
                       qualified = excluded.qualified,
                       polymarket = excluded.polymarket,
                       updated_at = excluded.updated_at""",
                (user_id, 1 if current["research"] else 0,
                 1 if current["qualified"] else 0,
                 1 if current["polymarket"] else 0, stamp),
            )
        return enabled

    def tiers(self, user_id: str) -> dict:
        """Independent flags, with the legacy single-source view as fallback."""
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT research, qualified, polymarket
                   FROM rival_hunt_tiers WHERE user_id = ?""",
                (user_id,),
            ).fetchone()
            if row is not None:
                return {
                    "research": int(row["research"]) == 1,
                    "qualified": int(row["qualified"]) == 1,
                    "polymarket": int(row["polymarket"]) == 1,
                }
            source = conn.execute(
                """SELECT selected_source FROM rival_hunt_source
                   WHERE user_id = ?""",
                (user_id,),
            ).fetchone()
        legacy = str(source["selected_source"]) if source else "forecasts"
        return {
            "research": legacy == "forecasts",
            "qualified": False,
            "polymarket": legacy == "polymarket",
        }

    def preferences(self, user_id: str) -> dict:
        """Read per-user filters. Legacy followers retain unchanged scope."""
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT assets,horizons,side,max_ask_pct,
                          min_remaining_seconds,direction_changes,live_shifts,
                          daily_limit
                   FROM rival_hunt_preferences WHERE user_id=?""",
                (user_id,),
            ).fetchone()
        data = dict(row) if row else {}
        return {
            "assets": tuple(str(data.get("assets", "BTC,ETH,SOL")).split(",")),
            "horizons": tuple(int(x) for x in str(
                data.get("horizons", "5,15")
            ).split(",")),
            "side": str(data.get("side", "both")),
            "max_ask_pct": int(data.get("max_ask_pct", 100)),
            "min_remaining_seconds": int(data.get("min_remaining_seconds", 90)),
            "direction_changes": bool(data.get("direction_changes", 0)),
            "live_shifts": bool(data.get("live_shifts", 0)),
            "daily_limit": int(data.get("daily_limit", DAILY_CAP_PER_TIER)),
        }

    def set_preference(self, user_id: str, field: str, value: object) -> dict:
        """Strict allowlist, atomic updates; no widening from old opt-ins."""
        from parlay_bot.perpetuals.markets import ASSETS as ALL_ASSETS
        from parlay_bot.perpetuals.markets import HORIZONS as ALL_HORIZONS

        allowed = {
            "assets": {"BTC", "ETH", "SOL", "XRP"},
            "horizons": {"5", "15", "60"},
            "side": {"both", "up", "down"},
            "max_ask_pct": {25, 50, 75, 100},
            "min_remaining_seconds": {90, 180, 300},
            "direction_changes": {0, 1},
            "live_shifts": {0, 1},
            "daily_limit": {0, 12, 24, 48},
        }
        if field not in allowed:
            raise ValueError("Unknown Auto-Hunt preference")
        if field in ("assets", "horizons"):
            if not isinstance(value, (tuple, list)) or not value:
                raise ValueError("Select at least one market")
            values = tuple(dict.fromkeys(str(x) for x in value))
            if any(x not in allowed[field] for x in values):
                raise ValueError("Unsupported market selection")
            order = ALL_ASSETS if field == "assets" else tuple(
                str(x) for x in ALL_HORIZONS
            )
            stored: str | int = ",".join(x for x in order if x in values)
        else:
            if value not in allowed[field]:
                raise ValueError("Unsupported notification setting")
            stored = value
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO rival_hunt_preferences(user_id) VALUES (?) "
                "ON CONFLICT(user_id) DO NOTHING",
                (user_id,),
            )
            # field is verified against a fixed allowlist, never user SQL.
            conn.execute(
                f"UPDATE rival_hunt_preferences SET {field}=? WHERE user_id=?",
                (stored, user_id),
            )
        return self.preferences(user_id)

    def count_sent_today(self, user_id: str, tier: str, now: datetime) -> int:
        """Sent alerts today in one tier's budget. Research and qualified
        budgets are separate; Polymarket rows are identified by venue."""
        if tier not in (*TIERS, "flip", "live_shift"):
            raise ValueError("Unsupported alert tier")
        day_start = now.replace(hour=0, minute=0, second=0,
                                microsecond=0).isoformat()
        if tier == "live_shift":
            condition = "venue = 'live_trend'"
            params = (user_id, day_start)
        elif tier == "flip":
            condition = "forecast_key LIKE 'flip|%'"
            params = (user_id, day_start)
        elif tier == "qualified":
            condition = "forecast_key LIKE 'qualified|%'"
            params: tuple = (user_id, day_start)
        elif tier == "polymarket":
            condition = "venue = 'polymarket'"
            params = (user_id, day_start)
        else:
            condition = ("venue NOT IN ('polymarket','live_trend') AND "
                         "forecast_key NOT LIKE 'qualified|%' AND "
                         "forecast_key NOT LIKE 'flip|%'")
            params = (user_id, day_start)
        with self.store.connect() as conn:
            row = conn.execute(
                f"""SELECT COUNT(*) AS n FROM rival_hunt_delivery
                    WHERE user_id = ? AND state = 'sent'
                      AND attempted_at >= ? AND {condition}""",
                params,
            ).fetchone()
        return int(row["n"])

    def daily_sent_counts(
        self, user_id: str, *, now: datetime | None = None,
    ) -> dict[str, int]:
        """Today's confirmed alerts, using exactly the delivery cap clock."""
        return self.daily_sent_counts_many((user_id,), now=now)[user_id]

    def daily_sent_counts_many(
        self, user_ids: tuple[str, ...], *, now: datetime | None = None,
    ) -> dict[str, dict[str, int]]:
        """One indexed query for all subscribers, not one connection per user."""
        users = tuple(dict.fromkeys(user_ids))
        if not users:
            return {}
        if len(users) > MAX_USERS or any(not user for user in users):
            raise ValueError("Invalid Auto-Hunt daily budget request")
        clock = now or datetime.now(UTC)
        if clock.utcoffset() is None:
            raise ValueError("Daily budget requires an aware clock")
        midnight = clock.astimezone(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        placeholders = ",".join("?" for _ in users)
        # Keep the snapshot bounded to 5 rows per subscriber even when a
        # user has selected Unlimited. DB aggregation avoids hauling every
        # previously sent DM over the connection every 15 seconds.
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT user_id, "
                "CASE WHEN venue='polymarket' THEN 'polymarket' "
                "WHEN venue='live_trend' THEN 'live_shift' "
                "WHEN forecast_key LIKE 'qualified|%' THEN 'qualified' "
                "WHEN forecast_key LIKE 'flip|%' THEN 'flip' "
                "ELSE 'research' END AS tier, COUNT(*) AS n "
                "FROM rival_hunt_delivery "
                "WHERE user_id IN (" + placeholders + ") "
                "AND state='sent' AND attempted_at>=? AND attempted_at<? "
                "GROUP BY user_id, tier",
                (*users, midnight.isoformat(),
                 (midnight + timedelta(days=1)).isoformat()),
            ).fetchall()
        counters = {
            user: {tier: 0 for tier in (
                "research", "qualified", "polymarket", "flip", "live_shift",
            )} for user in users
        }
        for row in rows:
            counters[str(row["user_id"])][str(row["tier"])] = int(row["n"])
        return counters

    def has_qualified_sent_window(self, user_id: str, asset: str,
                                  horizon: int, expires: datetime) -> bool:
        """True when a qualified DM already went out for this window.

        Only 'sent' rows suppress: a failed qualified attempt leaves the
        window open for a research fallback instead of duplicating it.
        """
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT 1 FROM rival_hunt_delivery
                   WHERE user_id = ? AND asset = ? AND horizon = ?
                     AND expires_at = ? AND state = 'sent'
                     AND forecast_key LIKE 'qualified|%'""",
                (user_id, asset, horizon, expires.isoformat()),
            ).fetchone()
        return row is not None

    def subscribe(self, user_id: str, *, now: datetime | None = None) -> bool:
        stamp = (now or datetime.now(UTC)).isoformat()
        with self.store.connect() as conn:
            prior = conn.execute(
                "SELECT enabled FROM rival_hunt_subscriptions WHERE user_id=?",
                (user_id,),
            ).fetchone()
            if prior is not None and int(prior["enabled"]) == 1:
                return False
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM rival_hunt_subscriptions WHERE enabled=1",
            ).fetchone()
            if int(count["n"]) >= MAX_USERS:
                raise ValueError("Auto-Hunt subscriptions are full right now.")
            conn.execute(
                """INSERT INTO rival_hunt_subscriptions(user_id,enabled,since_at)
                   VALUES(?,1,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       enabled=1,since_at=excluded.since_at,last_sent_at=NULL""",
                (user_id, stamp),
            )
            return True

    def choose_source(self, user_id: str, source: str) -> None:
        if source not in ("forecasts", "polymarket"):
            raise ValueError("Unsupported source")
        with self.store.connect() as conn:
            conn.execute(
                """INSERT INTO rival_hunt_source(user_id,selected_source)
                   VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET
                   selected_source=excluded.selected_source""",
                (user_id, source),
            )
            conn.execute(
                "UPDATE rival_hunt_subscriptions SET last_sent_at=NULL WHERE user_id=?",
                (user_id,),
            )

    def source(self, user_id: str) -> str:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT selected_source FROM rival_hunt_source WHERE user_id=?",
                (user_id,),
            ).fetchone()
        return str(row["selected_source"]) if row else "forecasts"

    def stop(self, user_id: str) -> bool:
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE rival_hunt_preferences SET direction_changes=0, "
                "live_shifts=0 WHERE user_id=?", (user_id,),
            )
            updated = conn.execute(
                "UPDATE rival_hunt_subscriptions SET enabled=0 WHERE user_id=? AND enabled=1",
                (user_id,),
            )
            flags = conn.execute(
                """UPDATE rival_hunt_tiers
                   SET research=0, qualified=0, polymarket=0
                   WHERE user_id=? AND (research=1 OR qualified=1 OR polymarket=1)""",
                (user_id,),
            )
            return updated.rowcount == 1 or flags.rowcount == 1

    def enabled(self, user_id: str) -> bool:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT enabled FROM rival_hunt_subscriptions WHERE user_id=?",
                (user_id,),
            ).fetchone()
            return row is not None and int(row["enabled"]) == 1

    def subscribers(self) -> list[dict]:
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT s.user_id,s.since_at,s.last_sent_at,
                          COALESCE(p.selected_source,'forecasts') AS source,
                          COALESCE(t.research,
                              CASE WHEN COALESCE(p.selected_source,'forecasts')
                                   = 'forecasts' THEN 1 ELSE 0 END) AS research,
                          COALESCE(t.qualified, 0) AS qualified,
                          COALESCE(t.polymarket,
                              CASE WHEN p.selected_source = 'polymarket'
                                   THEN 1 ELSE 0 END) AS polymarket
                   FROM rival_hunt_subscriptions s
                   LEFT JOIN rival_hunt_source p ON p.user_id=s.user_id
                   LEFT JOIN rival_hunt_tiers t ON t.user_id=s.user_id
                   WHERE s.enabled=1 ORDER BY s.user_id LIMIT ?""",
                (MAX_USERS,),
            ).fetchall()
            return [dict(row) for row in rows]

    def claim(self, user_id: str, candidate: HuntCandidate, *, now: datetime,
              key: str | None = None, apply_cooldown: bool = True) -> bool:
        """Atomically reserve a DM, including only safely retryable preflight.

        A previously SENT, UNKNOWN, FAILED, SENDING or legacy ATTEMPTED
        record can never be claimed again. Only a READY record that has
        never attempted message creation may be retried, with strict expiry.
        """
        effective_key = key or candidate.key
        with self.store.connect() as conn:
            eligible = conn.execute(
                """SELECT since_at,last_sent_at FROM rival_hunt_subscriptions
                   WHERE user_id=? AND enabled=1""", (user_id,),
            ).fetchone()
            if eligible is None:
                return False
            if candidate.venue != "polymarket" and (
                _instant(eligible["since_at"]) > candidate.issued
            ):
                return False
            old = conn.execute(
                """SELECT state,attempt_count,next_attempt_at
                   FROM rival_hunt_delivery
                   WHERE user_id=? AND forecast_key=?""",
                (user_id, effective_key),
            ).fetchone()
            if old is not None:
                if (
                    old["state"] != "ready"
                    or int(old["attempt_count"]) >= MAX_SAFE_RETRIES
                    or candidate.expires - now < MIN_REMAINING
                    or (
                        old["next_attempt_at"] is not None
                        and now < _instant(str(old["next_attempt_at"]))
                    )
                ):
                    return False
                # The per-user lock serializes local retries. SQL updates
                # nevertheless guard the row against competing processes.
                changed = conn.execute(
                    """UPDATE rival_hunt_delivery
                       SET state='ready',next_attempt_at=?
                       WHERE user_id=? AND forecast_key=? AND state='ready'
                         AND attempt_count<?
                         AND (next_attempt_at IS NULL OR next_attempt_at<=?)""",
                    ((now+RETRY_DELAY).isoformat(), user_id, effective_key,
                     MAX_SAFE_RETRIES, now.isoformat()),
                )
                return changed.rowcount == 1
            if apply_cooldown and eligible["last_sent_at"] and (
                now - _instant(eligible["last_sent_at"]) < COOLDOWN
            ):
                return False
            created = conn.execute(
                """INSERT INTO rival_hunt_delivery
                   (user_id,forecast_key,asset,horizon,venue,issued_at,expires_at,
                    attempted_at,state,attempt_count,next_attempt_at)
                   VALUES (?,?,?,?,?,?,?,?,?,0,?)
                   ON CONFLICT(user_id,forecast_key) DO NOTHING""",
                (user_id, effective_key, candidate.asset, candidate.horizon,
                 candidate.venue, candidate.issued.isoformat(),
                 candidate.expires.isoformat(), now.isoformat(), "ready",
                 (now+RETRY_DELAY).isoformat()),
            )
            if created.rowcount != 1:
                return False
            if apply_cooldown:
                conn.execute(
                    """UPDATE rival_hunt_subscriptions SET last_sent_at=?
                       WHERE user_id=? AND enabled=1""",
                    (now.isoformat(), user_id),
                )
            return True

    def begin_delivery(
        self, user_id: str, candidate: HuntCandidate, *,
        key: str | None = None, now: datetime | None = None,
    ) -> bool:
        """Cross the one-way network boundary only after preflight succeeds."""
        stamp = now or datetime.now(UTC)
        with self.store.connect() as conn:
            result = conn.execute(
                """UPDATE rival_hunt_delivery
                   SET state='sending', attempted_at=?,
                       attempt_count=attempt_count+1, next_attempt_at=NULL
                   WHERE user_id=? AND forecast_key=? AND state='ready'
                     AND attempt_count<? AND expires_at>?
                     AND EXISTS (
                         SELECT 1 FROM rival_hunt_subscriptions
                         WHERE user_id=? AND enabled=1
                     )""",
                (stamp.isoformat(), user_id, key or candidate.key,
                 MAX_SAFE_RETRIES, stamp.isoformat(), user_id),
            )
            return result.rowcount == 1

    def defer_preflight(
        self, user_id: str, candidate: HuntCandidate, *,
        key: str | None = None, now: datetime | None = None,
    ) -> None:
        """Retry safely later only if channel.send was NEVER called."""
        stamp = now or datetime.now(UTC)
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE rival_hunt_delivery
                   SET next_attempt_at=?,attempt_count=attempt_count+1
                   WHERE user_id=? AND forecast_key=? AND state='ready'
                     AND attempt_count<?""",
                ((stamp+RETRY_DELAY).isoformat(), user_id, key or candidate.key,
                 MAX_SAFE_RETRIES),
            )

    def finish(
        self, user_id: str, candidate: HuntCandidate, *, sent: bool,
        message_id: str | None = None, key: str | None = None,
    ) -> None:
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE rival_hunt_delivery SET
                       state=?,message_id=?,delivered_at=?
                   WHERE user_id=? AND forecast_key=?
                     AND (state IN ('sending','attempted')
                          OR (state='ready' AND ?=1))""",
                ("sent" if sent else "unknown", message_id,
                 datetime.now(UTC).isoformat() if sent else None,
                 user_id, key or candidate.key, 1 if sent else 0),
            )

    def delivery_health(self, user_id: str) -> dict:
        """Actual recorded results, never inferred success from an ON switch."""
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT state,COUNT(*) AS n
                   FROM rival_hunt_delivery WHERE user_id=?
                   GROUP BY state""", (user_id,),
            ).fetchall()
        return {str(row["state"]): int(row["n"]) for row in rows}

    def reviewed(self, user_id: str, message_id: str) -> bool:
        with self.store.connect() as conn:
            result = conn.execute(
                """UPDATE rival_hunt_delivery SET reviewed_at=?
                   WHERE user_id=? AND message_id=? AND state='sent'""",
                (datetime.now(UTC).isoformat(), user_id, message_id),
            )
            return result.rowcount == 1


async def eligible_research_watches(
    market, quotes, *, now: datetime | None = None,
) -> list[HuntCandidate]:
    """Inspect all six asset/horizon choices without user-entered strikes.

    A model forecast is only a research watch; lack of a sportsbook strike,
    validated payoff, or after-cost edge never becomes an invented bet.
    """
    if market is None:
        return []
    stamp = now or datetime.now(UTC)
    if stamp.utcoffset() is None:
        raise ValueError("Auto-Hunt requires an aware clock")
    stamp = stamp.astimezone(UTC)

    async def inspect_asset(asset: str) -> list[HuntCandidate]:
        # Promotion can differ by horizon, so read each horizon's actual owner.
        rows: list[tuple[str, dict]] = []
        for horizon in HORIZONS:
            selected = await asyncio.to_thread(
                promoted_engine, market.store, asset=asset,
                horizon=horizon, now=stamp,
            )
            source = recent_multi_watches if selected == "multi" else recent_watches
            watches = await asyncio.to_thread(
                source, market, asset=asset, now=stamp,
                limit=40, dedupe_horizons=False,
            )
            for watch in watches:
                if watch["horizon_minutes"] == horizon:
                    rows.append((selected, watch))
        winners: list[HuntCandidate] = []
        seen: set[tuple[int, str]] = set()
        for selected, watch in sorted(
            rows, key=lambda item: item[1]["issued"], reverse=True,
        ):
            venue = str(watch["venue"])
            horizon = int(watch["horizon_minutes"])
            if venue not in BOOK_VENUES or (horizon, venue) in seen:
                continue
            seen.add((horizon, venue))
            issued = watch["issued"]
            expires = watch["target"] + timedelta(minutes=5)
            p = float(watch["probability_up"])
            if (
                issued > stamp or stamp - issued > MAX_ISSUE_AGE
                or expires - stamp < MIN_REMAINING
                or not math.isfinite(p)
                or model_strength(p) != "directional watch"
            ):
                continue
            try:
                if selected == "multi":
                    proof = await asyncio.to_thread(
                        multi_summary, market.store,
                        asset=asset, horizon=horizon, now=stamp,
                    )
                else:
                    proof = await asyncio.to_thread(
                        _venue_proof, market, asset=asset, horizon=horizon,
                        venue=venue, now=stamp,
                    )
                if proof.get("model_beats_baseline") is not True:
                    continue
                book = await asyncio.wait_for(
                    _venue_book(quotes, asset=asset, venue=venue),
                    timeout=4.0,
                )
                check_at = datetime.now(UTC) if now is None else stamp
                book.validate(now=check_at)
                spread = (book.ask - book.bid) / ((book.ask + book.bid) / 2) * 10000
                if spread > MAX_SPREAD_BPS:
                    continue
                if expires - check_at < MIN_REMAINING:
                    continue
            except Exception:  # noqa: BLE001 - bad source cannot stop others
                _LOG.info("Auto-Hunt source not qualified asset=%s venue=%s", asset, venue)
                continue
            winners.append(HuntCandidate(
                asset=asset, horizon=horizon, venue=venue, issued=issued,
                expires=expires, probability_up=p, bid=book.bid, ask=book.ask,
                quote_at=book.exchange_at, engine=selected,
                model_version=str(watch.get("model_version", "live")),
            ))
        return winners

    outcomes = await asyncio.gather(
        *(inspect_asset(asset) for asset in ASSETS), return_exceptions=True,
    )
    picks: list[HuntCandidate] = []
    for asset, outcome in zip(ASSETS, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            _LOG.warning("Auto-Hunt market scan unavailable asset=%s", asset)
        else:
            picks.extend(outcome)
    # Strongest research lean first, then earliest expiring; cap per user below.
    return sorted(picks, key=lambda c: (-c.strength, c.expires, c.asset))


async def new_forecast_watches(
    market, *, now: datetime | None = None,
) -> list[HuntCandidate]:
    """Notify genuine newly issued model forecasts even when not trade-qualified.

    This path is intentionally cheap. It does NOT wait for costly model
    backtesting, historical recovery, or a network quote to say that a
    directional forecast has been published. Any unverified model is explicitly
    labeled research-only, NEVER an executable market opportunity.
    """
    if market is None:
        return []
    stamp = now or datetime.now(UTC)
    if stamp.utcoffset() is None:
        raise ValueError("Auto-Hunt requires an aware clock")
    stamp = stamp.astimezone(UTC)

    async def by_asset(asset: str) -> list[HuntCandidate]:
        try:
            # One live forecast snapshot per asset, regardless of how many
            # horizons the subscriber follows. Previously the same forty-row
            # query, feed health and candle checks ran three times per asset.
            live = await asyncio.to_thread(
                recent_watches, market, asset=asset, now=stamp,
                limit=40, dedupe_horizons=False,
            )
            fused: list[dict] | None = None
            picks: list[HuntCandidate] = []
            for horizon in HORIZONS:
                engine = await asyncio.to_thread(
                    promoted_engine, market.store, asset=asset,
                    horizon=horizon, now=stamp,
                )
                if engine == "multi":
                    if fused is None:
                        fused = await asyncio.to_thread(
                            recent_multi_watches, market, asset=asset, now=stamp,
                            limit=40, dedupe_horizons=False,
                        )
                    watches = fused
                else:
                    watches = live
                for watch in watches:
                    if int(watch["horizon_minutes"]) != horizon:
                        continue
                    issued = watch["issued"]
                    expires = watch["target"] + timedelta(minutes=5)
                    probability = float(watch["probability_up"])
                    if (
                        issued > stamp
                        or stamp - issued > FORECAST_ALERT_MAX_AGE
                        or expires - stamp < MIN_REMAINING
                        or not math.isfinite(probability)
                        or not 0 < probability < 1
                    ):
                        continue
                    picks.append(HuntCandidate(
                        asset=asset, horizon=horizon, venue=str(watch["venue"]),
                        issued=issued, expires=expires,
                        probability_up=probability, bid=None, ask=None,
                        quote_at=None, engine=engine,
                        model_version=str(watch.get("model_version", "live")),
                    ))
                    break
            return picks
        except Exception:  # noqa: BLE001 - one lagging feed must not stall others
            _LOG.exception("Auto-Hunt forecast lookup failed asset=%s", asset)
            return []

    results = await asyncio.gather(
        *(by_asset(asset) for asset in ASSETS), return_exceptions=True,
    )
    candidates: list[HuntCandidate] = []
    for result in results:
        if isinstance(result, BaseException):
            if isinstance(result, asyncio.CancelledError):
                raise result
            _LOG.warning("Auto-Hunt forecast scan unavailable")
        else:
            candidates.extend(result)
    # Prefer recently issued forecasts and earliest actionable expiry,
    # not raw model strength mistaken for a calibrated win probability.
    return sorted(candidates, key=lambda c: (-c.issued.timestamp(), c.expires))


async def qualified_watches(
    market, quotes, *, stream_shadow, now: datetime | None = None,
) -> list[HuntCandidate]:
    """Bounded concurrent screening, each quote rechecked after signal awaits.

    Explicit now supplies a deterministic clock for tests; production
    checks the wall clock AFTER each awaited signal, not at scan start.
    """
    if market is None:
        return []
    scan_at = now or datetime.now(UTC)
    if scan_at.utcoffset() is None:
        raise ValueError("Auto-Hunt requires an aware clock")
    scan_at = scan_at.astimezone(UTC)
    budget = asyncio.Semaphore(3)

    async def evaluate(asset: str, horizon: int) -> HuntCandidate | None:
        async with budget:
            decision: dict = {}
            started = time.monotonic()
            try:
                await asyncio.wait_for(signal_embed(
                    market, quotes, asset=asset, horizon_minutes=horizon,
                    now=scan_at, stream_shadow=stream_shadow,
                    decision_out=decision, decision_only=True,
                    deadline=started + 6.5,
                ), timeout=7)
            except SignalStageTimeout as exc:
                _LOG.warning(
                    "Auto-Hunt qualified slot %s/%dm timed out at %s "
                    "elapsed=%.2fs; no unverified alert issued",
                    asset, horizon, exc.stage, time.monotonic() - started,
                )
                return None
            except TimeoutError:
                _LOG.warning(
                    "Auto-Hunt qualified slot %s/%dm exceeded overall "
                    "deadline elapsed=%.2fs; no unverified alert issued",
                    asset, horizon, time.monotonic() - started,
                )
                return None
            except Exception:  # noqa: BLE001 - one failed slot cannot block others
                _LOG.exception("Auto-Hunt qualified slot %s/%dm failed", asset, horizon)
                return None
            if not decision.get("is_watch"):
                return None
            checked_at = scan_at if now is not None else datetime.now(UTC)
            quote_at = decision.get("quote_at")
            expires = decision["target"] + timedelta(minutes=5)
            if (
                not isinstance(quote_at, datetime)
                or quote_at.utcoffset() is None
                or not timedelta(0) <= checked_at - quote_at <= MAX_QUOTE_AGE
                or expires - checked_at < MIN_REMAINING
            ):
                return None
            return HuntCandidate(
                asset=asset, horizon=horizon, venue=str(decision["venue"]),
                issued=decision["issued"], expires=expires,
                probability_up=float(decision["probability"]),
                bid=decision.get("bid"), ask=decision.get("ask"),
                quote_at=quote_at, engine=str(decision["engine"]),
                model_version=str(decision["model_version"]),
                screened=True, after_cost_bps=decision.get("after_cost_bps"),
                after_cost_status=decision.get("after_cost_status"),
            )

    results = await asyncio.gather(
        *(evaluate(asset, horizon) for asset in ASSETS for horizon in HORIZONS),
        return_exceptions=True,
    )
    picks = [item for item in results if isinstance(item, HuntCandidate)]
    return sorted(picks, key=lambda c: (-c.strength, c.expires, c.asset))



def confirmed_direction_change(latest: HuntCandidate, previous: HuntCandidate,
                               *, now: datetime) -> bool:
    """True only for opposite strong models on distinct future candle windows."""
    if (
        latest.asset != previous.asset
        or latest.horizon != previous.horizon
        or latest.engine != previous.engine
        or not previous.issued < latest.issued <= now
        or previous.expires >= latest.expires
        or latest.issued - previous.issued > FLIP_MAX_PREVIOUS_AGE
        or now - latest.issued > FORECAST_ALERT_MAX_AGE
        or latest.expires - now < MIN_REMAINING
    ):
        return False
    before, after = previous.probability_up, latest.probability_up
    if not all(math.isfinite(p) and 0 < p < 1 for p in (before, after)):
        return False
    return (before <= FLIP_DOWN and after >= FLIP_UP) or (
        before >= FLIP_UP and after <= FLIP_DOWN
    )


def _causal_direction_pair(
    market, *, asset: str, horizon: int, engine: str, now: datetime,
) -> tuple[HuntCandidate, HuntCandidate] | None:
    """Read both forecast windows INCLUDING a settled previous outcome.

    The public recent_watches() viewer intentionally hides settled/expired
    historical rows. It therefore cannot be used for reversal detection:
    5m/15m prior windows may already have settled when the new issue arrives.
    """
    from parlay_bot.perpetuals.forward_audit import MODEL as LIVE_MODEL
    from parlay_bot.perpetuals.multi_exchange import MODEL as MULTI_MODEL

    if engine == "multi":
        sql = (
            "SELECT reference_venue AS venue, horizon_minutes, "
            "target_opened_at, issued_at, probability_up, model_version "
            "FROM perp_multi_forward WHERE asset=? AND horizon_minutes=? "
            "AND model_version=? AND issued_at>=? AND issued_at<=? "
            "ORDER BY issued_at DESC LIMIT 100"
        )
        version = MULTI_MODEL
    else:
        sql = (
            "SELECT venue, horizon_minutes, target_opened_at, issued_at, "
            "probability_up, model_version FROM perp_forward_audit "
            "WHERE asset=? AND horizon_minutes=? AND model_version=? "
            "AND issued_at>=? AND issued_at<=? "
            "ORDER BY issued_at DESC LIMIT 100"
        )
        version = LIVE_MODEL
    start = (now-FLIP_MAX_PREVIOUS_AGE).isoformat()
    with market.store.connect() as conn:
        records = conn.execute(
            sql, (asset, horizon, version, start, now.isoformat()),
        ).fetchall()
    # Use the same source and model on both sides, and distinct targets.
    # No vote-counting of correlated venue replicas.
    latest_by_venue: dict[str, HuntCandidate] = {}
    for raw in records:
        try:
            row = dict(raw)
            issued = _instant(str(row["issued_at"]))
            target = _instant(str(row["target_opened_at"]))
            venue = str(row["venue"])
            p = float(row["probability_up"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if (
            not math.isfinite(p) or not 0 < p < 1
            or not issued < target + timedelta(minutes=5)
            or issued > now or target > now + timedelta(minutes=65)
        ):
            continue
        candidate = HuntCandidate(
            asset=asset, horizon=horizon, venue=venue,
            issued=issued, expires=target+timedelta(minutes=5),
            probability_up=p, bid=None, ask=None, quote_at=None,
            engine=engine, model_version=version,
        )
        newer = latest_by_venue.get(venue)
        if newer is None:
            if (
                now - issued > FORECAST_ALERT_MAX_AGE
                or candidate.expires - now < MIN_REMAINING
            ):
                continue
            latest_by_venue[venue] = candidate
        elif candidate.expires < newer.expires:
            return newer, candidate
    return None


async def direction_change_watches(market, *,
                                   now: datetime | None = None) -> list[HuntCandidate]:
    """Compare two truly issued forecast windows, settled previous allowed."""
    if market is None:
        return []
    from dataclasses import replace

    stamp = (now or datetime.now(UTC)).astimezone(UTC)

    async def scan(asset: str) -> list[HuntCandidate]:
        findings = []
        for horizon in HORIZONS:
            try:
                engine = await asyncio.to_thread(
                    promoted_engine, market.store, asset=asset,
                    horizon=horizon, now=stamp,
                )
                pair = await asyncio.to_thread(
                    _causal_direction_pair, market, asset=asset, horizon=horizon,
                    engine=engine, now=stamp,
                )
                if pair and confirmed_direction_change(
                    pair[0], pair[1], now=stamp,
                ):
                    findings.append(replace(
                        pair[0], previous_probability=pair[1].probability_up,
                        previous_issued=pair[1].issued,
                    ))
            except Exception:  # noqa: BLE001
                _LOG.exception(
                    "Direction-change watch failed asset=%s horizon=%s",
                    asset, horizon,
                )
        return findings

    result = await asyncio.gather(
        *(scan(asset) for asset in ASSETS), return_exceptions=True,
    )
    findings = []
    for item in result:
        if isinstance(item, asyncio.CancelledError):
            raise item
        if isinstance(item, list):
            findings.extend(item)
    return findings


def candidate_allowed(candidate, preferences: dict, *, now: datetime) -> bool:
    """Apply explicit per-person filters before DB claim or DM send."""
    if (candidate.asset not in preferences["assets"]
        or candidate.horizon not in preferences["horizons"]):
        return False
    if isinstance(candidate, PolymarketNotice):
        if (candidate.expires - now).total_seconds() < preferences[
            "min_remaining_seconds"
        ]:
            return False
        ceiling = preferences["max_ask_pct"] / 100
        side = preferences["side"]
        return (
            (side in ("both", "up") and candidate.up_ask <= ceiling)
            or (side in ("both", "down") and candidate.down_ask <= ceiling)
        )
    return True


def hunt_embed(
    candidate: HuntCandidate | PolymarketNotice, *, observed_at: datetime,
    tier: str | None = None,
) -> discord.Embed:
    if isinstance(candidate, LiveShiftNotice):
        embed = discord.Embed(
            title=f"🔄 Live market trend changed • {candidate.asset} {candidate.direction}",
            description=(
                f"Two independent perpetual order books confirm a "
                f"**{candidate.direction}** price move of "
                f"**{candidate.move_bps:+.1f} basis points** "
                f"over approximately **{candidate.lookback_seconds} seconds**.\n"
                "Both venues crossed a 15-basis-point direction threshold "
                "and held confirmation across multiple checks.\n"
                f"Observed <t:{int(candidate.issued.timestamp())}:R>.\n\n"
                "**Live price momentum only.** This is not a new "
                "five-minute ML prediction, a calibrated chance of winning, "
                "or a verified betting opportunity. No wager placed."
            ), color=discord.Color.orange(),
        )
        embed.set_footer(text="Opt-in market shift • /rival-stop")
        return embed
    if isinstance(candidate, PolymarketNotice):
        fill_line = (
            "10-share depth-weighted ask before fees: "
            + (f"Up **${candidate.up_average_ask:.2f}/share**"
               if candidate.up_average_ask is not None else "Up **insufficient depth**")
            + " • "
            + (f"Down **${candidate.down_average_ask:.2f}/share**"
               if candidate.down_average_ask is not None else "Down **insufficient depth**")
        )
        embed = discord.Embed(
            title=f"📊 Polymarket • {candidate.asset} {candidate.horizon}m market",
            description=(
                "**Listed Polymarket market, not a RivaL prediction.**\n"
                f"Up ask: **${candidate.up_ask:.2f}/share** per share • "
                f"Down ask: **${candidate.down_ask:.2f}/share** per share.\n"
                f"{fill_line}\n"
                f"Book received <t:{int(candidate.quote_at.timestamp())}:R>.\n"
                f"Market closes <t:{int(candidate.expires.timestamp())}:R>.\n"
                f"[View exact event]({candidate.url})\n\n"
                "Top-of-book and depth estimates are NOT guaranteed fills, "
                "include no fees, and are NOT calibrated win probabilities or a "
                "verified edge. No bet placed. Availability varies by jurisdiction."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Read-only global Polymarket data • /rival-stop")
        return embed
    seconds = max(0, int((candidate.expires - observed_at).total_seconds()))
    if tier == "flip" and candidate.previous_probability is not None:
        old = "UP" if candidate.previous_probability > 0.5 else "DOWN"
        embed = discord.Embed(
            title=f"🔄 RivaL direction changed • {candidate.asset} {candidate.horizon}m",
            description=(
                f"Earlier model issue: **{old}** "
                f"({candidate.previous_probability:.1%} raw UP score).\\n"
                f"Newest model issue: **{candidate.direction}** "
                f"({candidate.probability_up:.1%} raw UP score).\\n"
                f"New issue <t:{int(candidate.issued.timestamp())}:R> • "
                f"Expires <t:{int(candidate.expires.timestamp())}:R>.\\n\\n"
                "Two real forecasts on distinct 5-minute reference windows "
                "crossed opposite sides of a confidence buffer. Not an "
                "intrabar recalculation, calibrated win probability, or "
                "profitable trading opportunity. No order placed."
            ),
            color=discord.Color.orange(),
        )
        embed.set_footer(text="Opt-in model reversal research • /rival-stop")
        return embed
    if tier == "qualified" and candidate.screened:
        # Historic perpetual simulation is not contract-specific bet EV.
        edge = candidate.after_cost_bps
        if edge is not None and math.isfinite(edge):
            edge_line = (
                f"Historical simulated after-cost return: **{edge:+.1f} bps** "
                "(severe-cost research mean, not this contract's edge)."
            )
        else:
            edge_line = "Historical simulated after-cost return: unavailable."
        edge_line += (
            " Current betting edge: **NOT ESTABLISHED**; this is not a bet opportunity."
        )
        embed = discord.Embed(
            title=(f"✅ Auto-Hunt • QUALIFIED WATCH • {candidate.asset} "
                   f"{candidate.horizon}m • {candidate.direction}"),
            description=(
                "**Tier: QUALIFIED WATCH** — validated forecast evidence "
                "with a fresh matching-source quote.\n"
                f"Raw directional model strength: **{candidate.strength:.1%}** "
                "(not calibrated odds).\n"
                f"Forecast reference venue: **{candidate.venue}**.\n"
                f"Perpetual-exchange {candidate.venue} book at "
                f"<t:{int(candidate.quote_at.timestamp())}:R>: "
                f"Bid **${candidate.bid:,.2f}** / "
                f"Ask **${candidate.ask:,.2f}**.\n"
                f"{edge_line}\n"
                f"Issued <t:{int(candidate.issued.timestamp())}:R> • "
                f"Expires <t:{int(candidate.expires.timestamp())}:R> "
                f"(~{seconds}s remained at notification).\n\n"
                "Not a guaranteed prediction or automatically executable "
                "market. Refresh to review any current quote."
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(
            text="Opt-in only • No wagers or orders • /rival-stop to stop alerts")
        return embed
    quote_line = (
        f"Validated {candidate.venue} book at "
        f"<t:{int(candidate.quote_at.timestamp())}:R>: "
        f"Bid **${candidate.bid:,.2f}** / Ask **${candidate.ask:,.2f}**."
        if candidate.screened and candidate.quote_at is not None
        and candidate.bid is not None and candidate.ask is not None
        else "Live executable quote and after-cost advantage: **NOT VERIFIED**."
    )
    embed = discord.Embed(
        title=f"🔎 Auto-Hunt • {candidate.asset} {candidate.horizon}m • {candidate.direction}",
        description=(
            "**New model forecast • research only**\n"
            "**Tier: EARLY RESEARCH** — a fresh forecast without proven "
            "edge. Never a wager.\n"
            f"Raw directional model strength: **{candidate.strength:.1%}** "
            "(not calibrated odds).\n"
            f"Forecast reference venue: **{candidate.venue}**.\n"
            f"{quote_line}\n"
            f"Issued <t:{int(candidate.issued.timestamp())}:R> • "
            f"Expires <t:{int(candidate.expires.timestamp())}:R> "
            f"(~{seconds}s remained at notification).\n\n"
            "Not a betting recommendation, trade signal, guaranteed prediction, "
            "or automatically executable market. Refresh to review any current quote."
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Opt-in only • No wagers or orders • /rival-stop to stop alerts")
    return embed



async def current_alert_review(service: AutoHuntService, row: dict) -> discord.Embed:
    """Re-query actual current market, never replay an old quote as fresh."""
    try:
        if row["venue"] == "live_trend":
            scanner = getattr(service.bot, "live_direction", None)
            stream = getattr(service.bot, "live_stream", None)
            now = datetime.now(UTC)
            trend = scanner._confirmed.get(row["asset"]) if scanner else None
            hyper = stream.snapshot("hyperliquid", row["asset"], now=now) if stream else None
            binance = stream.snapshot("binance", row["asset"], now=now) if stream else None
            if hyper is None or binance is None or trend is None:
                return discord.Embed(
                    title="Live market direction unavailable",
                    description="Both fresh exchange books and an established "
                                "confirmed trend are required. No prior quote "
                                "is reused and no new prediction is invented.",
                    color=discord.Color.orange(),
                )
            return discord.Embed(
                title=f"📡 Current live trend • {row['asset']}",
                description=(
                    f"Most recent confirmed two-exchange price momentum: "
                    f"**{trend}**.\\n"
                    f"Hyperliquid bid/ask: **{hyper.bid:,.4f} / {hyper.ask:,.4f}**.\\n"
                    f"Binance USD-M bid/ask: "
                    f"**{binance.bid:,.4f} / {binance.ask:,.4f}**.\\n"
                    "Research-only live momentum, NOT the ML model's "
                    "latest candle-based prediction."
                ), color=discord.Color.blurple(),
            )
        if row["venue"] == "polymarket":
            notice = await asyncio.wait_for(
                service.polymarket.market(
                    row["asset"], int(row["horizon"]),
                    opened=_instant(row["issued_at"]),
                ), timeout=10,
            )
            if notice is None:
                # The button must keep working after the old 5m/15m
                # window expires. Advance only to a separately verified
                # real current market, never relabel the old quote as new.
                notice = await asyncio.wait_for(
                    service.polymarket.market(
                        row["asset"], int(row["horizon"]),
                    ), timeout=10,
                )
            if notice is None:
                return discord.Embed(
                    title="Market window closed or unavailable",
                    description="No currently listed matching event has a "
                                "verified fresh orderbook. Old quotes are "
                                "not reused as new market prices.",
                    color=discord.Color.orange(),
                )
            embed = hunt_embed(notice, observed_at=datetime.now(UTC))
            if notice.issued != _instant(row["issued_at"]):
                embed.add_field(
                    name="New market window",
                    value="The previous event expired. This is a newly "
                          "verified event with its own independent orderbook.",
                    inline=False,
                )
            return embed
        return await asyncio.wait_for(
            signal_embed(
                service.market, service.quotes,
                asset=row["asset"], horizon_minutes=int(row["horizon"]),
                stream_shadow=getattr(service.bot, "stream_shadow", None),
            ), timeout=12,
        )
    except Exception:  # noqa: BLE001 - old quotes must not look current
        _LOG.exception("RivaL fresh review failed")
        return discord.Embed(
            title="Current market review unavailable",
            description="Unable to validate a newer live quote or forecast. "
                        "The previous alert is not a new trading signal.",
            color=discord.Color.orange(),
        )


class RepeatReviewView(discord.ui.View):
    """Private, repeatable refresh without consuming the original DM button."""

    def __init__(self, owner_id: int, message_id: str):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.message_id = message_id

    @discord.ui.button(label="Refresh Again", emoji="🔄",
                       style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction,
                      _: discord.ui.Button) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the alert recipient can review this.", ephemeral=True,
            )
            return
        await interaction.response.defer()
        service = getattr(interaction.client, "auto_hunt", None)
        row = (
            await asyncio.to_thread(
                service.delivery_for_message,
                str(self.owner_id), self.message_id,
            )
            if service is not None else None
        )
        if row is None:
            await interaction.followup.send(
                "This alert is no longer saved. Open /rival for a new one.",
                ephemeral=True,
            )
            return
        embed = await current_alert_review(service, row)
        await interaction.edit_original_response(
            embed=embed, view=RepeatReviewView(self.owner_id, self.message_id),
        )


class AutoHuntActions(discord.ui.View):
    """Stateless registered view: old DM buttons still work after a restart."""

    def __init__(self, *, timeout: float | None = None) -> None:
        super().__init__(timeout=timeout)

    @discord.ui.button(
        label="Review Fresh Signal", custom_id="rival:hunt:review",
        style=discord.ButtonStyle.primary,
    )
    async def review(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        # Discord requires an acknowledgment within ~3s. Quote research can
        # take longer, so defer BEFORE any database or exchange I/O.
        await interaction.response.defer(ephemeral=True, thinking=True)
        service = getattr(interaction.client, "auto_hunt", None)
        if service is None or interaction.message is None:
            await interaction.followup.send("Review temporarily unavailable.", ephemeral=True)
            return
        user_id = str(interaction.user.id)
        message_id = str(interaction.message.id)
        row = await asyncio.to_thread(service.delivery_for_message, user_id, message_id)
        if row is None:
            await interaction.followup.send("This is not your saved alert.", ephemeral=True)
            return
        embed = await current_alert_review(service, row)
        await interaction.followup.send(
            embed=embed,
            view=RepeatReviewView(interaction.user.id, message_id),
            ephemeral=True,
        )
        await asyncio.to_thread(service.store.reviewed, user_id, message_id)

    @discord.ui.button(
        label="Dismiss", custom_id="rival:hunt:dismiss",
        style=discord.ButtonStyle.secondary,
    )
    async def dismiss(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(thinking=False)
        service = getattr(interaction.client, "auto_hunt", None)
        if service is None or interaction.message is None:
            await interaction.followup.send("Alert unavailable.", ephemeral=True)
            return
        row = await asyncio.to_thread(
            service.delivery_for_message, str(interaction.user.id),
            str(interaction.message.id),
        )
        if row is None:
            await interaction.followup.send("This is not your alert.", ephemeral=True)
            return
        await interaction.message.edit(view=None)


class AutoHuntService:
    def __init__(self, *, bot: discord.Client, market, quotes, store: AutoHuntStore,
                 stream_shadow=None) -> None:
        self.bot = bot
        self.market = market
        self.quotes = quotes
        self.store = store
        self.stream_shadow = stream_shadow
        self.polymarket = PolymarketReader()
        self._user_locks: dict[str, asyncio.Lock] = {}
        self._cycle_lock = asyncio.Lock()
        self._poly_cycle_lock = asyncio.Lock()
        self._delivery_budget = asyncio.Semaphore(8)

    def _user_lock(self, user_id: str) -> asyncio.Lock:
        # Bounded at MAX_USERS while enabled; retained lock objects are small.
        return self._user_locks.setdefault(user_id, asyncio.Lock())

    def delivery_for_message(self, user_id: str, message_id: str) -> dict | None:
        with self.store.store.connect() as conn:
            row = conn.execute(
                """SELECT asset,horizon,venue,expires_at,issued_at
                   FROM rival_hunt_delivery WHERE user_id=? AND message_id=?
                     AND state='sent'""",
                (user_id, message_id),
            ).fetchone()
        return dict(row) if row else None

    async def _dm_opt_in_probe(
        self, user_id: int, *, user: discord.abc.User | None = None,
        label: str = "research alerts",
    ) -> None:
        """Confirm a DM reaches Discord BEFORE enabling any alert tier."""
        if user is None:
            user = self.bot.get_user(user_id)
        if user is None:
            user = await asyncio.wait_for(self.bot.fetch_user(user_id), timeout=5)
        channel = user.dm_channel or await asyncio.wait_for(user.create_dm(), timeout=5)
        await asyncio.wait_for(
            channel.send(
                f"🔔 RivaL is checking you can receive {label}. "
                "This is a delivery test, not a wager or a qualified alert."
            ),
            timeout=8,
        )

    async def subscribe(
        self, user: discord.abc.User, *, source: str = "forecasts",
    ) -> tuple[bool, str]:
        if source not in ("forecasts", "polymarket"):
            return False, "Unsupported alert source."
        if source == "forecasts" and self.market is None:
            return False, "Live crypto forecasting unavailable; forecasts not enabled."
        flag = "research" if source == "forecasts" else "polymarket"
        async with self._user_lock(str(user.id)):
            try:
                enabled = await asyncio.to_thread(self.store.enabled, str(user.id))
                flags = await asyncio.to_thread(self.store.tiers, str(user.id))
                if enabled and flags[flag]:
                    return True, "Already following " + (
                        "Polymarket markets." if source == "polymarket" else
                        "RivaL forecasts."
                    )
                await self._dm_opt_in_probe(
                    user.id, user=user, label=source + " research notifications",
                )
                if not enabled:
                    await asyncio.to_thread(self.store.subscribe, str(user.id))
                await asyncio.to_thread(self.store.choose_source, str(user.id), source)
                await asyncio.to_thread(self.store.set_tier, str(user.id), flag, True)
                return True, (
                    "Now following Polymarket crypto markets with actual Up/Down "
                    "quotes, not invented model predictions."
                    if source == "polymarket" else
                    "Now following newly issued RivaL research forecasts."
                )
            except (discord.HTTPException, TimeoutError):
                return False, "DM blocked or timed out. No subscription confirmed."
            except ValueError as exc:
                return False, str(exc)
            except Exception:  # noqa: BLE001 - fail closed on storage outage
                _LOG.exception("Auto-Hunt subscribe failed")
                return False, "Could not save Auto-Hunt. No subscription confirmed."

    async def update_preference(
        self, user_id: int, field: str, value: object,
    ) -> dict:
        async with self._user_lock(str(user_id)):
            if field in ("direction_changes", "live_shifts") and value == 1:
                if field == "live_shifts" and getattr(self.bot, "live_stream", None) is None:
                    raise ValueError(
                        "Live WebSocket feeds are disabled. No live alerts enabled."
                    )
                enabled = await asyncio.to_thread(self.store.enabled, str(user_id))
                preferences = await asyncio.to_thread(
                    self.store.preferences, str(user_id),
                )
                if not enabled or not preferences[field]:
                    await self._dm_opt_in_probe(
                        user_id, label=(
                            "live market price-shift alerts" if field == "live_shifts"
                            else "new model direction-change alerts"
                        ),
                    )
                    if not enabled:
                        await asyncio.to_thread(self.store.subscribe, str(user_id))
                        # A live-alert opt-in must NOT silently enable
                        # legacy model research notifications as a side effect.
                        await asyncio.to_thread(
                            self.store.set_tier, str(user_id), "research", False,
                        )
            return await asyncio.to_thread(
                self.store.set_preference, str(user_id), field, value,
            )

    async def stop(self, user_id: int) -> bool:
        # A slow DM to a DIFFERENT user can never delay this cancellation.
        async with self._user_lock(str(user_id)):
            return await asyncio.to_thread(self.store.stop, str(user_id))

    async def toggle_tier(self, user_id: int, tier: str) -> tuple[bool, str]:
        """Flip one independent tier. Turning a tier ON subscribes the user
        if needed; turning one OFF never touches the other tiers."""
        if tier not in TIERS:
            return False, "Unsupported alert tier."
        async with self._user_lock(str(user_id)):
            flags = await asyncio.to_thread(self.store.tiers, str(user_id))
            new_state = not flags[tier]
            if new_state:
                try:
                    await self._dm_opt_in_probe(user_id, label=tier + " alerts")
                except (discord.HTTPException, TimeoutError, ValueError, AttributeError):
                    return False, "DM delivery test failed. Alert tier NOT enabled."
                enabled = await asyncio.to_thread(self.store.enabled, str(user_id))
                if not enabled:
                    await asyncio.to_thread(self.store.subscribe, str(user_id))
            await asyncio.to_thread(self.store.set_tier, str(user_id), tier, new_state)
            label = {"research": "Research", "qualified": "Qualified",
                     "polymarket": "Polymarket"}[tier]
            return new_state, (
                f"{label} alerts {'ON' if new_state else 'OFF'}."
            )

    async def status(self, user_id: int) -> str:
        try:
            active = await asyncio.to_thread(self.store.enabled, str(user_id))
        except Exception:  # noqa: BLE001 - storage fail closed
            return "Status unavailable. No new alert confirmed."
        if not active:
            return "OFF • choose a source to start"
        flags = await asyncio.to_thread(self.store.tiers, str(user_id))
        parts = " • ".join(
            f"{label} {'on' if flags[tier] else 'off'}"
            for tier, label in (("research", "Research"),
                                ("qualified", "Qualified"),
                                ("polymarket", "Polymarket"))
        )
        preferences = await asyncio.to_thread(
            self.store.preferences, str(user_id),
        )
        if not (
            any(flags.values())
            or preferences["live_shifts"]
            or preferences["direction_changes"]
        ):
            return "OFF • all alert types off — choose one to start"
        try:
            health = await asyncio.to_thread(self.store.delivery_health, str(user_id))
        except Exception:  # noqa: BLE001
            return "ON • " + parts + " • Delivery health unavailable"
        try:
            today = await asyncio.to_thread(
                self.store.daily_sent_counts, str(user_id),
            )
        except Exception:  # noqa: BLE001 - status display is best effort
            today = None
        parts += (
            f" • Model reversals {'on' if preferences['direction_changes'] else 'off'}"
            f" • Live trends {'on' if preferences['live_shifts'] else 'off'}"
            f" • Coins {','.join(preferences['assets'])}"
            f" • {','.join(str(h) + 'm' for h in preferences['horizons'])}"
        )
        uncertainty = sum(health.get(state, 0) for state in ("unknown", "sending", "attempted"))
        now = datetime.now(UTC)
        reset = (now.replace(hour=0, minute=0, second=0, microsecond=0)
                 + timedelta(days=1))
        budget = (
            "\n**Today's sent alerts (UTC):** "
            + " • ".join(
                f"{label} {today[tier]}/"
                + (str(preferences["daily_limit"])
                   if preferences["daily_limit"] > 0 else "Unlimited")
                for tier, label in (
                    ("research", "Research"), ("qualified", "Qualified"),
                    ("polymarket", "Polymarket"),
                    ("live_shift", "Live Mode"), ("flip", "Reversals"),
                ) if (
                    flags[tier] if tier in flags else preferences[
                        "live_shifts" if tier == "live_shift"
                        else "direction_changes"
                    ]
                )
            )
            + (
                f" • Resets <t:{int(reset.timestamp())}:R>"
                if preferences["daily_limit"] > 0 else
                " • No daily reset needed"
            )
            if today is not None else
            "\nToday's alert budget is temporarily unavailable."
        )
        return (
            "ON • " + parts + f" • Sent {health.get('sent', 0)} total"
            + (f" • Unconfirmed {uncertainty}" if uncertainty else "")
            + budget
            + (
                "\nUnlimited removes RivaL's daily cap, not duplicate "
                "protection. "
                if preferences["daily_limit"] == 0 else
                "\nDaily caps pause additional notifications until UTC reset. "
            )
            + "A qualifying signal and working DMs are still required."
        )

    async def _send_dm(self, user_id: str, candidate, stamp: datetime,
                       *, tier: str | None) -> object | None:
        """One attempt after bounded preflight; ambiguous sends never replay."""
        key = (
            QUALIFIED_KEY_PREFIX + candidate.key if tier == "qualified"
            else FLIP_KEY_PREFIX + candidate.key if tier == "flip"
            else candidate.key
        )
        dispatch_started = False
        message = None

        def fresh(at: datetime) -> bool:
            if candidate.expires - at < MIN_REMAINING:
                return False
            if tier == "qualified":
                return candidate.quote_at is not None and (
                    timedelta(0) <= at - candidate.quote_at <= MAX_QUOTE_AGE
                )
            if isinstance(candidate, LiveShiftNotice):
                return timedelta(0) <= at - candidate.issued <= timedelta(seconds=20)
            if isinstance(candidate, PolymarketNotice):
                return timedelta(0) <= at - candidate.quote_at <= timedelta(seconds=25)
            return True

        try:
            user = self.bot.get_user(int(user_id))
            if user is None:
                user = await asyncio.wait_for(self.bot.fetch_user(int(user_id)), timeout=5)
            channel = user.dm_channel or await asyncio.wait_for(user.create_dm(), timeout=5)
            if not fresh(datetime.now(UTC)):
                _LOG.info("Auto-Hunt candidate expired during preflight venue=%s", candidate.venue)
                return None
            dispatch_started = await asyncio.to_thread(
                self.store.begin_delivery, user_id, candidate, key=key, now=datetime.now(UTC),
            )
            if not dispatch_started:
                return None
            # PostgreSQL checkout can stall AFTER the first freshness check.
            # Recheck immediately before channel.send crosses the network boundary.
            attempted = datetime.now(UTC)
            if not fresh(attempted):
                _LOG.info("Auto-Hunt candidate expired during outbox claim venue=%s", candidate.venue)
                return None
            started = time.monotonic()
            status = "unconfirmed"
            try:
                message = await asyncio.wait_for(
                    channel.send(
                        embed=hunt_embed(candidate, observed_at=attempted, tier=tier),
                        view=AutoHuntActions(timeout=900),
                    ), timeout=8,
                )
                status = "sent"
            finally:
                if isinstance(candidate, PolymarketNotice):
                    received = candidate.received_at or candidate.quote_at
                    _LOG.info(
                        "RivaL hunt asset=%s horizon=%s user=%s tier=polymarket "
                        "window_age_ms=%d observed_to_attempt_ms=%d source_quote_age_ms=%d "
                        "delivery_ms=%d status=%s",
                        candidate.asset, candidate.horizon, user_id,
                        int((attempted - candidate.issued).total_seconds() * 1000),
                        int((attempted - received).total_seconds() * 1000),
                        int((attempted - candidate.quote_at).total_seconds() * 1000),
                        int((time.monotonic() - started) * 1000), status,
                    )
        except (discord.HTTPException, TimeoutError, ValueError):
            if not dispatch_started:
                await asyncio.to_thread(self.store.defer_preflight, user_id, candidate, key=key)
            _LOG.warning("Auto-Hunt DM not confirmed user=%s", user_id)
        except Exception:  # noqa: BLE001 - no ambiguous resend
            if not dispatch_started:
                await asyncio.to_thread(self.store.defer_preflight, user_id, candidate, key=key)
            _LOG.exception("Auto-Hunt DM failed")
        return message

    async def live_cycle(self, notices: list[LiveShiftNotice]) -> int:
        """Deliver new live market shifts from one shared five-second scan.

        The scanner reads cached WebSocket order books only. This method
        does not fetch price data per Discord member or change model forecasts.
        """
        if not notices:
            return 0
        subs = await asyncio.to_thread(self.store.subscribers)
        total = 0
        budget = self._delivery_budget

        async def deliver(sub: dict) -> int:
            user_id = str(sub["user_id"])
            async with budget, self._user_lock(user_id):
                prefs = await asyncio.to_thread(self.store.preferences, user_id)
                if not prefs["live_shifts"]:
                    return 0
                sent = 0
                for notice in notices:
                    now = datetime.now(UTC)
                    if (
                        notice.issued < _instant(str(sub["since_at"]))
                        or notice.expires <= now
                        or (now - notice.issued).total_seconds() > 20
                        or notice.asset not in prefs["assets"]
                    ):
                        continue
                    if prefs["daily_limit"] > 0 and _cap_reached(
                        await asyncio.to_thread(
                            self.store.count_sent_today, user_id, "live_shift", now,
                        ), prefs["daily_limit"],
                    ):
                        break
                    claimed = await asyncio.to_thread(
                        self.store.claim, user_id, notice,
                        now=now, apply_cooldown=False,
                    )
                    if not claimed:
                        continue
                    message = await self._send_dm(
                        user_id, notice, now, tier="live_shift",
                    )
                    await asyncio.to_thread(
                        self.store.finish, user_id, notice,
                        sent=message is not None,
                        message_id=str(message.id) if message else None,
                    )
                    if message is not None:
                        sent += 1
                return sent

        results = await asyncio.gather(
            *(deliver(sub) for sub in subs), return_exceptions=True,
        )
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, int):
                total += result
            else:
                _LOG.warning("Live trend alert delivery failed safely")
        return total

    async def cycle(self, *, scope: str = "all") -> int:
        # Only one scan and one candidate set per cycle, regardless of users.
        if scope not in ("all", "forecasts", "polymarket"):
            raise ValueError("Unsupported alert scan scope")
        async with AsyncExitStack() as locks:
            if scope != "polymarket":
                await locks.enter_async_context(self._cycle_lock)
            if scope != "forecasts":
                await locks.enter_async_context(self._poly_cycle_lock)
            subscribers = await asyncio.to_thread(self.store.subscribers)
            if scope == "polymarket":
                subscribers = [sub for sub in subscribers if sub.get("polymarket")]
            if not subscribers:
                return 0
            configs = await asyncio.gather(*(
                asyncio.to_thread(self.store.preferences, str(sub["user_id"]))
                for sub in subscribers
            ))
            # Avoid live quote DNS, Polymarket HTTP and evidence reads when
            # every follower of that tier is already at the sent-alert cap.
            # Delivery still rechecks caps after the scan for race safety.
            scan_at = datetime.now(UTC)
            daily = await asyncio.to_thread(
                self.store.daily_sent_counts_many,
                tuple(str(sub["user_id"]) for sub in subscribers),
                now=scan_at,
            )
            counts = [daily[str(sub["user_id"])] for sub in subscribers]
            want_qualified = any(
                s.get("qualified") and not _cap_reached(
                    count["qualified"], prefs["daily_limit"],
                )
                for s, count, prefs in zip(subscribers, counts, configs, strict=True)
            )
            want_research = any(
                s.get("research") and not _cap_reached(
                    count["research"], prefs["daily_limit"],
                )
                for s, count, prefs in zip(subscribers, counts, configs, strict=True)
            )
            want_poly = any(
                s.get("polymarket") and not _cap_reached(
                    count["polymarket"], prefs["daily_limit"],
                )
                for s, count, prefs in zip(subscribers, counts, configs, strict=True)
            )
            want_flips = any(
                prefs["direction_changes"] and not _cap_reached(
                    count["flip"], prefs["daily_limit"],
                )
                for prefs, count in zip(configs, counts, strict=True)
            )
            if scope == "polymarket":
                want_qualified = want_research = want_flips = False
            elif scope == "forecasts":
                want_poly = False
            # All enabled producers start together. Polymarket has its own
            # delivery pipeline, so exchange evidence/DB work cannot hold a
            # fresh contract notice behind unrelated scans.
            budget = self._delivery_budget

            async def scan(name, producer):
                started = time.monotonic()
                try:
                    return await producer
                except Exception:  # noqa: BLE001 - independent source isolation
                    _LOG.exception("RivaL Auto-Hunt producer failed source=%s", name)
                    return []
                finally:
                    _LOG.info("RivaL Auto-Hunt scan source=%s scan_ms=%d", name,
                              int((time.monotonic() - started) * 1000))

            async def polymarket_pipeline() -> int:
                if not want_poly:
                    return 0
                markets = await scan("polymarket", self.polymarket.active())
                _LOG.info("RivaL Auto-Hunt poly_markets=%d", len(markets))

                async def deliver_poly(subscriber: dict) -> int:
                    if not subscriber.get("polymarket"):
                        return 0
                    user_id = str(subscriber["user_id"])
                    async with budget, self._user_lock(user_id):
                        if not (await asyncio.to_thread(self.store.tiers, user_id))["polymarket"]:
                            return 0
                        preferences = await asyncio.to_thread(self.store.preferences, user_id)
                        for candidate in markets:
                            stamp = datetime.now(UTC)
                            if (
                                not candidate_allowed(candidate, preferences, now=stamp)
                                or not self.polymarket._usable(
                                    candidate, now=stamp, display_only=False,
                                )
                            ):
                                continue
                            if preferences["daily_limit"] > 0 and _cap_reached(
                                await asyncio.to_thread(
                                    self.store.count_sent_today, user_id, "polymarket", stamp,
                                ), preferences["daily_limit"],
                            ):
                                break
                            if not await asyncio.to_thread(
                                self.store.claim, user_id, candidate, now=stamp,
                            ):
                                continue
                            message = await self._send_dm(user_id, candidate, stamp, tier=None)
                            await asyncio.to_thread(
                                self.store.finish, user_id, candidate,
                                sent=message is not None,
                                message_id=str(message.id) if message is not None else None,
                            )
                            return int(message is not None)
                    return 0

                outcomes = await asyncio.gather(
                    *(deliver_poly(sub) for sub in subscribers), return_exceptions=True,
                )
                return delivered(outcomes)

            def delivered(outcomes) -> int:
                total = 0
                for result in outcomes:
                    if isinstance(result, BaseException):
                        if isinstance(result, asyncio.CancelledError):
                            raise result
                        _LOG.error(
                            "Auto-Hunt individual delivery failed safely: %s",
                            type(result).__name__,
                            exc_info=(type(result), result, result.__traceback__),
                        )
                    else:
                        total += result
                return total

            async def deliver(subscriber: dict) -> int:
                user_id = str(subscriber["user_id"])
                since = _instant(str(subscriber["since_at"]))
                sent = 0
                async with budget, self._user_lock(user_id):
                    tiers = await asyncio.to_thread(self.store.tiers, user_id)
                    preferences = await asyncio.to_thread(self.store.preferences, user_id)
                    # QUALIFIED first: every distinct window alerts, with no
                    # cooldown and no per-cycle limit hiding a later window.
                    # A window with a SENT qualified DM suppresses the
                    # research alert for the same window, in this cycle and
                    # later ones; a failed qualified attempt does not, so a
                    # research fallback stays possible instead of duplicating.
                    claimed_windows: set[tuple[str, int, datetime]] = set()
                    if tiers.get("qualified"):
                        for candidate in qualified:
                            if not candidate_allowed(
                                candidate, preferences, now=datetime.now(UTC),
                            ):
                                continue
                            stamp = datetime.now(UTC)
                            if (
                                candidate.issued < since
                                or candidate.expires - stamp < MIN_REMAINING
                            ):
                                continue
                            if preferences["daily_limit"] > 0 and _cap_reached(
                                await asyncio.to_thread(
                                    self.store.count_sent_today, user_id, "qualified", stamp,
                                ),
                                preferences["daily_limit"],
                            ):
                                _LOG.info(
                                    "RivaL auto-hunt daily cap user=%s tier=qualified",
                                    user_id,
                                )
                                break
                            if (
                                candidate.quote_at is None
                                or not timedelta(0) <= stamp - candidate.quote_at
                                <= MAX_QUOTE_AGE
                            ):
                                continue
                            key = QUALIFIED_KEY_PREFIX + candidate.key
                            claimed = await asyncio.to_thread(
                                self.store.claim, user_id, candidate,
                                now=stamp, key=key, apply_cooldown=False,
                            )
                            if not claimed:
                                continue
                            message = await self._send_dm(
                                user_id, candidate, stamp, tier="qualified")
                            await asyncio.to_thread(
                                self.store.finish, user_id, candidate,
                                sent=message is not None,
                                message_id=(
                                    str(message.id) if message is not None
                                    else None
                                ),
                                key=key,
                            )
                            if message is not None:
                                sent += 1
                                claimed_windows.add(candidate.window)
                            _LOG.info(
                                "RivaL hunt asset=%s horizon=%s venue=%s user=%s "
                                "issue_to_attempt_ms=%d status=%s tier=qualified",
                                candidate.asset, candidate.horizon,
                                candidate.venue, user_id,
                                int((stamp - candidate.issued).total_seconds() * 1000),
                                "sent" if message is not None else "unconfirmed",
                            )
                    # EARLY_RESEARCH: at most one per cycle per user, under
                    # the existing 120-second cooldown, as before.
                    if tiers.get("research"):
                        for candidate in forecasts:
                            if not candidate_allowed(
                                candidate, preferences, now=datetime.now(UTC),
                            ):
                                continue
                            if candidate.window in claimed_windows:
                                continue
                            stamp = datetime.now(UTC)
                            if (
                                candidate.issued < since
                                or candidate.expires - stamp < MIN_REMAINING
                            ):
                                continue
                            if await asyncio.to_thread(
                                self.store.has_qualified_sent_window,
                                user_id, candidate.asset, candidate.horizon,
                                candidate.expires,
                            ):
                                continue
                            if preferences["daily_limit"] > 0 and _cap_reached(
                                await asyncio.to_thread(
                                    self.store.count_sent_today, user_id, "research", stamp,
                                ),
                                preferences["daily_limit"],
                            ):
                                _LOG.info(
                                    "RivaL auto-hunt daily cap user=%s tier=research",
                                    user_id,
                                )
                                break
                            claimed = await asyncio.to_thread(
                                self.store.claim, user_id, candidate, now=stamp,
                            )
                            if not claimed:
                                continue
                            message = await self._send_dm(
                                user_id, candidate, stamp, tier=None)
                            await asyncio.to_thread(
                                self.store.finish, user_id, candidate,
                                sent=message is not None,
                                message_id=(
                                    str(message.id) if message is not None
                                    else None
                                ),
                            )
                            if message is not None:
                                sent += 1
                            _LOG.info(
                                "RivaL hunt asset=%s horizon=%s venue=%s user=%s "
                                "issue_to_attempt_ms=%d status=%s tier=research",
                                candidate.asset, candidate.horizon,
                                candidate.venue, user_id,
                                int((stamp - candidate.issued).total_seconds() * 1000),
                                "sent" if message is not None else "unconfirmed",
                            )
                            break
                    # A model reversal is not every tick. It requires distinct
                    # newly issued source-backed forecasts on 5m candle windows.
                    if preferences["direction_changes"]:
                        for candidate in flips:
                            stamp = datetime.now(UTC)
                            if (
                                candidate.issued < since
                                or candidate.expires - stamp < MIN_REMAINING
                                or not candidate_allowed(
                                    candidate, preferences, now=stamp,
                                )
                            ):
                                continue
                            if preferences["daily_limit"] > 0 and _cap_reached(
                                await asyncio.to_thread(
                                    self.store.count_sent_today, user_id, "flip", stamp,
                                ),
                                preferences["daily_limit"],
                            ):
                                break
                            key = FLIP_KEY_PREFIX + candidate.key
                            claimed = await asyncio.to_thread(
                                self.store.claim, user_id, candidate,
                                now=stamp, key=key, apply_cooldown=False,
                            )
                            if not claimed:
                                continue
                            message = await self._send_dm(
                                user_id, candidate, stamp, tier="flip",
                            )
                            await asyncio.to_thread(
                                self.store.finish, user_id, candidate,
                                sent=message is not None,
                                message_id=str(message.id) if message else None,
                                key=key,
                            )
                            if message is not None:
                                sent += 1
                            break
                return sent

            async def forecast_pipeline() -> int:
                jobs = {}
                if want_qualified:
                    jobs["qualified"] = qualified_watches(
                        self.market, self.quotes, stream_shadow=self.stream_shadow,
                    )
                if want_research:
                    jobs["research"] = new_forecast_watches(self.market)
                if want_flips:
                    jobs["flip"] = direction_change_watches(self.market)
                results = await asyncio.gather(*(scan(name, job) for name, job in jobs.items()))
                by_tier = dict(zip(jobs, results, strict=True))
                # Qualified and early-research must finish together: a qualified
                # window retains priority and suppresses its duplicate research DM.
                nonlocal qualified, forecasts, flips
                qualified = by_tier.get("qualified", [])
                forecasts = by_tier.get("research", [])
                flips = by_tier.get("flip", [])
                _LOG.info(
                    "RivaL Auto-Hunt cycle subscribers=%d qualified=%d forecasts=%d",
                    len(subscribers), len(qualified), len(forecasts),
                )
                if not qualified and not forecasts and not flips:
                    return 0
                outcomes = await asyncio.gather(
                    *(deliver(sub) for sub in subscribers), return_exceptions=True,
                )
                return delivered(outcomes)

            qualified, forecasts, flips = [], [], []
            # Structured concurrency cancels both delivery pipelines together on
            # shutdown. Outbox claims retain the existing no-ambiguous-replay rule.
            async with asyncio.TaskGroup() as group:
                poly_task = group.create_task(polymarket_pipeline())
                forecast_task = group.create_task(forecast_pipeline())
            return poly_task.result() + forecast_task.result()
