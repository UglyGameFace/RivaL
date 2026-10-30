"""Opt-in Auto-Hunt for newly issued, evidence-screened crypto research watches.

This is NOT a betting executor or a proof of profitable edge. The authoritative
forecast ledger and venue-specific order book are reused. DB claims are written
before each DM attempt, so a timeout never creates duplicate betting alerts.
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
    ASSETS,
    recent_multi_watches,
    recent_watches,
)
from parlay_bot.discord_app.signals import (
    MIN_REMAINING,
    _venue_book,
    _venue_proof,
    model_strength,
    signal_embed,
)
from parlay_bot.discord_app.polymarket_watch import PolymarketNotice, PolymarketReader
from parlay_bot.perpetuals.promotion import multi_summary, promoted_engine
from parlay_bot.storage.hot import RelationalHotStore

_LOG = logging.getLogger(__name__)
HORIZONS = (5, 15)
BOOK_VENUES = ("hyperliquid", "binance", "okx", "bitget")
MAX_USERS = 100
COOLDOWN = timedelta(seconds=120)
MAX_ISSUE_AGE = timedelta(seconds=90)
MAX_SPREAD_BPS = 25.0
# New research alerts are deliberately distinct from qualified trade signals.
# Don't silently discard every forecast just because live P&L is unproven.
FORECAST_ALERT_MAX_AGE = timedelta(minutes=3)


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
                   );"""
            )

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
            return updated.rowcount == 1

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
                          COALESCE(p.selected_source,'forecasts') AS source
                   FROM rival_hunt_subscriptions s
                   LEFT JOIN rival_hunt_source p ON p.user_id=s.user_id
                   WHERE s.enabled=1 ORDER BY s.user_id LIMIT ?""",
                (MAX_USERS,),
            ).fetchall()
            return [dict(row) for row in rows]

    def claim(self, user_id: str, candidate: HuntCandidate, *, now: datetime) -> bool:
        """Claim before network I/O. Even ambiguous DM errors are never retried."""
        with self.store.connect() as conn:
            eligible = conn.execute(
                """SELECT since_at,last_sent_at FROM rival_hunt_subscriptions
                   WHERE user_id=? AND enabled=1""",
                (user_id,),
            ).fetchone()
            if eligible is None:
                return False
            if candidate.venue != "polymarket" and (
                _instant(eligible["since_at"]) > candidate.issued
            ):
                return False
            if eligible["last_sent_at"] and (
                now - _instant(eligible["last_sent_at"]) < COOLDOWN
            ):
                return False
            result = conn.execute(
                """INSERT INTO rival_hunt_delivery
                   (user_id,forecast_key,asset,horizon,venue,issued_at,expires_at,
                    attempted_at,state)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(user_id,forecast_key) DO NOTHING""",
                (
                    user_id, candidate.key, candidate.asset, candidate.horizon,
                    candidate.venue, candidate.issued.isoformat(),
                    candidate.expires.isoformat(), now.isoformat(), "attempted",
                ),
            )
            if result.rowcount != 1:
                return False
            # Rate limit even uncertain attempts; reconnect must not fan out.
            conn.execute(
                """UPDATE rival_hunt_subscriptions SET last_sent_at=?
                   WHERE user_id=? AND enabled=1""",
                (now.isoformat(), user_id),
            )
            return True

    def finish(
        self, user_id: str, candidate: HuntCandidate, *, sent: bool,
        message_id: str | None = None,
    ) -> None:
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE rival_hunt_delivery SET state=?,message_id=?,delivered_at=?
                   WHERE user_id=? AND forecast_key=? AND state='attempted'""",
                (
                    "sent" if sent else "failed", message_id,
                    datetime.now(UTC).isoformat() if sent else None,
                    user_id, candidate.key,
                ),
            )

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


