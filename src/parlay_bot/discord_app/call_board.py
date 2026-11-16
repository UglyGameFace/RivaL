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
MARKETS = ("exchange", "polymarket", "kalshi")
# Check promptly for new exchange forecasts. The full model/contract reader
# runs on a slower, market-specific bounded cadence or when issuance changes.
CHECK_SECONDS = 15
FULL_REFRESH_SECONDS = {"exchange": 60, "polymarket": 30, "kalshi": 45}
SCHEMA = """CREATE TABLE IF NOT EXISTS rival_live_call_board (
    channel_id TEXT NOT NULL,
    market_kind TEXT NOT NULL,
    asset TEXT NOT NULL,
    horizon_minutes INTEGER NOT NULL,
    message_id TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    PRIMARY KEY(channel_id,market_kind,asset,horizon_minutes)
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
            "Prospectively issued UP/DOWN forecasts show numeric confidence, "
            "but raw model scores are NOT measured win rates. "
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
            score_type = (
                "Platt-calibrated model estimate; forward reliability unverified"
                if snapshot.calibrated else
                "RAW model estimate; NOT calibrated or a verified win rate"
            )
            text = (
                f"**{direction}**\n"
                f"**Confidence: {snapshot.probability:.1%}** "
                f"({score_type})\n"
                f"Model: `{snapshot.model_version or 'unknown'}` · "
                f"Exchange: {snapshot.venue or 'unavailable'}\n"
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
                    + f" · {direction} · Confidence: {max(p, 1 - p):.1%}"
                    + " (raw, research only)"
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



async def build_polymarket_calls(service, asset: str, horizon: int):
    from parlay_bot.discord_app.polymarket_live_board import matching_call

    embed, _ = await service._observation(asset, horizon)
    current = service.peek(asset, horizon)
    board, call = current if current is not None else (None, None)
    if matching_call(board, call, asset=asset, horizon=horizon):
        probability = call.probability_up
        if (call.direction in ("UP", "DOWN")
                and isinstance(probability, (int, float))
                and math.isfinite(probability) and 0 < probability < 1):
            confidence = (probability if call.direction == "UP" else 1 - probability)
            embed.add_field(
                name="📊 Frozen model confidence",
                value=(
                    f"**{call.direction} · {confidence:.1%}** "
                    "(raw contract-model estimate, NOT verified win rate)\n"
                    f"Model: `{call.model}` · recorded "
                    + discord.utils.format_dt(call.issued_at, style="R")
                ),
                inline=False,
            )
    else:
        embed.add_field(
            name="📊 Frozen model confidence",
            value="No verified matching, prospectively issued contract call.",
            inline=False,
        )
    embed.set_footer(
        text="Polymarket contract price-to-beat ≠ exchange candle/TP · No orders"
    )
    return embed, hashlib.sha256(str(embed.to_dict()).encode()).hexdigest()


async def build_kalshi_calls(browser, reader, asset: str):
    """Kalshi YES/NO applies only to its titled market, not an invented 5m."""
    snapshot = await browser.browse(asset)
    embed = discord.Embed(
        title=f"RivaL Live Calls · Kalshi · {asset}",
        description=(
            "Real Kalshi crypto contracts, independent of Polymarket/perps. "
            "YES/NO reflects the exact listed contract's rules. "
            "Never assume a contract shares the 5/15/60-minute candle horizon."
        ),
        color=discord.Color.blurple(),
    )
    for market in snapshot.markets[:3]:
        try:
            preview = (
                await asyncio.to_thread(
                    reader.contract_preview, asset=asset, market=market,
                ) if reader is not None else None
            )
        except (ValueError, KeyError, TypeError):
            preview = None
        title = market.title[:140]
        name = f"{title} · {market.ticker}"
        text = (
            f"Closes {discord.utils.format_dt(market.close_time, style='R')}\n"
        )
        p = preview.get("candidate_yes") if preview is not None else None
        if isinstance(p, (int, float)) and math.isfinite(p) and 0 < p < 1:
            side = "YES" if p > .5 else "NO" if p < .5 else "TIED"
            if side == "TIED":
                text += "No directional contract call (50/50 model)."
            else:
                text += (
                    f"**{side} · Confidence: {max(p, 1 - p):.1%}** "
                    "(raw frozen model estimate, not verified accuracy)\n"
                    f"Model: `{preview['model']}` · "
                    "**UNQUALIFIED RESEARCH; NO TRADE CALL**"
                )
        else:
            text += "NO ISSUED MODEL CALL · Contract outcome not predicted."
        embed.add_field(name=name[:256], value=text[:1024], inline=False)
    if not snapshot.markets:
        embed.add_field(
            name="Current contracts",
            value="No exact, verified Kalshi crypto contracts currently listed.",
            inline=False,
        )
    embed.set_footer(
        text="No synthetic strikes/TP or cross-venue prices · No real orders"
    )
    return embed, hashlib.sha256(str(embed.to_dict()).encode()).hexdigest()


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

    def __init__(self, bot, store, market, *, polymarket=None, kalshi=None,
                 kalshi_forward=None) -> None:
        self.bot, self.store, self.market = bot, store, market
        self.polymarket = polymarket
        self.kalshi = kalshi
        self.kalshi_forward = kalshi_forward
        self.view = CallBoardView(self)
        self._lock = asyncio.Lock()
        self._ready = False
        self._last_evaluated: dict[tuple, datetime] = {}
        self._last_revision: dict[tuple, str | None] = {}
        self._refresh_event = asyncio.Event()

    def initialize(self) -> None:
        if not self._ready:
            with self.store.connect() as conn:
                conn.execute(SCHEMA)
            self._ready = True

    def _rows(self) -> list[dict]:
        self.initialize()
        with self.store.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM rival_live_call_board "
                "ORDER BY channel_id,market_kind,asset,horizon_minutes"
            ).fetchall()]

    def _save(self, channel, kind, asset, horizon, message, owner, fingerprint) -> None:
        with self.store.connect() as conn:
            conn.execute(
                """INSERT INTO rival_live_call_board
                (channel_id,market_kind,asset,horizon_minutes,
                 message_id,owner_id,fingerprint) VALUES (?,?,?,?,?,?,?)""",
                (str(channel), kind, asset, horizon, str(message),
                 str(owner), fingerprint),
            )

    def _touch(self, row: dict, fingerprint: str) -> None:
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE rival_live_call_board SET fingerprint=?
                WHERE channel_id=? AND asset=? AND message_id=?""",
                (fingerprint, row["channel_id"], row["asset"], row["message_id"]),
            )

    def _delete(self, row: dict) -> None:
        with self.store.connect() as conn:
            conn.execute(
                """DELETE FROM rival_live_call_board
                WHERE channel_id=? AND asset=? AND message_id=?""",
                (row["channel_id"], row["asset"], row["message_id"]),
            )

    async def _render(self, kind: str, asset: str, horizon: int):
        if kind == "exchange":
            return await asyncio.to_thread(live_calls_embed, self.market, asset)
        if kind == "polymarket":
            if self.polymarket is None:
                raise ValueError("Polymarket research not configured")
            return await build_polymarket_calls(self.polymarket, asset, horizon)
        if kind == "kalshi":
            if self.kalshi is None:
                raise ValueError("Kalshi research not configured")
            return await build_kalshi_calls(
                self.kalshi, self.kalshi_forward, asset,
            )
        raise ValueError("Unsupported call board market")

    def _revision(self, asset: str) -> str | None:
        """Cheap indexed check; a new immutable forecast triggers a redraw."""
        if self.market is None or getattr(self.market, "store", None) is None:
            return None
        with self.market.store.connect() as conn:
            row = conn.execute(
                "SELECT MAX(issued_at) AS latest FROM perp_forward_audit WHERE asset=?",
                (asset,),
            ).fetchone()
        return str(row["latest"]) if row and row["latest"] else None

    def request_refresh(self) -> None:
        """Called by the real forecast worker after successful new issuance."""
        self._refresh_event.set()

    async def wait_for_refresh(self) -> bool:
        try:
            await asyncio.wait_for(self._refresh_event.wait(), timeout=CHECK_SECONDS)
        except TimeoutError:
            return False
        self._refresh_event.clear()
        return True

    async def start(self, interaction: discord.Interaction, asset: str,
                    *, kind: str = "exchange", horizon: int = 5) -> str:
        if asset not in ASSETS or horizon not in HORIZONS:
            raise ValueError("Unsupported calls asset or horizon")
        if kind not in MARKETS:
            return (
                "No source-matched live calls for this market. "
                "Its independent market research panel remains available."
            )
        channel = interaction.channel
        if channel is None or getattr(interaction, "guild_id", None) is None:
            return "Use a server channel to start Live Calls. No DM was sent."
        async with self._lock:
            rows = await asyncio.to_thread(self._rows)
            if any(
                r["channel_id"] == str(channel.id) and r["asset"] == asset
                and r["market_kind"] == kind
                and r["horizon_minutes"] == (horizon if kind == "polymarket" else 0)
                for r in rows
            ):
                return f"{asset} {kind} already has a Live Calls card here."
            if len(rows) >= MAX_BOARDS:
                return "Live Calls capacity reached; stop an old panel first."
            if sum(r["owner_id"] == str(interaction.user.id)
                   for r in rows) >= MAX_OWNER_BOARDS:
                return "You already have four Live Calls panels. Stop one first."
            try:
                embed, key = await asyncio.wait_for(
                    self._render(kind, asset, horizon), timeout=20,
                )
            except Exception:  # noqa: BLE001 - one unsupported market fails closed
                _LOG.warning("Requested market Live Calls unavailable", exc_info=True)
                return "Cannot verify fresh calls for this market; no panel posted."
            try:
                msg = await asyncio.wait_for(channel.send(embed=embed, view=self.view),
                                             timeout=8)
            except (discord.HTTPException, AttributeError, TimeoutError):
                return "Cannot post here; check Send Messages permission."
            try:
                await asyncio.to_thread(
                    self._save, channel.id, kind, asset,
                    horizon if kind == "polymarket" else 0,
                    msg.id, interaction.user.id, key,
                )
            except Exception:  # noqa: BLE001 - a failed post must be rolled back
                _LOG.exception("RivaL Live Calls persistence failed")
                try:
                    await msg.delete()
                except (discord.HTTPException, AttributeError):
                    pass
                return "Could not save this panel; no active tracking created."
            self._last_evaluated[
                (kind, asset, horizon if kind == "polymarket" else 0)
            ] = datetime.now(UTC)
            return (
                f"Posted {asset} {kind} Live Calls in this channel. "
                "One message updates automatically, without DMs."
            )

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
        await asyncio.to_thread(self._touch, row, key)
        return True

    async def cycle(self, *, force: bool = False) -> int:
        """Refresh on issuance and market boundaries, without DB query storms."""
        async with self._lock:
            rows = await asyncio.to_thread(self._rows)
            if not rows:
                return 0
            now = datetime.now(UTC)
            cards = {}
            updated = 0
            for row in rows:
                kind, asset = row["market_kind"], row["asset"]
                horizon = int(row["horizon_minutes"])
                key = (kind, asset, horizon)
                try:
                    if key not in cards:
                        recent = self._last_evaluated.get(key)
                        stale = (
                            recent is None or
                            (now - recent).total_seconds() >= FULL_REFRESH_SECONDS[kind]
                        )
                        revision = self._last_revision.get(key)
                        new_issuance = False
                        if kind == "exchange" and not force and not stale:
                            checked = await asyncio.to_thread(self._revision, asset)
                            new_issuance = checked != revision
                            self._last_revision[key] = checked
                        # An exchange issuance wake must not cause a Gamma/CLOB
                        # or Kalshi REST burst unrelated to that new forecast.
                        if (force and kind == "exchange") or stale or new_issuance:
                            cards[key] = await asyncio.wait_for(
                                self._render(kind, asset, horizon), timeout=20,
                            )
                            self._last_evaluated[key] = datetime.now(UTC)
                        else:
                            cards[key] = None
                    if cards[key] is not None:
                        updated += await self._edit(row, *cards[key])
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one venue cannot stop others
                    _LOG.warning("Live Calls update failed kind=%s asset=%s",
                                 kind, asset, exc_info=True)
            return updated

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
                    card, key = await self._render(
                        row["market_kind"], row["asset"],
                        int(row["horizon_minutes"]),
                    )
                    await self._edit(row, card, key, force=True)
                    result = "Live Calls refreshed."
                except Exception:  # noqa: BLE001
                    _LOG.warning("Manual calls refresh failed", exc_info=True)
                    result = "Could not verify fresh calls; retry."
            else:
                result = "Unknown calls action."
        await interaction.followup.send(result, ephemeral=True)
