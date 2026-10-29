"""Private, persistent one-message RivaL direction targets.

A target consumes the existing *prospective* ledger, not imported historical
candles. Updating the existing DM requires no interaction-token refresh and
survives a bot restart. The user can cancel any time.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import discord

from parlay_bot.discord_app.opportunities import ASSETS, _cash, recent_watches
from parlay_bot.storage.hot import RelationalHotStore

if TYPE_CHECKING:
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore

_LOG = logging.getLogger(__name__)
HORIZONS = (5, 15)
MAX_TARGETS = 100


@dataclass(frozen=True)
class DirectionTarget:
    user_id: str
    channel_id: str
    message_id: str
    asset: str
    horizon_minutes: int
    fingerprint: str


@dataclass(frozen=True)
class TargetSnapshot:
    asset: str
    horizon_minutes: int
    direction: str
    detail: str
    venue: str | None = None
    probability: float | None = None
    price: float | None = None
    issued_at: datetime | None = None
    ends_at: datetime | None = None

    @property
    def fingerprint(self) -> str:
        """Exclude wall-clock time so unchanged observations never trigger edits."""
        raw = json.dumps({
            "asset": self.asset,
            "horizon": self.horizon_minutes,
            "direction": self.direction,
            "detail": self.detail,
            "venue": self.venue,
            "probability": (round(self.probability, 5)
                            if self.probability is not None else None),
            "price": round(self.price, 8) if self.price is not None else None,
            "issued": self.issued_at.isoformat() if self.issued_at else None,
            "ends": self.ends_at.isoformat() if self.ends_at else None,
        }, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()


def target_snapshot(
    market: PerpetualMarketStore | None, *, asset: str,
    horizon_minutes: int, now: datetime | None = None,
) -> TargetSnapshot:
    if asset not in ASSETS or horizon_minutes not in HORIZONS:
        raise ValueError("choose BTC/ETH/SOL and 5m or 15m")
    stamp = now or datetime.now(UTC)
    if stamp.utcoffset() is None:
        raise ValueError("target clock requires timezone")
    if market is None:
        return TargetSnapshot(
            asset, horizon_minutes, "WAIT",
            "Live market collector is unavailable.",
        )
    try:
        watches = recent_watches(market, asset=asset, now=stamp, limit=5)
    except Exception:  # noqa: BLE001 - no internal SQL/auth details in Discord
        _LOG.exception("RivaL target observation unavailable")
        return TargetSnapshot(
            asset, horizon_minutes, "WAIT",
            "Cannot verify the current forecast or market feed.",
        )
    for watch in watches:
        if watch["horizon_minutes"] != horizon_minutes:
            continue
        # A prediction applies to its future interval, not forever.
        # Both 5m and 15m models predict a future five-minute candle;
        # the 15m horizon advances its opening, not its duration.
        ends = watch["target"] + timedelta(minutes=5)
        if stamp >= ends or watch["issued"] > stamp:
            continue
        p = float(watch["probability_up"])
        if not math.isfinite(p) or not 0 < p < 1:
            continue
        direction = "UP" if p > 0.5 else "DOWN" if p < 0.5 else "WAIT"
        if direction == "WAIT":
            return TargetSnapshot(
                asset, horizon_minutes, "WAIT",
                "The current model is tied; there is no directional call.",
            )
        return TargetSnapshot(
            asset=asset, horizon_minutes=horizon_minutes,
            direction=direction,
            detail="Prospective model watch. Not an approved trade.",
            venue=str(watch["venue"]),
            probability=max(p, 1 - p),
            price=float(watch["price"]),
            issued_at=watch["issued"],
            ends_at=ends,
        )
    return TargetSnapshot(
        asset, horizon_minutes, "WAIT",
        "No fresh unexpired prediction for this timeframe. Checking again automatically.",
    )


def target_embed(snapshot: TargetSnapshot) -> discord.Embed:
    labels = {"UP": "📈 UP", "DOWN": "📉 DOWN", "WAIT": "⏳ WAIT"}
    colors = {
        "UP": discord.Color.green(),
        "DOWN": discord.Color.red(),
        "WAIT": discord.Color.orange(),
    }
    embed = discord.Embed(
        title=f"{snapshot.asset} • {snapshot.horizon_minutes}m • {labels[snapshot.direction]}",
        description=f"**{snapshot.detail}**",
        color=colors[snapshot.direction],
    )
    if snapshot.price is not None and snapshot.probability is not None:
        embed.add_field(
            name="Latest verified candle",
            value=_cash(snapshot.price), inline=True,
        )
        embed.add_field(
            name="Raw model probability",
            value=f"{snapshot.probability:.1%}", inline=True,
        )
        embed.add_field(
            name="Source",
            value=snapshot.venue or "Unavailable", inline=True,
        )
        if snapshot.ends_at is not None:
            embed.add_field(
                name="Forecast window ends",
                value=discord.utils.format_dt(snapshot.ends_at, style="R"),
                inline=False,
            )
    embed.set_footer(
        text="Auto-updates when the call changes • Not a guaranteed or executable bet • /rival-stop",
    )
    return embed


class DirectionTargetStore:
    def __init__(self, store: RelationalHotStore) -> None:
        self.store = store

    def initialize(self) -> None:
        with self.store.connect() as conn:
            conn.executescript(
                """CREATE TABLE IF NOT EXISTS rival_direction_targets (
                     user_id TEXT PRIMARY KEY,
                     channel_id TEXT NOT NULL,
                     message_id TEXT NOT NULL,
                     asset TEXT NOT NULL,
                     horizon_minutes INTEGER NOT NULL,
                     fingerprint TEXT NOT NULL,
                     updated_at TEXT NOT NULL
                   );"""
            )

    @staticmethod
    def _target(row: dict) -> DirectionTarget:
        return DirectionTarget(
            str(row["user_id"]), str(row["channel_id"]),
            str(row["message_id"]), str(row["asset"]),
            int(row["horizon_minutes"]), str(row["fingerprint"]),
        )

    def get(self, user_id: str) -> DirectionTarget | None:
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM rival_direction_targets WHERE user_id=?",
                (user_id,),
            ).fetchone()
        return self._target(dict(row)) if row else None

    def active(self) -> list[DirectionTarget]:
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM rival_direction_targets ORDER BY user_id LIMIT ?",
                (MAX_TARGETS,),
            ).fetchall()
        return [self._target(dict(row)) for row in rows]

    def can_follow(self, user_id: str) -> bool:
        """An existing user may retarget; new watches respect a bounded budget."""
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM rival_direction_targets WHERE user_id=?",
                (user_id,),
            ).fetchone()
            if row is not None:
                return True
            total = conn.execute(
                "SELECT COUNT(*) AS total FROM rival_direction_targets",
            ).fetchone()
            return int(total["total"]) < MAX_TARGETS

    def save(self, target: DirectionTarget) -> None:
        if target.asset not in ASSETS or target.horizon_minutes not in HORIZONS:
            raise ValueError("invalid target")
        if not all(value.isdecimal() and int(value) > 0 for value in (
            target.user_id, target.channel_id, target.message_id,
        )):
            raise ValueError("invalid Discord identity")
        self.initialize()
        with self.store.connect() as conn:
            present = conn.execute(
                "SELECT 1 FROM rival_direction_targets WHERE user_id=?",
                (target.user_id,),
            ).fetchone()
            if present is None:
                count = conn.execute(
                    "SELECT COUNT(*) AS total FROM rival_direction_targets",
                ).fetchone()
                if int(count["total"]) >= MAX_TARGETS:
                    raise ValueError("Target slots are full; existing watches continue.")
            conn.execute(
                """INSERT INTO rival_direction_targets
                   (user_id,channel_id,message_id,asset,horizon_minutes,
                    fingerprint,updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     channel_id=excluded.channel_id,
                     message_id=excluded.message_id,
                     asset=excluded.asset,
                     horizon_minutes=excluded.horizon_minutes,
                     fingerprint=excluded.fingerprint,
                     updated_at=excluded.updated_at""",
                (
                    target.user_id, target.channel_id, target.message_id,
                    target.asset, target.horizon_minutes, target.fingerprint,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def updated(self, previous: DirectionTarget, fingerprint: str) -> None:
        """No other target or message can inherit an old updater's digest."""
        self.initialize()
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE rival_direction_targets SET fingerprint=?, updated_at=?
                   WHERE user_id=? AND channel_id=? AND message_id=?
                     AND asset=? AND horizon_minutes=?""",
                (
                    fingerprint, datetime.now(UTC).isoformat(),
                    previous.user_id, previous.channel_id, previous.message_id,
                    previous.asset, previous.horizon_minutes,
                ),
            )

    def stop(self, user_id: str, *, message_id: str | None = None) -> None:
        self.initialize()
        with self.store.connect() as conn:
            if message_id is None:
                conn.execute(
                    "DELETE FROM rival_direction_targets WHERE user_id=?",
                    (user_id,),
                )
            else:
                conn.execute(
                    """DELETE FROM rival_direction_targets
                       WHERE user_id=? AND message_id=?""",
                    (user_id, message_id),
                )


class DirectionTargetService:
    def __init__(self, *, bot: discord.Client,
                 market: PerpetualMarketStore | None,
                 store: DirectionTargetStore) -> None:
        self.bot = bot
        self.market = market
        self.store = store
        self.lock = asyncio.Lock()

    async def follow(
        self, user: discord.abc.User, *, asset: str,
        horizon_minutes: int,
    ) -> tuple[bool, str]:
        """Only a direct /rival-target interaction opts a user into DMs."""
        if asset not in ASSETS or horizon_minutes not in HORIZONS:
            raise ValueError("invalid target")
        async with self.lock:
            existing = await asyncio.to_thread(self.store.get, str(user.id))
            if not await asyncio.to_thread(self.store.can_follow, str(user.id)):
                return False, "Personal watch capacity is full. Try again later."
            snapshot = await asyncio.to_thread(
                target_snapshot, self.market,
                asset=asset, horizon_minutes=horizon_minutes,
            )
            try:
                channel = user.dm_channel or await user.create_dm()
                message = None
                if existing is not None and str(channel.id) == existing.channel_id:
                    try:
                        message = await channel.fetch_message(int(existing.message_id))
                        await message.edit(embed=target_embed(snapshot))
                    except discord.NotFound:
                        message = None
                if message is None:
                    message = await channel.send(embed=target_embed(snapshot))
            except (discord.Forbidden, discord.HTTPException):
                return False, "I couldn't DM you. Enable DMs for RivaL, then retry."
            target = DirectionTarget(
                str(user.id), str(channel.id), str(message.id),
                asset, horizon_minutes, snapshot.fingerprint,
            )
            await asyncio.to_thread(self.store.save, target)
            return True, (
                f"Watching **{asset} {horizon_minutes}m** in your DMs. "
                "The same message will change to UP, DOWN, or WAIT when evidence changes. "
                "Use /rival-stop to cancel."
            )

    async def stop(self, user_id: int) -> bool:
        async with self.lock:
            existing = await asyncio.to_thread(self.store.get, str(user_id))
            if existing is None:
                return False
            await asyncio.to_thread(self.store.stop, str(user_id))
            # Do not leave a stale UP or DOWN card posing as an active watch.
            try:
                channel = self.bot.get_channel(int(existing.channel_id))
                if channel is None:
                    channel = await self.bot.fetch_channel(int(existing.channel_id))
                message = await channel.fetch_message(int(existing.message_id))
                await message.edit(embed=discord.Embed(
                    title="RivaL • Target stopped",
                    description="Monitoring is off. Use /rival-target to watch again.",
                    color=discord.Color.light_grey(),
                ))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                _LOG.info("Stopped target; original DM could not be updated")
            return True

    async def cycle(self) -> int:
        """Bounded read-only forecast snapshots; edit only changed existing DMs."""
        # Even without the collector, old cards must turn to WAIT. Never
        # leave a green/red forecast frozen after a production outage.
        async with self.lock:
            targets = await asyncio.to_thread(self.store.active)
            cache: dict[tuple[str, int], TargetSnapshot] = {}
            changed = 0
            for target in targets:
                key = (target.asset, target.horizon_minutes)
                if key not in cache:
                    cache[key] = await asyncio.to_thread(
                        target_snapshot, self.market,
                        asset=target.asset, horizon_minutes=target.horizon_minutes,
                    )
                snapshot = cache[key]
                if snapshot.fingerprint == target.fingerprint:
                    continue
                try:
                    channel = self.bot.get_channel(int(target.channel_id))
                    if channel is None:
                        channel = await self.bot.fetch_channel(int(target.channel_id))
                    message = await channel.fetch_message(int(target.message_id))
                    await message.edit(embed=target_embed(snapshot))
                except (discord.NotFound, discord.Forbidden):
                    # A DM was deleted or access revoked; no replacement spam.
                    await asyncio.to_thread(
                        self.store.stop, target.user_id, message_id=target.message_id,
                    )
                except discord.HTTPException:
                    _LOG.warning("RivaL target update failed transiently for a DM")
                else:
                    await asyncio.to_thread(
                        self.store.updated, target, snapshot.fingerprint,
                    )
                    changed += 1
            return changed