def hunt_embed(
    candidate: HuntCandidate | PolymarketNotice, *, observed_at: datetime,
) -> discord.Embed:
    if isinstance(candidate, PolymarketNotice):
        embed = discord.Embed(
            title=f"📊 Polymarket • {candidate.asset} {candidate.horizon}m market",
            description=(
                "**Listed Polymarket market, not a RivaL prediction.**\n"
                f"Up ask: **{candidate.up_ask:.1%}** per share • "
                f"Down ask: **{candidate.down_ask:.1%}** per share.\n"
                f"Book received <t:{int(candidate.quote_at.timestamp())}:R>.\n"
                f"Market closes <t:{int(candidate.expires.timestamp())}:R>.\n"
                f"[View exact event]({candidate.url})\n\n"
                "Quoted prices are NOT calibrated win probabilities or a "
                "verified edge. No bet placed. Availability varies by jurisdiction."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Read-only global Polymarket data • /rival-stop")
        return embed
    seconds = max(0, int((candidate.expires - observed_at).total_seconds()))
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
    def __init__(self, *, bot: discord.Client, market, quotes, store: AutoHuntStore) -> None:
        self.bot = bot
        self.market = market
        self.quotes = quotes
        self.store = store
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

    async def subscribe(
        self, user: discord.abc.User, *, source: str = "forecasts",
    ) -> tuple[bool, str]:
        if source not in ("forecasts", "polymarket"):
            return False, "Unsupported alert source."
        if source == "forecasts" and self.market is None:
            return False, "Live crypto forecasting unavailable; forecasts not enabled."
        async with self._user_lock(str(user.id)):
            try:
                enabled = await asyncio.to_thread(self.store.enabled, str(user.id))
                current = await asyncio.to_thread(self.store.source, str(user.id))
                if enabled and current == source:
                    return True, "Already following " + (
                        "Polymarket markets." if source == "polymarket" else
                        "RivaL forecasts."
                    )
                channel = user.dm_channel or await user.create_dm()
                await asyncio.wait_for(channel.send(
                    "🔔 Auto-Hunt source request: " +
                    ("Polymarket live Up/Down markets" if source == "polymarket"
                     else "RivaL new model predictions") +
                    ". The /rival menu confirms when enabled. No bet is placed."
                ), timeout=8)
                if not enabled:
                    await asyncio.to_thread(self.store.subscribe, str(user.id))
                await asyncio.to_thread(self.store.choose_source, str(user.id), source)
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

    async def status(self, user_id: int) -> str:
        try:
            active = await asyncio.to_thread(self.store.enabled, str(user_id))
        except Exception:  # noqa: BLE001 - storage fail closed
            return "Status unavailable. No new alert confirmed."
        if not active:
            return "OFF • choose a source to start"
        source = await asyncio.to_thread(self.store.source, str(user_id))
        return ("ON • Polymarket 5m/15m markets"
                if source == "polymarket" else "ON • RivaL new model forecasts")

    async def cycle(self) -> int:
        # Only one scan and one candidate set per cycle, regardless of users.
        async with self._cycle_lock:
            subscribers = await asyncio.to_thread(self.store.subscribers)
            if not subscribers:
                return 0
            sources = {sub.get("source", "forecasts") for sub in subscribers}
            forecasts = (
                await new_forecast_watches(self.market)
                if "forecasts" in sources else []
            )
            markets = (
                await self.polymarket.active()
                if "polymarket" in sources else []
            )
            _LOG.info(
                "RivaL Auto-Hunt cycle subscribers=%d forecasts=%d poly_markets=%d",
                len(subscribers), len(forecasts), len(markets),
            )
            if not forecasts and not markets:
                return 0
            # Shared bounded Discord/PG concurrency. Each user's own lock
            # serializes delivery with that user's stop and re-opt-in request.
            budget = asyncio.Semaphore(8)

            async def deliver(subscriber: dict) -> int:
                user_id = str(subscriber["user_id"])
                since = _instant(str(subscriber["since_at"]))
                source = subscriber.get("source", "forecasts")
                candidates = markets if source == "polymarket" else forecasts
                async with budget, self._user_lock(user_id):
                    for candidate in candidates:
                        stamp = datetime.now(UTC)
                        if (
                            (candidate.venue != "polymarket" and candidate.issued < since)
                            or candidate.expires - stamp < MIN_REMAINING
                        ):
                            continue
                        claimed = await asyncio.to_thread(
                            self.store.claim, user_id, candidate, now=stamp,
                        )
                        if not claimed:
                            continue
                        message = None
                        try:
                            user = self.bot.get_user(int(user_id))
                            if user is None:
                                user = await asyncio.wait_for(
                                    self.bot.fetch_user(int(user_id)),
                                    timeout=5,
                                )
                            channel = user.dm_channel or await user.create_dm()
                            message = await asyncio.wait_for(
                                channel.send(
                                    embed=hunt_embed(candidate, observed_at=stamp),
                                    view=AutoHuntActions(timeout=900),
                                ), timeout=8,
                            )
                        except (discord.HTTPException, TimeoutError, ValueError):
                            _LOG.warning(
                                "Auto-Hunt DM not confirmed user=%s", user_id,
                            )
                        except Exception:  # noqa: BLE001 - no ambiguous resend
                            _LOG.exception("Auto-Hunt DM failed")
                        # Claim already exists. If this acknowledgement
                        # fails, a restart will NOT send the same DM again.
                        await asyncio.to_thread(
                            self.store.finish, user_id, candidate,
                            sent=message is not None,
                            message_id=(
                                str(message.id) if message is not None else None
                            ),
                        )
                        _LOG.info(
                            "RivaL hunt asset=%s horizon=%s venue=%s user=%s "
                            "issue_to_attempt_ms=%d status=%s",
                            candidate.asset, candidate.horizon,
                            candidate.venue, user_id,
                            int((stamp - candidate.issued).total_seconds() * 1000),
                            "sent" if message is not None else "unconfirmed",
                        )
                        return int(message is not None)
                return 0

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
