"""Watch several models at once: a side-by-side card and a live watch post.

The compare card puts up to five models on trial next to each other, one
small table per timeframe (its trial day, results, how often its calls came
true, its edge over the main model, and where it stands). The live watch
post is that card posted in a channel; RivaL edits it every
``REFRESH_SECONDS`` and it survives restarts (``rival_model_watch``). Its
Stop button works only for the person who started it.

Every number comes from ``model_detail.model_report`` (the hourly promotion
check, early stop and the test-model ledger), so the compare card and each
model's own page always agree.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

import discord

from parlay_bot.discord_app.model_detail import HORIZONS, model_report
from parlay_bot.discord_app.trial_board import (
    _ORDER,
    LEVEL_WITHIN,
    MIN_DAYS,
    TOO_FEW,
    _tiny_count,
    grouped,
    model_line,
    short_name,
)

_LOG = logging.getLogger(__name__)

MAX_MODELS = 5
MAX_WATCHES_PER_OWNER = 3
REFRESH_SECONDS = 300

SCHEMA = """CREATE TABLE IF NOT EXISTS rival_model_watch (
    message_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, owner_id TEXT NOT NULL,
    asset TEXT NOT NULL, models_json TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT)"""


def _pts(gain: float | None) -> str:
    if gain is None:
        return "—"
    points = gain * 1000
    return f"{points:+.1f}" if abs(points) < 100 else f"{points:+.0f}"[:5]


def _status(h: dict) -> str:
    if h["passed"]:
        return "PASSED"
    if h["exchanges"] and h["cut"] >= len(h["exchanges"]):
        return "cut"
    if h["results"] < TOO_FEW or h["gain"] is None:
        return "early"
    return ("ahead" if h["gain"] > LEVEL_WITHIN
            else "behind" if h["gain"] < -LEVEL_WITHIN else "level")


def compare_embed(reports: dict[str, dict | None], *, asset: str,
                  live: bool = False, now: datetime | None = None) -> discord.Embed:
    """Up to five models side by side, one table per timeframe that has data."""
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    names = list(reports)
    from parlay_bot.perpetuals.model_lab import family_names

    lab_names = family_names([name for name in reports if name.startswith("lab_")])
    labels = {name: (report or {}).get("name") or lab_names.get(name)
              or (short_name(name) if name.startswith("Lab ") else name)
              for name, report in reports.items()}
    embed = discord.Embed(
        title=f"{'📡 Live watch' if live else '🧪 Compare'} · {asset} · {len(names)} models",
        description=("Each model next to the others, scored like the main model on the "
                     "same candles. Edge = less error than the main model, in points "
                     "(+ is better)."),
        color=discord.Color.blurple(),
    )
    missing = [labels[name] for name, report in reports.items() if not report or not report["horizons"]]
    for horizon in HORIZONS:
        rows = []
        for name, report in reports.items():
            h = (report or {}).get("horizons", {}).get(horizon)
            if h is None:
                continue
            led = h["ledger"]
            right = ("?" if not h.get("ledger_ok", True)
                     else f"{led['right'] / led['n']:.0%}" if led["n"] >= TOO_FEW else "few")
            day = f"{min(h['days'], 99)}/{MIN_DAYS}"
            line = model_line(labels[name], day, _tiny_count(h["results"]), right, _pts(h["gain"]),
                              expanded=str(h.get("family", "")).startswith("lab_"))
            rows.append((labels[name], h, line))
        if not rows:
            continue
        # Same phone-width layout as Bot Performance: grouped by status.
        ordered = sorted(rows, key=lambda r: (
            _ORDER[_status(r[1])], -(r[1]["gain"] if r[1]["gain"] is not None else -9)))
        table = grouped(f"{'Model':<11} {'Day':>5} {'Res':>4} {'Right':>5} {'Edge':>5}",
                        [(_status(h), line) for _, h, line in ordered])
        judged = [(name, h) for name, h, _ in rows
                  if h["results"] >= TOO_FEW and h["gain"] is not None]
        lead = ""
        if judged:
            name, h = max(judged, key=lambda r: r[1]["gain"])
            lead = (f"Best here: **{name}** ({_pts(h['gain'])} points, day "
                    f"{h['days']}/{MIN_DAYS})." if h["gain"] > LEVEL_WITHIN
                    else "None of these is beating the main model here yet.")
        embed.add_field(name=f"📏 {horizon}-min",
                        value=table + lead, inline=False)
    if missing:
        embed.add_field(name="Not on trial here",
                        value=", ".join(missing)[:300] + ": no recent results for this coin "
                              "(retired, or not scored yet).", inline=False)
    footer = "Research only. No real trades."
    if live:
        footer += f" · Updates every {REFRESH_SECONDS // 60} min · last {stamp:%H:%M} UTC"
    embed.set_footer(text=footer)
    embed.timestamp = stamp
    return embed


def reports_for(store, asset: str, names: list[str]) -> dict[str, dict | None]:
    return {name: model_report(store, asset, name) for name in names[:MAX_MODELS]}


class _WatchButton(discord.ui.Button["ModelWatchView"]):
    def __init__(self, action: str, label: str, emoji: str, style) -> None:
        super().__init__(label=label, emoji=emoji, style=style,
                         custom_id=f"rival:watch:{action}")
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        await self.view.service.handle(interaction, self.action)


class ModelWatchView(discord.ui.View):
    """Restart-persistent controls on every live watch post."""

    def __init__(self, service: ModelWatchService) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.add_item(_WatchButton("refresh", "Refresh now", "🔄",
                                   discord.ButtonStyle.secondary))
        self.add_item(_WatchButton("stop", "Stop watching", "🔕",
                                   discord.ButtonStyle.danger))


class ModelWatchService:
    def __init__(self, bot, store) -> None:
        self.bot = bot
        self.store = store
        self.view = ModelWatchView(self)
        self._ready = False
        self._lock = asyncio.Lock()

    def initialize(self) -> None:
        if self._ready:
            return
        with self.store.connect() as conn:
            conn.execute(SCHEMA)
        self._ready = True

    def _rows(self, owner_id: str | None = None) -> list[dict]:
        self.initialize()
        with self.store.connect() as conn:
            if owner_id is None:
                rows = conn.execute("SELECT * FROM rival_model_watch").fetchall()
            else:
                rows = conn.execute("SELECT * FROM rival_model_watch WHERE owner_id=?",
                                    (owner_id,)).fetchall()
        return [dict(r) for r in rows]

    def _embed(self, asset: str, names: list[str]) -> discord.Embed:
        return compare_embed(reports_for(self.store, asset, names), asset=asset, live=True)

    async def start(self, interaction: discord.Interaction, asset: str,
                    names: list[str]) -> str:
        """Post a live watch in this channel; returns what to tell the user."""
        names = list(dict.fromkeys(names))[:MAX_MODELS]
        if not names:
            return "Pick at least one model to watch."
        owner = str(interaction.user.id)
        existing = await asyncio.to_thread(self._rows, owner)
        if len(existing) >= MAX_WATCHES_PER_OWNER:
            return (f"You already have {MAX_WATCHES_PER_OWNER} live watches. Press "
                    "Stop watching on one of them first.")
        embed = await asyncio.to_thread(self._embed, asset, names)
        channel = interaction.channel
        try:
            message = await asyncio.wait_for(channel.send(embed=embed, view=self.view),
                                             timeout=8)
        except (AttributeError, discord.HTTPException, TimeoutError):
            # No permission to post here: send it to the owner's DMs instead.
            try:
                dm = interaction.user.dm_channel or await interaction.user.create_dm()
                message = await asyncio.wait_for(dm.send(embed=embed, view=self.view),
                                                 timeout=8)
            except (discord.HTTPException, TimeoutError):
                return "Couldn't post the live watch here or in your DMs."
        now = datetime.now(UTC).isoformat()

        def save() -> None:
            self.initialize()
            with self.store.connect() as conn:
                conn.execute(
                    """INSERT INTO rival_model_watch (message_id,channel_id,owner_id,asset,
                         models_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?)""",
                    (str(message.id), str(message.channel.id), owner, asset,
                     json.dumps(names), now, now))

        await asyncio.to_thread(save)
        from parlay_bot.perpetuals.model_lab import family_names

        lab_labels = family_names([n for n in names if n.startswith("lab_")])
        labels = [lab_labels.get(n, n) for n in names]
        return (f"📡 Live watch posted: {', '.join(labels)} on {asset}, updating every "
                f"{REFRESH_SECONDS // 60} minutes.")

    async def _edit(self, row: dict) -> bool:
        names = json.loads(row["models_json"])
        embed = await asyncio.to_thread(self._embed, row["asset"], names)
        channel = self.bot.get_partial_messageable(int(row["channel_id"]))
        try:
            await asyncio.wait_for(channel.get_partial_message(int(row["message_id"])).edit(
                embed=embed, view=self.view), timeout=8)
        except discord.NotFound:
            await asyncio.to_thread(self._delete, row["message_id"])  # post was deleted
            return False
        stamp = datetime.now(UTC).isoformat()

        def touch() -> None:
            with self.store.connect() as conn:
                conn.execute("UPDATE rival_model_watch SET updated_at=? WHERE message_id=?",
                             (stamp, row["message_id"]))

        await asyncio.to_thread(touch)
        return True

    def _delete(self, message_id: str) -> None:
        self.initialize()
        with self.store.connect() as conn:
            conn.execute("DELETE FROM rival_model_watch WHERE message_id=?", (message_id,))

    async def cycle(self) -> int:
        """Refresh every live watch post (one at a time; reports are small reads)."""
        async with self._lock:
            updated = 0
            for row in await asyncio.to_thread(self._rows):
                try:
                    updated += await self._edit(row)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one post never stops the others
                    _LOG.warning("RivaL live model watch update failed message=%s",
                                 row["message_id"], exc_info=True)
            return updated

    async def handle(self, interaction: discord.Interaction, action: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        message_id = str(interaction.message.id) if interaction.message else ""
        rows = [r for r in await asyncio.to_thread(self._rows) if r["message_id"] == message_id]
        if not rows:
            await interaction.followup.send("This watch post is no longer active.",
                                            ephemeral=True)
            return
        row = rows[0]
        if action == "stop":
            if str(interaction.user.id) != row["owner_id"]:
                await interaction.followup.send(
                    "Only the person who started this watch can stop it.", ephemeral=True)
                return
            await asyncio.to_thread(self._delete, message_id)
            try:
                stopped = interaction.message.embeds[0] if interaction.message.embeds else None
                if stopped is not None:
                    stopped.set_footer(text="Watch stopped. Research only. No real trades.")
                await interaction.message.edit(embed=stopped, view=None)
            except discord.HTTPException:
                pass
            await interaction.followup.send("Watch stopped.", ephemeral=True)
            return
        try:
            await asyncio.wait_for(self._edit(row), timeout=40)
            notice = "Watch refreshed."
        except (TimeoutError, discord.HTTPException):
            notice = "Couldn't refresh right now; it updates itself every 5 minutes."
        await interaction.followup.send(notice, ephemeral=True)
