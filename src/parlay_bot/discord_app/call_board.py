"""One durable, opt-in Discord channel panel for unqualified research calls.

Forecast direction and trade approval are separate. Never invent a future
issuance, a same-venue quote, stop-loss, profit target, or market edge.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from datetime import UTC, datetime, timedelta

import discord

from parlay_bot.discord_app.opportunities import recent_watches
from parlay_bot.discord_app.targeting import target_snapshot
from parlay_bot.perpetuals.markets import ASSETS, HORIZONS

_LOG = logging.getLogger(__name__)
MAX_BOARDS = 24
MAX_OWNER_BOARDS = 4
SCHEMA = """CREATE TABLE IF NOT EXISTS rival_live_call_board (
    channel_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    message_id TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    PRIMARY KEY(channel_id,asset)
)"""


def live_calls_embed(market, asset: str, now: datetime | None = None):
    """Use the same prospective forecast snapshot as /rival's manual screen."""
    if asset not in ASSETS:
        raise ValueError("Unsupported calls asset")
    clock = now or datetime.now(UTC)
    if clock.utcoffset() is None:
        raise ValueError("Calls clock must be timezone aware")
    clock = clock.astimezone(UTC)
    snapshots = [
        target_snapshot(market, asset=asset, horizon_minutes=h, now=clock)
        for h in HORIZONS
    ]
    try:
        next_rows = (recent_watches(
            market, asset=asset, now=clock, limit=40, dedupe_horizons=False,
        ) if market is not None else None)
    except Exception:  # noqa: BLE001 - future-issuance check is independent
        _LOG.warning("RivaL next call lookup unavailable", exc_info=True)
        next_rows = None
    embed = discord.Embed(
        title=f"RivaL Live Calls · {asset} · 5m / 15m / 60m",
        description=(
            "Prospectively issued UP/DOWN research forecasts remain visible "
            "even when no profitable trading edge is proven. "
            "**Unqualified means NO TRADE CALL.**"
        ),
        color=discord.Color.blurple(),
    )
    fingerprint = [asset]
    for h, snapshot in zip(HORIZONS, snapshots, strict=True):
        if (snapshot.direction in ("UP", "DOWN")
                and snapshot.status in ("ok", "weak_model", "unqualified_model")
                and snapshot.probability is not None):
            verdict = ("WATCH ONLY; profitability not proven" if snapshot.evidence_qualified
                       else "UNQUALIFIED RESEARCH; NO TRADE CALL")
            direction = "📈 UP" if snapshot.direction == "UP" else "📉 DOWN"
            text = (
                f"**{direction}** · {snapshot.probability:.1%} raw model strength "
                "(not a measured win rate)\n"
                f"Source: {snapshot.venue or 'unavailable'}\n"
                f"**{verdict}**"
            )
            if snapshot.price is not None and math.isfinite(snapshot.price):
                text += f"\nReference candle: USD {snapshot.price:,.4f} (not a live bid/ask)"
            if snapshot.issued_at is not None:
                text += "\nIssued " + discord.utils.format_dt(snapshot.issued_at, style="R")
            if snapshot.ends_at is not None:
                text += "\nWindow ends " + discord.utils.format_dt(snapshot.ends_at, style="R")
        else:
            text = "**NO CURRENT FORECAST**\n" + snapshot.detail

        next_line = "Next issued target: not yet available"
        if next_rows is None:
            next_line = "Next issued target: lookup unavailable"
        elif snapshot.ends_at is not None and snapshot.venue is not None:
            later = []
            for row in next_rows:
                if (row["horizon_minutes"] != h or
                        row["venue"] != snapshot.venue or row["issued"] > clock):
                    continue
                p = float(row["probability_up"])
                target = row["target"]
                if (target + timedelta(minutes=5) <= snapshot.ends_at
                        or not math.isfinite(p) or not 0 < p < 1):
                    continue
                later.append((target, p))
            if later:
                target, p = min(later, key=lambda pair: pair[0])
                direction = "UP" if p > 0.5 else "DOWN" if p < 0.5 else "FLAT"
                next_line = (
                    "Next *already-issued* target: "
                    + discord.utils.format_dt(target, style="t")
                    + f" · {direction} (research only)"
                )
        embed.add_field(name=f"{h}-minute call", value=text + "\n" + next_line,
                        inline=False)
        fingerprint.extend((str(h), snapshot.fingerprint, next_line))
    embed.add_field(
        name="Entry · SL · TP1 · TP2 · R:R",
        value=("**Not verified.** No stop, targets, or risk/reward will be "
               "fabricated from raw direction or a reference candle."),
        inline=False,
    )
    embed.set_footer(text="One edited channel card · Research only · No real orders")
    return embed, hashlib.sha256("|".join(fingerprint).encode()).hexdigest()


class CallBoardView(discord.ui.View):
    def __init__(self, service) -> None:
        super().__init__(timeout=None)
        self.service = service
        for action, label, emoji, style in (
            ("refresh", "Refresh", "🔄", discord.ButtonStyle.secondary),
            ("stop", "Stop", "🔕", discord.ButtonStyle.danger),
        ):
            button = discord.ui.Button(
                label=label, emoji=emoji, style=style,
                custom_id=f"rival:live-calls:{action}",
            )
            button.callback = self._callback(action)
            self.add_item(button)

    def _callback(self, action):
        async def handle(interaction: discord.Interaction):
            await self.service.handle(interaction, action)
        return handle


