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
    _venue_book,
    _venue_proof,
    model_strength,
    signal_embed,
)
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
# Qualified delivery keys are prefixed so one forecast can yield a research
# record and a qualified record without either claiming the other. Research
# and Polymarket keys stay unprefixed, so pre-reconcile delivery rows keep
# deduplicating exactly as before.
QUALIFIED_KEY_PREFIX = "qualified|"
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
                   CREATE TABLE IF NOT EXISTS rival_hunt_tiers (
                       user_id TEXT PRIMARY KEY,
                       research INTEGER NOT NULL DEFAULT 0,
                       qualified INTEGER NOT NULL DEFAULT 0,
                       polymarket INTEGER NOT NULL DEFAULT 0,
                       updated_at TEXT NOT NULL
                   );"""
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

    def count_sent_today(self, user_id: str, tier: str, now: datetime) -> int:
        """Sent alerts today in one tier's budget. Research and qualified
        budgets are separate; Polymarket rows are identified by venue."""
        if tier not in TIERS:
            raise ValueError("Unsupported alert tier")
        day_start = now.replace(hour=0, minute=0, second=0,
                                microsecond=0).isoformat()
        if tier == "qualified":
            condition = "forecast_key LIKE 'qualified|%'"
            params: tuple = (user_id, day_start)
        elif tier == "polymarket":
            condition = "venue = 'polymarket'"
            params = (user_id, day_start)
        else:
            condition = ("venue != 'polymarket' AND "
                         "forecast_key NOT LIKE 'qualified|%'")
            params = (user_id, day_start)
        with self.store.connect() as conn:
            row = conn.execute(
                f"""SELECT COUNT(*) AS n FROM rival_hunt_delivery
                    WHERE user_id = ? AND state = 'sent'
                      AND attempted_at >= ? AND {condition}""",
                params,
            ).fetchone()
        return int(row["n"])

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
            # Mirror the actual promoted model policy used by /rival-signal.
            picks: list[HuntCandidate] = []
            for horizon in HORIZONS:
                engine = await asyncio.to_thread(
                    promoted_engine, market.store, asset=asset,
                    horizon=horizon, now=stamp,
                )
                reader = recent_multi_watches if engine == "multi" else recent_watches
                watches = await asyncio.to_thread(
                    reader, market, asset=asset, now=stamp,
                    limit=40, dedupe_horizons=False,
                )
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
                    # Only newest honest forecast per asset/horizon, not all
                    # ten correlated exchanges reported as ten bets.
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
            try:
                await asyncio.wait_for(signal_embed(
                    market, quotes, asset=asset, horizon_minutes=horizon,
                    now=scan_at, stream_shadow=stream_shadow,
                    decision_out=decision,
                ), timeout=7)
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


def hunt_embed(
    candidate: HuntCandidate | PolymarketNotice, *, observed_at: datetime,
    tier: str | None = None,
) -> discord.Embed:
    if isinstance(candidate, PolymarketNotice):
        fill_line = (
            "10-share depth-weighted ask before fees: "
            + (f"Up **{candidate.up_average_ask:.1%}**"
               if candidate.up_average_ask is not None else "Up **insufficient depth**")
            + " • "
            + (f"Down **{candidate.down_average_ask:.1%}**"
               if candidate.down_average_ask is not None else "Down **insufficient depth**")
        )
        embed = discord.Embed(
            title=f"📊 Polymarket • {candidate.asset} {candidate.horizon}m market",
            description=(
                "**Listed Polymarket market, not a RivaL prediction.**\n"
                f"Up ask: **{candidate.up_ask:.1%}** per share • "
                f"Down ask: **{candidate.down_ask:.1%}** per share.\n"
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
        try:
            if row["venue"] == "polymarket":
                opened = _instant(row["issued_at"])
                fresh = await asyncio.wait_for(
                    service.polymarket.market(
                        row["asset"], int(row["horizon"]), opened=opened,
                    ), timeout=10,
                )
                if fresh is None:
                    embed = discord.Embed(
                        title="Polymarket market no longer verified",
                        description=(
                            "The listed market is expired, unavailable, or "
                            "lacks a fresh book. The original alert is NOT "
                            "a current quote. No order was placed."
                        ),
                    )
                else:
                    embed = hunt_embed(fresh, observed_at=datetime.now(UTC))
            else:
                embed = await asyncio.wait_for(
                    signal_embed(
                        service.market, service.quotes,
                        asset=row["asset"], horizon_minutes=int(row["horizon"]),
                        stream_shadow=getattr(interaction.client, "stream_shadow", None),
                    ), timeout=12,
                )
        except Exception:  # noqa: BLE001 - no stale quote masquerades as current
            embed = discord.Embed(
                title="Market review unavailable",
                description="No currently verified quote. Do not act on an old alert.",
            )
        await interaction.followup.send(embed=embed, ephemeral=True)
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
        if not any(flags.values()):
            return "OFF • all alert tiers off — choose one to start"
        try:
            health = await asyncio.to_thread(self.store.delivery_health, str(user_id))
        except Exception:  # noqa: BLE001
            return "ON • " + parts + " • Delivery health unavailable"
        uncertainty = sum(health.get(state, 0) for state in ("unknown", "sending", "attempted"))
        return (
            "ON • " + parts + f" • Sent {health.get('sent', 0)}"
            + (f" • Unconfirmed {uncertainty}" if uncertainty else "")
            + " • Future delivery not guaranteed"
        )

    async def _send_dm(self, user_id: str, candidate, stamp: datetime,
                       *, tier: str | None) -> object | None:
        """One DM attempt. Returns the message, or None when unconfirmed.

        The claim already exists by the time this runs, so an ambiguous
        failure here is never retried: a restart will NOT send it again.
        """
        message = None
        key = QUALIFIED_KEY_PREFIX + candidate.key if tier == "qualified" else candidate.key
        dispatch_started = False
        try:
            user = self.bot.get_user(int(user_id))
            if user is None:
                user = await asyncio.wait_for(
                    self.bot.fetch_user(int(user_id)),
                    timeout=5,
                )
            channel = user.dm_channel or await user.create_dm()
            # User lookup and DM creation can take longer than quote validity.
            dispatch_at = datetime.now(UTC)
            if tier == "qualified" and (
                candidate.quote_at is None
                or not timedelta(0) <= dispatch_at - candidate.quote_at <= MAX_QUOTE_AGE
                or candidate.expires - dispatch_at < MIN_REMAINING
            ):
                _LOG.info("Auto-Hunt qualified quote expired before send")
                return None
            if isinstance(candidate, PolymarketNotice) and (
                not timedelta(0) <= dispatch_at - candidate.quote_at
                <= timedelta(seconds=25)
                or candidate.expires - dispatch_at < MIN_REMAINING
            ):
                _LOG.info("Auto-Hunt Polymarket book expired before send")
                return None
            dispatch_started = await asyncio.to_thread(
                self.store.begin_delivery, user_id, candidate,
                key=key, now=dispatch_at,
            )
            if not dispatch_started:
                return None
            message = await asyncio.wait_for(
                channel.send(
                    embed=hunt_embed(candidate, observed_at=stamp, tier=tier),
                    view=AutoHuntActions(timeout=900),
                ), timeout=8,
            )
        except (discord.HTTPException, TimeoutError, ValueError):
            if not dispatch_started:
                await asyncio.to_thread(
                    self.store.defer_preflight, user_id, candidate, key=key,
                )
            _LOG.warning("Auto-Hunt DM not confirmed user=%s", user_id)
        except Exception:  # noqa: BLE001 - no ambiguous resend
            if not dispatch_started:
                await asyncio.to_thread(
                    self.store.defer_preflight, user_id, candidate, key=key,
                )
            _LOG.exception("Auto-Hunt DM failed")
        return message

    async def cycle(self) -> int:
        # Only one scan and one candidate set per cycle, regardless of users.
        async with self._cycle_lock:
            subscribers = await asyncio.to_thread(self.store.subscribers)
            if not subscribers:
                return 0
            want_qualified = any(s.get("qualified") for s in subscribers)
            want_research = any(s.get("research") for s in subscribers)
            want_poly = any(s.get("polymarket") for s in subscribers)
            qualified = (
                await qualified_watches(
                    self.market, self.quotes,
                    stream_shadow=self.stream_shadow,
                )
                if want_qualified else []
            )
            forecasts = (
                await new_forecast_watches(self.market)
                if want_research else []
            )
            markets = (
                await self.polymarket.active()
                if want_poly else []
            )
            _LOG.info(
                "RivaL Auto-Hunt cycle subscribers=%d qualified=%d "
                "forecasts=%d poly_markets=%d",
                len(subscribers), len(qualified), len(forecasts), len(markets),
            )
            if not qualified and not forecasts and not markets:
                return 0
            # Shared bounded Discord/PG concurrency. Each user's own lock
            # serializes delivery with that user's stop and re-opt-in request.
            budget = asyncio.Semaphore(8)

            async def deliver(subscriber: dict) -> int:
                user_id = str(subscriber["user_id"])
                since = _instant(str(subscriber["since_at"]))
                sent = 0
                async with budget, self._user_lock(user_id):
                    # QUALIFIED first: every distinct window alerts, with no
                    # cooldown and no per-cycle limit hiding a later window.
                    # A window with a SENT qualified DM suppresses the
                    # research alert for the same window, in this cycle and
                    # later ones; a failed qualified attempt does not, so a
                    # research fallback stays possible instead of duplicating.
                    claimed_windows: set[tuple[str, int, datetime]] = set()
                    if subscriber.get("qualified"):
                        for candidate in qualified:
                            stamp = datetime.now(UTC)
                            if (
                                candidate.issued < since
                                or candidate.expires - stamp < MIN_REMAINING
                            ):
                                continue
                            if await asyncio.to_thread(
                                self.store.count_sent_today, user_id,
                                "qualified", stamp,
                            ) >= DAILY_CAP_PER_TIER:
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
                    if subscriber.get("research"):
                        for candidate in forecasts:
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
                            if await asyncio.to_thread(
                                self.store.count_sent_today, user_id,
                                "research", stamp,
                            ) >= DAILY_CAP_PER_TIER:
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
                    # POLYMARKET: at most one per cycle per user, as before.
                    if subscriber.get("polymarket"):
                        for candidate in markets:
                            stamp = datetime.now(UTC)
                            if candidate.expires - stamp < MIN_REMAINING:
                                continue
                            if await asyncio.to_thread(
                                self.store.count_sent_today, user_id,
                                "polymarket", stamp,
                            ) >= DAILY_CAP_PER_TIER:
                                _LOG.info(
                                    "RivaL auto-hunt daily cap user=%s tier=polymarket",
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
                                "issue_to_attempt_ms=%d status=%s tier=polymarket",
                                candidate.asset, candidate.horizon,
                                candidate.venue, user_id,
                                int((stamp - candidate.issued).total_seconds() * 1000),
                                "sent" if message is not None else "unconfirmed",
                            )
                            break
                return sent

            outcomes = await asyncio.gather(
                *(deliver(sub) for sub in subscribers), return_exceptions=True,
            )
            total = 0
            for result in outcomes:
                if isinstance(result, BaseException):
                    if isinstance(result, asyncio.CancelledError):
                        raise result
                    _LOG.error(
                        "Auto-Hunt individual delivery failed safely: %s",
                        type(result).__name__,
                    )
                else:
                    total += result
            return total