class CallBoardService:
    """Restart-safe, one message per channel/asset, owner controlled."""

    def __init__(self, bot, store, market) -> None:
        self.bot, self.store, self.market = bot, store, market
        self.view = CallBoardView(self)
        self._lock = asyncio.Lock()
        self._ready = False

    def initialize(self) -> None:
        if not self._ready:
            with self.store.connect() as conn:
                conn.execute(SCHEMA)
            self._ready = True

    def _rows(self) -> list[dict]:
        self.initialize()
        with self.store.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM rival_live_call_board ORDER BY channel_id,asset"
            ).fetchall()]

    def _save(self, channel, asset, message, owner, fingerprint) -> None:
        with self.store.connect() as conn:
            conn.execute(
                """INSERT INTO rival_live_call_board
                (channel_id,asset,message_id,owner_id,fingerprint) VALUES (?,?,?,?,?)""",
                (str(channel), asset, str(message), str(owner), fingerprint),
            )

    def _delete(self, row: dict) -> None:
        with self.store.connect() as conn:
            conn.execute(
                """DELETE FROM rival_live_call_board
                WHERE channel_id=? AND asset=? AND message_id=?""",
                (row["channel_id"], row["asset"], row["message_id"]),
            )

    async def start(self, interaction: discord.Interaction, asset: str) -> str:
        if asset not in ASSETS:
            raise ValueError("Unsupported calls asset")
        channel = interaction.channel
        if channel is None or getattr(interaction, "guild_id", None) is None:
            return "Use a server channel to start Live Calls. No DM was sent."
        async with self._lock:
            rows = await asyncio.to_thread(self._rows)
            if any(r["channel_id"] == str(channel.id) and r["asset"] == asset
                   for r in rows):
                return f"{asset} already has a Live Calls card here."
            if len(rows) >= MAX_BOARDS:
                return "Live Calls capacity reached; stop an old panel first."
            if sum(r["owner_id"] == str(interaction.user.id)
                   for r in rows) >= MAX_OWNER_BOARDS:
                return "You already have four Live Calls panels. Stop one first."
            embed, key = await asyncio.to_thread(live_calls_embed, self.market, asset)
            try:
                msg = await asyncio.wait_for(channel.send(embed=embed, view=self.view),
                                             timeout=8)
            except (discord.HTTPException, AttributeError, TimeoutError):
                return "Cannot post here; check Send Messages permission."
            try:
                await asyncio.to_thread(
                    self._save, channel.id, asset, msg.id, interaction.user.id, key,
                )
            except Exception:
                _LOG.exception("RivaL Live Calls persistence failed")
                try:
                    await msg.delete()
                except (discord.HTTPException, AttributeError):
                    pass
                return "Could not save this panel; no active tracking created."
            return (f"Posted {asset} 5m/15m/60m Live Calls in this channel. "
                    "One message updates automatically, without DMs.")

    async def _edit(self, row: dict, card, key: str, *, force: bool = False) -> bool:
        if not force and row["fingerprint"] == key:
            return False
        msg = self.bot.get_partial_messageable(
            int(row["channel_id"])
        ).get_partial_message(int(row["message_id"]))
        try:
            await asyncio.wait_for(msg.edit(embed=card, view=self.view), timeout=8)
        except discord.NotFound:
            await asyncio.to_thread(self._delete, row)
            return False
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE rival_live_call_board SET fingerprint=?
                WHERE channel_id=? AND asset=? AND message_id=?""",
                (key, row["channel_id"], row["asset"], row["message_id"]),
            )
        return True

    async def cycle(self) -> int:
        """Use the existing target-refresh loop; one read per asset per cycle."""
        async with self._lock:
            rows = await asyncio.to_thread(self._rows)
            cards = {}
            count = 0
            for row in rows:
                try:
                    if row["asset"] not in cards:
                        cards[row["asset"]] = await asyncio.to_thread(
                            live_calls_embed, self.market, row["asset"],
                        )
                    count += await self._edit(row, *cards[row["asset"]])
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one channel never stops others
                    _LOG.warning("Live Calls update failed", exc_info=True)
            return count

    async def _stop(self, row: dict) -> None:
        await asyncio.to_thread(self._delete, row)
        try:
            msg = self.bot.get_partial_messageable(
                int(row["channel_id"])
            ).get_partial_message(int(row["message_id"]))
            await asyncio.wait_for(msg.edit(
                embed=discord.Embed(
                    title=f"RivaL Live Calls · {row['asset']} · stopped",
                    description="This message no longer tracks forecasts.",
                    color=discord.Color.light_grey(),
                ), view=None,
            ), timeout=8)
        except (discord.HTTPException, AttributeError, TimeoutError):
            _LOG.info("Stopped calls row; channel message edit unavailable")

    async def stop_owner(self, owner_id: int) -> bool:
        async with self._lock:
            rows = [r for r in await asyncio.to_thread(self._rows)
                    if r["owner_id"] == str(owner_id)]
            for row in rows:
                await self._stop(row)
            return bool(rows)

    async def handle(self, interaction: discord.Interaction, action: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        mid = str(interaction.message.id) if interaction.message else ""
        async with self._lock:
            row = next((r for r in await asyncio.to_thread(self._rows)
                        if r["message_id"] == mid), None)
            if row is None:
                result = "Live Calls panel is no longer active."
            elif row["owner_id"] != str(interaction.user.id):
                result = "Only the person who started this panel can control it."
            elif action == "stop":
                await self._stop(row)
                result = "Live Calls panel stopped."
            elif action == "refresh":
                try:
                    card, key = await asyncio.to_thread(
                        live_calls_embed, self.market, row["asset"],
                    )
                    await self._edit(row, card, key, force=True)
                    result = "Live Calls refreshed."
                except Exception:  # noqa: BLE001
                    _LOG.warning("Manual calls refresh failed", exc_info=True)
                    result = "Could not verify fresh calls; retry."
            else:
                result = "Unknown calls action."
        await interaction.followup.send(result, ephemeral=True)
