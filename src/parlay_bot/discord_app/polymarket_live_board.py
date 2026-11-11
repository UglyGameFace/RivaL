"""Opt-in, restart-safe, automatically edited Polymarket tracking DM.

One private message per user changes in place as genuine current/next Gamma
markets, CLOB books and separately sourced RivaL forecasts change.
No order execution, predicted Polymarket strike or fabricated win probability.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import discord

from parlay_bot.discord_app.polymarket_watch import (
    ASSETS,
    HORIZONS,
    READ_TIMEOUT,
    MarketUnavailable,
    PolymarketReader,
    PolymarketWindowBoard,
    unavailable_text,
)
from parlay_bot.perpetuals.model_league import Contest, league, league_text, settlement_schema
from parlay_bot.perpetuals.polymarket_research import ContractCall, PolymarketResearch
from parlay_bot.storage.hot import RelationalHotStore

if TYPE_CHECKING:
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore

_LOG = logging.getLogger(__name__)
MAX_TRACKERS = 100


@dataclass(frozen=True)
class LivePolymarketTracker:
    user_id: str
    asset: str
    horizon: int
    channel_id: str
    message_id: str
    fingerprint: str


class LivePolymarketBoardStore:
    def __init__(self, store: RelationalHotStore) -> None:
        self.store = store

    def initialize(self) -> None:
        with self.store.connect() as conn:
            conn.executescript(
                """CREATE TABLE IF NOT EXISTS rival_polymarket_live_board (
                    user_id TEXT PRIMARY KEY,
                    asset TEXT NOT NULL,
                    horizon INTEGER NOT NULL,
                    channel_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rival_polymarket_ping_optout (
                    user_id TEXT PRIMARY KEY,
                    updated_at TEXT NOT NULL
                );"""
            )

    @staticmethod
    def _record(row) -> LivePolymarketTracker:
        return LivePolymarketTracker(
            user_id=str(row["user_id"]), asset=str(row["asset"]),
            horizon=int(row["horizon"]), channel_id=str(row["channel_id"]),
            message_id=str(row["message_id"]),
            fingerprint=str(row["fingerprint"]),
        )

    def get(self, user_id: str) -> LivePolymarketTracker | None:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM rival_polymarket_live_board "
                "WHERE user_id=? AND enabled=1", (user_id,),
            ).fetchone()
        return self._record(row) if row else None

    def active(self) -> list[LivePolymarketTracker]:
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM rival_polymarket_live_board "
                "WHERE enabled=1 ORDER BY user_id LIMIT ?", (MAX_TRACKERS,),
            ).fetchall()
        return [self._record(row) for row in rows]

    def save(self, tracker: LivePolymarketTracker) -> None:
        if (
            tracker.asset not in ASSETS or tracker.horizon not in HORIZONS
            or not all(x.isdecimal() and int(x) > 0 for x in (
                tracker.user_id, tracker.channel_id, tracker.message_id,
            ))
        ):
            raise ValueError("Invalid live Polymarket tracker")
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO rival_polymarket_live_board "
                "(user_id,asset,horizon,channel_id,message_id,fingerprint,"
                "enabled,updated_at) VALUES (?,?,?,?,?, ?,1,?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "asset=excluded.asset,horizon=excluded.horizon,"
                "channel_id=excluded.channel_id,message_id=excluded.message_id,"
                "fingerprint=excluded.fingerprint,enabled=1,"
                "updated_at=excluded.updated_at",
                (tracker.user_id, tracker.asset, tracker.horizon,
                 tracker.channel_id, tracker.message_id, tracker.fingerprint,
                 datetime.now(UTC).isoformat()),
            )

    def changed(self, tracker: LivePolymarketTracker, fingerprint: str) -> None:
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE rival_polymarket_live_board "
                "SET fingerprint=?,updated_at=? "
                "WHERE user_id=? AND channel_id=? AND message_id=? "
                "AND enabled=1",
                (fingerprint, datetime.now(UTC).isoformat(), tracker.user_id,
                 tracker.channel_id, tracker.message_id),
            )

    def pings_on(self, user_id: str) -> bool:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM rival_polymarket_ping_optout WHERE user_id=?", (user_id,),
            ).fetchone()
        return row is None

    def toggle_pings(self, user_id: str) -> bool:
        """Flip the user's call pings; returns whether pings are now on."""
        with self.store.connect() as conn:
            gone = conn.execute(
                "DELETE FROM rival_polymarket_ping_optout WHERE user_id=?", (user_id,),
            )
            if gone.rowcount:
                return True
            conn.execute(
                "INSERT INTO rival_polymarket_ping_optout (user_id,updated_at) VALUES (?,?) "
                "ON CONFLICT(user_id) DO NOTHING",
                (user_id, datetime.now(UTC).isoformat()),
            )
        return False

    def retarget(self, tracker: LivePolymarketTracker, asset: str, horizon: int) -> bool:
        """Change the market of this exact card in place."""
        if asset not in ASSETS or horizon not in HORIZONS:
            raise ValueError("Invalid live Polymarket tracker")
        with self.store.connect() as conn:
            done = conn.execute(
                "UPDATE rival_polymarket_live_board SET asset=?,horizon=?,fingerprint='',"
                "updated_at=? WHERE user_id=? AND channel_id=? AND message_id=? "
                "AND enabled=1",
                (asset, horizon, datetime.now(UTC).isoformat(), tracker.user_id,
                 tracker.channel_id, tracker.message_id),
            )
        return done.rowcount == 1

    def stop(self, user_id: str) -> bool:
        with self.store.connect() as conn:
            done = conn.execute(
                "UPDATE rival_polymarket_live_board SET enabled=0 "
                "WHERE user_id=? AND enabled=1", (user_id,),
            )
        return done.rowcount == 1

    def can_start(self, user_id: str) -> bool:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM rival_polymarket_live_board "
                "WHERE user_id=? AND enabled=1", (user_id,),
            ).fetchone()
            if row:
                return True
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM rival_polymarket_live_board "
                "WHERE enabled=1",
            ).fetchone()
            return int(count["n"]) < MAX_TRACKERS


def value_line(call: ContractCall, quote) -> str:
    """Is the called side's share price below the model's own chance for it?

    Uses Polymarket's crypto taker fee. The model's chance is its own
    estimate, not a proven probability, and the line says so.
    """
    if quote is None or call.probability_up is None or call.direction not in ("UP", "DOWN"):
        return ""
    chance = call.probability_up if call.direction == "UP" else 1 - call.probability_up
    cost = getattr(quote, "up_cost" if call.direction == "UP" else "down_cost", None)
    if cost is None:
        return "Price check unavailable: exact fees or ten-share depth are missing.\n"
    gap = chance - cost
    verdict = ("✅ cheaper than the model's own chance" if gap > 0
               else "⛔ costs more than the model's own chance — skip at this price")
    return (f"Price check: {call.direction} costs {cost * 100:.0f}¢ with fee; model gives "
            f"{call.direction} {chance:.0%} → {verdict} (model estimate, unproven).\n")


def matching_call(board, call, *, asset: str, horizon: int) -> bool:
    return (
        isinstance(call, ContractCall) and board is not None and board.current is not None
        and call.slug == board.current.slug and call.asset == asset and call.horizon == horizon
        and call.expires == board.current.closes
        and call.issued_at <= datetime.now(UTC) < call.expires
    )


def live_polymarket_embed(
    board: PolymarketWindowBoard | None, snapshot: ContractCall | None, *,
    asset: str, horizon: int, ai_status: str | None = None,
    formula: dict | None = None, league: dict | None = None,
) -> discord.Embed:
    current = board.current if board is not None else None
    valid = matching_call(board, snapshot, asset=asset, horizon=horizon)
    direction = snapshot.direction if valid else "WAIT"
    champion = league.get("champion") if league else None
    outranked = not valid or champion != snapshot.model or snapshot.qualified_at_issue is False
    status = (f"Research {direction} • unqualified" if outranked else f"Qualified {direction}") if valid else "Forecast pending"
    title = f"📡 Polymarket • {asset} • {horizon} min | {status}"
    embed = discord.Embed(
        title=title,
        description=(
            f"**Experimental contract-specific {snapshot.kind} research.** "
            "The first model forecast is frozen; live prices update independently. "
            "Accuracy and profitable edge are not established."
            if valid else "**No current contract-specific AI call.** "
            + (ai_status or "Waiting for the frozen forecast checkpoint and verified market inputs.")
        ), color=discord.Color.blurple() if valid else discord.Color.orange(),
    )
    quote = board.current_quote if board is not None else None
    embed.add_field(
        name="💰 Polymarket share asks",
        value=(f"UP ${quote.up_ask:.2f} • DOWN ${quote.down_ask:.2f}\n"
               f"Source update <t:{int(quote.quote_at.timestamp())}:R>"
               if quote is not None else unavailable_text(
                   board.quote_unavailable_reason if board else None)),
        inline=False,
    )
    if current is not None:
        if not valid:
            target = current.opens+timedelta(minutes=horizon*.2)
            embed.add_field(name="Forecast status", value=(
                f"This contract's forecast checkpoint is <t:{int(target.timestamp())}:R>. "
                "It needs a fresh order book and model inputs at that time; "
                "a missed checkpoint is shown as unavailable, never a guessed call."
                if datetime.now(UTC) <= target+timedelta(seconds=20) else
                "No frozen checkpoint forecast is available for this contract. "
                "The next contract gets a separate attempt."
            ), inline=False)
        embed.add_field(
            name="🎯 Exact contract",
            value=f"[Open verified event]({current.url}) • closes <t:{int(current.closes.timestamp())}:R>",
            inline=False,
        )
        embed.add_field(
            name="Price to beat",
            value=f"${current.price_to_beat:,.4f}" if current.price_to_beat is not None
            else "Not published or verified; never estimated from another exchange",
            inline=False,
        )
        source = quote.resolution_source if quote is not None else None
        if source and "chain.link" in source.lower():
            resolution_text = (
                "Chainlink reference from this contract's published market. "
                "Use the exact event rules for the settlement observation."
            )
        elif source and "binance.com" in source.lower():
            resolution_text = (
                "Binance reference from this contract's published market. "
                "Do not substitute a perpetual futures candle."
            )
        else:
            resolution_text = (
                "Exact settlement source not verified in this quote. "
                "Open the listed contract to check its rules."
            )
        embed.add_field(name="Settlement source", value=resolution_text, inline=False)
    embed.add_field(name="🏆 Alert qualification",
                    value=league_text(league, asset=asset, horizon=horizon), inline=False)
    if valid:
        call_label = ("🧠 First model call (frozen) • unqualified model research" if outranked
                      else "🧠 First model call (frozen)"
                      if snapshot.kind == "model" else f"🧠 {snapshot.kind} contract research")
        embed.add_field(name=call_label, value=f"**{direction}** • {snapshot.thesis}", inline=False)
        embed.add_field(name="Call timing", value=(
            f"First issued <t:{int(snapshot.issued_at.timestamp())}:R> for this contract. "
            "New quotes never change the first model call or its score. "
            "WAIT is a research-only uncertainty state, not a trading instruction."
        ), inline=False)
        embed.add_field(name="Research limitation", value=snapshot.risk, inline=False)
        if ai_status:
            embed.add_field(name="AI refresh", value=ai_status, inline=False)
    if formula is not None and current is not None and formula.get("slug") == current.slug:
        chance = formula["chance_up"]
        embed.add_field(name="📐 Price formula checkpoint (frozen)", value=(
            f"**{chance:.0%} UP / {1 - chance:.0%} DOWN** from {formula.get('reference_label', 'source-matched spot')} vs. the "
            f"reference, time left and EWMA volatility "
            f"(<t:{int(formula['at'].timestamp())}:R>). Snapshot asks: UP "
            f"${formula['up_ask']:.2f} • DOWN ${formula['down_ask']:.2f}. "
            f"{formula.get('payoff', 'Research approximation')}; strike: {formula.get('strike_source', 'unverified')}. "
            "Testing only; no proven edge."
        ), inline=False)
    if board is not None and board.next_window is not None:
        next_window = board.next_window
        embed.add_field(name="⏭️ Next contract • forecast pending", value=(
            f"[Next event]({next_window.url}) • opens <t:{int(next_window.opens.timestamp())}:R>\n"
            "Next model call: **WAIT / not issued**. The current contract's "
            "forecast is not reused for the next contract. A separate frozen "
            "forecast appears here when that contract opens and its verified "
            "order book is available."
        ), inline=False)
    embed.set_footer(text="Live market quotes • First model call frozen • No orders")
    return embed


def _receipt_ms(interaction) -> int | None:
    """Milliseconds between the user's tap (Discord's clock) and now."""
    created = getattr(interaction, "created_at", None)
    if not isinstance(created, datetime):
        return None
    return int((datetime.now(UTC) - created).total_seconds() * 1000)


class _DMMarketSelect(discord.ui.Select["LivePolymarketDMView"]):
    """Switch this card to another coin/timeframe without opening /rival."""

    def __init__(self) -> None:
        super().__init__(
            placeholder="Switch coin & timeframe",
            custom_id="rival:poly:dm:market", row=1,
            options=[
                discord.SelectOption(
                    label=f"{asset} • {'1 hour' if horizon == 60 else f'{horizon} min'}",
                    value=f"{asset}:{horizon}",
                )
                for asset in ASSETS for horizon in HORIZONS
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        await self.view.service.handle_dm_action(interaction, "market:" + self.values[0])


class LivePolymarketDMView(discord.ui.View):
    """Restart-persistent DM controls; authorize against the active tracker."""

    def __init__(self, service: LivePolymarketBoardService) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.add_item(_DMMarketSelect())

    @discord.ui.button(
        label="Refresh now", emoji="🔄",
        style=discord.ButtonStyle.secondary, custom_id="rival:poly:dm:refresh",
    )
    async def refresh_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ) -> None:
        await self.service.handle_dm_action(interaction, "refresh")

    @discord.ui.button(
        label="Stop tracking", emoji="🔕",
        style=discord.ButtonStyle.danger, custom_id="rival:poly:dm:stop",
    )
    async def stop_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ) -> None:
        await self.service.handle_dm_action(interaction, "stop")

    @discord.ui.button(
        label="Pings on/off", emoji="🔔",
        style=discord.ButtonStyle.secondary, custom_id="rival:poly:dm:pings",
    )
    async def pings_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ) -> None:
        await self.service.handle_dm_action(interaction, "pings")

    @discord.ui.button(
        label="Track record", emoji="📊",
        style=discord.ButtonStyle.secondary, custom_id="rival:poly:dm:record",
    )
    async def record_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ) -> None:
        await self.service.handle_dm_action(interaction, "record")


class LivePolymarketBoardService:
    def __init__(
        self, *, bot: discord.Client, store: LivePolymarketBoardStore,
        reader: PolymarketReader, market: PerpetualMarketStore | None,
        research: PolymarketResearch | None = None,
    ) -> None:
        self.bot = bot
        self.store = store
        self.reader = reader
        self.market = market
        self.research = research
        self._closed = False
        self.dm_view = LivePolymarketDMView(self)
        self._lock = asyncio.Lock()
        self._cycle_lock = asyncio.Lock()
        self._user_locks: dict[str, asyncio.Lock] = {}
        # In-memory verified source/evidence snapshots. The guided menu
        # renders these without another network or PostgreSQL request.
        self._live_cache: dict[
            tuple[str, int], tuple[datetime, PolymarketWindowBoard, ContractCall | None]
        ] = {}
        self._prime_pending: dict[tuple[str, int], asyncio.Task] = {}
        # user_id -> slug already pinged, so each contract pings once.
        self._pinged: dict[str, str] = {}
        # Optional: the price-formula test (set by the bot) for the card line.
        self.formula = None
        # (computed_at, model_league result): which model, if any, beats the price.
        self._league: tuple[datetime, dict] | None = None

    def peek(self, asset: str, horizon: int) -> tuple[PolymarketWindowBoard, ContractCall | None] | None:
        saved = self._live_cache.get((asset, horizon))
        now = datetime.now(UTC)
        if saved is None or now - saved[0] > timedelta(seconds=20):
            return None
        board = saved[1]
        if board.current is not None and board.current.closes <= now:
            return None
        if board.current_quote is not None and (
            now - board.current_quote.quote_at > timedelta(seconds=25)
            or board.current_quote.expires <= now
        ):
            return board.__class__(
                checked_at=board.checked_at, current=board.current,
                next_window=board.next_window, current_quote=None,
                quote_unavailable_reason="book_stale",
            ), saved[2]
        return board, saved[2]

    async def preview(self, asset: str, horizon: int) -> tuple[PolymarketWindowBoard, ContractCall | None] | None:
        """One short warmup read; never block menu on the model DB pool."""
        if self._closed:
            return None
        cached = self.peek(asset, horizon)
        if cached is not None:
            return cached
        key = (asset, horizon)
        task = self._prime_pending.get(key)
        if task is None or task.done():
            task = asyncio.create_task(self._observation(asset, horizon))
            self._prime_pending[key] = task
            def finished(completed, k=key):
                if self._prime_pending.get(k) is completed:
                    self._prime_pending.pop(k, None)
                if not completed.cancelled():
                    completed.exception()
            task.add_done_callback(finished)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=1.2)
        except (TimeoutError, Exception):  # noqa: BLE001 - warmup best effort
            return None
        return self.peek(asset, horizon)

    async def request_contract_ai(self, asset: str, horizon: int) -> bool:
        """Spend a shared free-AI request ONLY for an explicit contract call.

        Preview may warm the real order book, but a tracker, board cycle or
        background refresh never invokes this method.
        """
        if self._closed or self.research is None:
            return False
        reviewer = getattr(self.research, "ai", None)
        if reviewer is None or not getattr(reviewer, "available", False):
            return False
        cached = self.peek(asset, horizon)
        if cached is None:
            await self.preview(asset, horizon)
            cached = self.peek(asset, horizon)
        if cached is None or cached[0].current_quote is None:
            return False
        notice = cached[0].current_quote
        now = datetime.now(UTC)
        if notice.expires <= now or now-notice.quote_at > timedelta(seconds=25):
            return False
        self.research.observe(notice, request_ai=True)
        return True

    async def wait_ready(self, asset: str, horizon: int):
        if self._closed:
            return None

        async def finish_market():
            pending = self._prime_pending.get((asset, horizon))
            if pending is not None:
                await asyncio.wait_for(asyncio.shield(pending), timeout=READ_TIMEOUT + 2)
            if self.peek(asset, horizon) is None:
                await self.preview(asset, horizon)
                pending = self._prime_pending.get((asset, horizon))
                if pending is not None:
                    await asyncio.wait_for(asyncio.shield(pending), timeout=READ_TIMEOUT + 2)

        await finish_market()
        cached = self.peek(asset, horizon)
        if cached is not None and self.research is not None and cached[0].current is not None:
            await self.research.wait(cached[0].current.slug)
        # A slow model reply does not make the input order book fresh again.
        await finish_market()
        return self.peek(asset, horizon)

    async def aclose(self):
        self._closed = True
        pending = list(self._prime_pending.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._prime_pending.clear()

    async def _observation(
        self, asset: str, horizon: int,
    ) -> tuple[discord.Embed, str]:
        async def load_board():
            try:
                return await asyncio.wait_for(
                    self.reader.window_board(asset, horizon), timeout=READ_TIMEOUT + 1,
                )
            except Exception as exc:  # noqa: BLE001
                # Name the failure: a timeout, a refused book (MarketUnavailable
                # carries its reason, e.g. no_liquidity) and an HTTP error are
                # different problems. Type and reason code only, no payloads.
                # Only MarketUnavailable's fixed codes are logged as a reason;
                # other exception text may echo a remote payload.
                reason = str(exc)[:40] if isinstance(exc, MarketUnavailable) else "-"
                _LOG.warning(
                    "Live Polymarket verified window unavailable asset=%s "
                    "horizon=%dm error=%s reason=%s", asset, horizon,
                    type(exc).__name__, reason,
                )
                return None

        board = await load_board()
        snapshot = None
        if board is not None:
            if self.research is not None and board.current_quote is not None:
                snapshot = self.research.observe(board.current_quote)
            elif self.research is not None and board.current is not None:
                # The frozen call does not depend on the live quote. Near the
                # close one side of the book often empties (no_liquidity) and
                # the quote drops out for seconds at a time; the card must keep
                # showing the stored call instead of falling back to WAIT.
                snapshot = self.research.peek(board.current.slug)
            snapshot = await self._qualified_call(board, snapshot, asset, horizon)
            self._live_cache[(asset, horizon)] = (datetime.now(UTC), board, snapshot)
        reading = None
        if self.formula is not None and board is not None and board.current is not None:
            latest = self.formula.latest.get(board.current.slug)
            if latest is not None and latest["slug"] == board.current.slug:
                reading = latest
        embed = live_polymarket_embed(
            board, snapshot, asset=asset, horizon=horizon, formula=reading,
            league=self._league_entry(asset, horizon, board.current_quote if board else None),
            ai_status=(self.research.refresh_note(board.current.slug) if snapshot is not None
                       else self.research.status(board.current.slug)
                       if self.research is not None and board is not None and board.current is not None else None),
        )
        # No live clock in the fingerprint: unchanged markets never spam edits.
        digest = hashlib.sha256(
            ("dm-controls-v1|" + str(embed.to_dict())).encode("utf-8"),
        ).hexdigest()
        return embed, digest

    async def handle_dm_action(
        self, interaction: discord.Interaction, action: str,
    ) -> None:
        """Acknowledge quickly and authorize by user, channel, and current card."""
        started = time.monotonic()
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.HTTPException as exc:
            # Discord already gave up (10062): record how late the tap arrived.
            _LOG.warning("RivaL DM control not acknowledged action=%s receipt_ms=%s reason=%s",
                         action, _receipt_ms(interaction), type(exc).__name__)
            return
        receipt, ack = _receipt_ms(interaction), int((time.monotonic() - started) * 1000)
        (_LOG.warning if (receipt or 0) + ack > 2000 else _LOG.info)(
            "RivaL DM control action=%s receipt_ms=%s ack_ms=%d", action, receipt, ack)
        message = interaction.message
        message_id = str(message.id) if message is not None else ""
        channel_id = str(interaction.channel_id)
        user_id = str(interaction.user.id)
        try:
            if action == "stop":
                stopped = await asyncio.wait_for(
                    self.stop(int(user_id), expected_message_id=message_id,
                              expected_channel_id=channel_id), timeout=8,
                )
                notice = ("Tracking stopped. Your DM card is now inactive."
                          if stopped else "This is an older panel. Open /rival → Live Panel.")
            elif action == "refresh":
                updated = await asyncio.wait_for(
                    self.refresh_dm(user_id, channel_id, message_id), timeout=13,
                )
                notice = ("Live panel refreshed." if updated
                          else "This is an older or inactive panel. Use /rival → Live Panel.")
            elif action in ("pings", "record") or action.startswith("market:"):
                tracker = await asyncio.to_thread(self.store.get, user_id)
                if (tracker is None or tracker.channel_id != channel_id
                        or tracker.message_id != message_id):
                    notice = "This is an older or inactive panel. Use /rival → Live Panel."
                elif action == "pings":
                    on = await asyncio.to_thread(self.store.toggle_pings, user_id)
                    notice = ("🔔 Pings ON: one message for a qualified model's frozen "
                              "call with a current after-fee edge." if on else
                              "🔕 Pings OFF: the card still updates, but no new messages.")
                elif action == "record":
                    notice = (f"**{tracker.asset} {tracker.horizon}m on Polymarket**\n"
                              + league_text(self._league_entry(tracker.asset, tracker.horizon),
                                                   asset=tracker.asset, horizon=tracker.horizon)
                              + "\nFor your own next-calls scorecard: /rival → Bot Performance "
                              "→ Track Next Predictions.")
                else:
                    asset, _, horizon_text = action.removeprefix("market:").partition(":")
                    horizon = int(horizon_text) if horizon_text.isdecimal() else 0
                    if asset not in ASSETS or horizon not in HORIZONS:
                        notice = "Unsupported market."
                    else:
                        async with self._user_locks.setdefault(user_id, asyncio.Lock()):
                            moved = await asyncio.to_thread(
                                self.store.retarget, tracker, asset, horizon)
                        updated = moved and await asyncio.wait_for(
                            self.refresh_dm(user_id, channel_id, message_id), timeout=13,
                        )
                        notice = (f"This card now tracks {asset} "
                                  f"{'1h' if horizon == 60 else f'{horizon}m'}."
                                  if updated else "Could not switch this card. Try again.")
            else:
                notice = "Unsupported panel action."
        except (TimeoutError, discord.HTTPException):
            _LOG.warning("RivaL live DM control failed action=%s user=%s",
                         action, user_id)
            notice = "Live panel action unavailable. The next automatic update will retry."
        await interaction.followup.send(notice, ephemeral=True)

    async def refresh_dm(
        self, user_id: str, channel_id: str, message_id: str,
    ) -> bool:
        async with self._user_locks.setdefault(user_id, asyncio.Lock()):
            tracker = await asyncio.to_thread(self.store.get, user_id)
            if (tracker is None or tracker.channel_id != channel_id
                    or tracker.message_id != message_id):
                return False
            embed, fingerprint = await self._observation(tracker.asset, tracker.horizon)
            channel = self.bot.get_partial_messageable(int(channel_id))
            await asyncio.wait_for(
                channel.get_partial_message(int(message_id)).edit(
                    embed=embed, view=self.dm_view,
                ), timeout=5,
            )
            await asyncio.to_thread(self.store.changed, tracker, fingerprint)
            return True

    async def start(self, user: discord.abc.User, *,
                    asset: str, horizon: int) -> tuple[bool, str]:
        if asset not in ASSETS or horizon not in HORIZONS:
            return False, "Select BTC, ETH, SOL or XRP and 5m/15m/1h."
        async with self._lock, self._user_locks.setdefault(str(user.id), asyncio.Lock()):
            previous = await asyncio.to_thread(self.store.get, str(user.id))
            if not await asyncio.to_thread(self.store.can_start, str(user.id)):
                return False, "The automatic live tracker is at capacity."
            embed, fingerprint = await self._observation(asset, horizon)
            if horizon == 60:
                observed = self.peek(asset, horizon)
                if observed is None or (
                    observed[0].current is None and observed[0].next_window is None
                ):
                    return False, "No verified hourly Polymarket contract is listed right now."
            try:
                channel = user.dm_channel or await asyncio.wait_for(
                    user.create_dm(), timeout=5,
                )
                # One automatically edited panel per user. Reopening moves
                # the panel to the bottom of the DM without retaining stale
                # panels or sending mid-contract flip notifications.
                sent = await asyncio.wait_for(
                    channel.send(embed=embed, view=self.dm_view), timeout=5,
                )
                message_id = str(sent.id)
                if previous is not None and previous.message_id != message_id:
                    try:
                        old = self.bot.get_partial_messageable(int(previous.channel_id))
                        await asyncio.wait_for(
                            old.get_partial_message(int(previous.message_id)).edit(
                                embed=discord.Embed(
                                    description="This card moved to a newer message below.",
                                    color=discord.Color.dark_grey(),
                                ), view=None), timeout=5,
                        )
                    except (TimeoutError, discord.HTTPException):
                        _LOG.info("Old Polymarket tracker card could not be retired")
            except (TimeoutError, discord.HTTPException) as exc:
                _LOG.warning(
                    "Live Polymarket tracker DM unavailable user=%s reason=%s",
                    user.id, type(exc).__name__,
                )
                return False, "DM unavailable. No active tracker confirmed."
            await asyncio.to_thread(
                self.store.save, LivePolymarketTracker(
                    user_id=str(user.id), asset=asset, horizon=horizon,
                    channel_id=str(channel.id), message_id=message_id,
                    fingerprint=fingerprint,
                ),
            )
            return True, (
                f"Auto-tracking {asset} {horizon}m: live panel moved to the bottom of your DMs. "
                "The same panel keeps updating live market prices and the first "
                "frozen model call. You get one ping when each contract's UP/DOWN call is issued."
            )

    async def stop(
        self, user_id: int, *, expected_message_id: str | None = None,
        expected_channel_id: str | None = None,
    ) -> bool:
        async with self._user_locks.setdefault(str(user_id), asyncio.Lock()):
            previous = await asyncio.to_thread(self.store.get, str(user_id))
            if (expected_message_id is not None and
                    (previous is None or previous.message_id != expected_message_id
                     or previous.channel_id != expected_channel_id)):
                return False
            ended = await asyncio.to_thread(self.store.stop, str(user_id))
            if previous is not None and ended:
                try:
                    channel = self.bot.get_partial_messageable(
                        int(previous.channel_id),
                    )
                    await asyncio.wait_for(
                        channel.get_partial_message(
                            int(previous.message_id)
                        ).edit(embed=discord.Embed(
                            title="Live Polymarket tracking stopped",
                            description="This card is no longer updating. "
                                        "Use /rival → Live Mode to restart.",
                            color=discord.Color.orange(),
                        ), view=None), timeout=5,
                    )
                except (TimeoutError, discord.HTTPException):
                    _LOG.info("Stopped tracker; original DM could not be edited")
            return ended

    def contest_models(self, asset: str, horizon: int) -> list[dict] | None:
        """Every contest model's latest score here, from memory; None if stale.

        Reads the league the board already refreshes every 10 minutes, so
        the Performance page never recomputes it.
        """
        if self._league is None or datetime.now(UTC)-self._league[0] > timedelta(minutes=20):
            return None
        entry = self._league[1].get("markets", {}).get((asset, horizon))
        return list(entry.get("models", [])) if entry else []

    def _league_entry(self, asset: str, horizon: int, quote=None) -> dict:
        empty = {"models": [], "champion": None}
        if self._league is None or datetime.now(UTC)-self._league[0] > timedelta(minutes=20):
            return empty
        result = self._league[1]
        valid_until = result.get("valid_until")
        if valid_until is not None and datetime.now(UTC) >= valid_until:
            return empty
        entry = result["markets"].get((asset, horizon), empty)
        if quote is None:
            saved = self._live_cache.get((asset, horizon))
            quote = saved[1].current_quote if saved else None
        schema = settlement_schema(quote) if quote is not None else None
        if schema is None:
            saved = self._live_cache.get((asset, horizon))
            schema = getattr(saved[2], "settlement_schema", None) if saved else None
        return entry.get("scopes", {}).get(schema, empty)

    async def _qualified_call(self, board, snapshot, asset, horizon):
        quote = board.current_quote
        entry = self._league_entry(asset, horizon, quote)
        model = entry.get("champion")
        store = getattr(getattr(self.research, "learner", None), "store", None)
        if store is None or board.current is None:
            return snapshot if model is None else None
        try:
            contest = self.formula.contest if self.formula is not None else Contest(store)
            row = await asyncio.wait_for(asyncio.to_thread(
                contest.display_call, board.current.slug, model), timeout=3)
        except Exception:  # noqa: BLE001 - unavailable champion cannot create a call
            return None
        if row is None or (quote is not None and (
                row["schema_key"] != settlement_schema(quote) or row["condition_id"] != quote.condition_id)):
            return None
        research = not row["qualified_at_issue"]
        p = float(row["probability_up"])
        direction = ("UP" if p > .5 else "DOWN" if p < .5 else "WAIT") if research else row["direction"]
        return ContractCall(
            slug=row["slug"], asset=asset, horizon=horizon,
            issued_at=datetime.fromisoformat(row["issued_at"]),
            expires=datetime.fromisoformat(row["expires_at"]), direction=direction,
            model=row["model"], thesis=(
                f"{p:.1%} UP / {1-p:.1%} DOWN. Frozen research forecast; no verified edge."
                if research else "Contest-qualified model; first checkpoint call is frozen."),
            risk="Prospective paper evidence; executable fills and future profit are unverified.",
            evidence_sha256=row["evidence_sha256"], kind="model",
            probability_up=p, settlement_schema=row["schema_key"],
            condition_id=row["condition_id"],
            qualified_at_issue=row["qualified_at_issue"],
        )

    async def _refresh_league(self) -> None:
        """Recompute the per-market contest at most every 10 minutes."""
        now = datetime.now(UTC)
        if self._league is not None and now - self._league[0] < timedelta(minutes=10):
            return
        store = getattr(getattr(self.research, "learner", None), "store", None)
        if store is None:
            return
        try:
            result = await asyncio.wait_for(asyncio.to_thread(league, store), timeout=20)
        except Exception:  # noqa: BLE001 - keep the last result; never guess a champion
            _LOG.info("Model league refresh unavailable")
            return
        self._league = (now, result)

    async def cycle(self) -> int:
        """Deliver each shared market when ready; slow peers cannot delay it."""
        await self._refresh_league()
        async with self._cycle_lock:
            trackers = await asyncio.to_thread(self.store.active)
            by_key: dict[tuple[str, int], list[LivePolymarketTracker]] = {}
            for tracker in trackers:
                by_key.setdefault((tracker.asset, tracker.horizon), []).append(tracker)
            budget = asyncio.Semaphore(8)
            # Quote/event reads are limited separately from message edits.
            # Without this budget twelve tracked asset/timeframe boards can
            # launch twelve simultaneous Gamma + dual-CLOB lookups on top of
            # the independent background bulk scanner, exhausting host HTTP
            # connections and creating synchronized request timeouts.
            observation_budget = asyncio.Semaphore(4)

            async def update(tracker, embed, fingerprint) -> int:
                async with budget, self._user_locks.setdefault(tracker.user_id, asyncio.Lock()):
                    # A Stop or retarget during this shared read takes effect
                    # before editing. Never restore the previous target's card.
                    current = await asyncio.to_thread(self.store.get, tracker.user_id)
                    if current != tracker or tracker.fingerprint == fingerprint:
                        return 0
                    try:
                        channel = self.bot.get_partial_messageable(int(tracker.channel_id))
                        await asyncio.wait_for(
                            channel.get_partial_message(int(tracker.message_id)).edit(
                                embed=embed, view=self.dm_view,
                            ), timeout=5,
                        )
                    except discord.NotFound as exc:
                        if exc.code != 10008:
                            # Unknown channel, not an erased message: do not
                            # send into an unverified destination or opt out.
                            _LOG.warning("Live Polymarket DM destination missing user=%s code=%s",
                                         tracker.user_id, exc.code)
                            return 0
                        try:
                            # A deleted card should not silently disable tracking.
                            # This user lock also prevents Stop/re-target races.
                            sent = await asyncio.wait_for(
                                channel.send(embed=embed, view=self.dm_view), timeout=5,
                            )
                            await asyncio.to_thread(
                                self.store.save,
                                LivePolymarketTracker(
                                    user_id=tracker.user_id, asset=tracker.asset,
                                    horizon=tracker.horizon, channel_id=tracker.channel_id,
                                    message_id=str(sent.id), fingerprint=fingerprint,
                                ),
                            )
                            _LOG.info("Recovered deleted live Polymarket DM panel user=%s",
                                      tracker.user_id)
                            return 1
                        except (TimeoutError, discord.HTTPException):
                            _LOG.warning("Could not restore deleted Polymarket DM user=%s",
                                         tracker.user_id)
                            return 0
                    except (discord.HTTPException, TimeoutError) as exc:
                        _LOG.warning(
                            "Live Polymarket DM update failed user=%s reason=%s",
                            tracker.user_id, type(exc).__name__,
                        )
                    else:
                        await asyncio.to_thread(self.store.changed, tracker, fingerprint)
                        return 1
                    return 0

            async def observe(key, members) -> int:
                try:
                    async with observation_budget:
                        embed, fingerprint = await self._observation(*key)
                    # Release the quote-read budget before Discord edits.
                    # One slow DM must not block another verified book.
                    changed = sum(await asyncio.gather(*(
                        update(tracker, embed, fingerprint) for tracker in members
                    )))
                    saved = self._live_cache.get(key)
                    if saved is not None and saved[2] is not None:
                        await asyncio.gather(*(
                            self._ping(tracker, saved[1], saved[2]) for tracker in members
                        ))
                    return changed
                except Exception:  # noqa: BLE001 - independent markets stay live
                    _LOG.exception("Live Polymarket board update failed asset=%s horizon=%s", *key)
                    return 0

            return sum(await asyncio.gather(*(
                observe(key, members) for key, members in by_key.items()
            )))

    async def _ping(self, tracker: LivePolymarketTracker, board, call: ContractCall) -> None:
        """One new DM (which notifies) when a contract's frozen UP/DOWN call is issued.

        Only fresh calls: issued within the last minute and with more than a
        minute left, so a restart never pings an old call and nothing pings
        in a contract's final minute. WAIT never pings. Edits to the live card
        do not notify on Discord, so this is a separate short message.
        """
        now = datetime.now(UTC)
        # Every model and every alert path obey the same qualified contest.
        entry = self._league_entry(tracker.asset, tracker.horizon)
        if (entry is None or entry.get("champion") != call.model
                or call.qualified_at_issue is False
                or call.direction not in ("UP", "DOWN")
                or not matching_call(board, call, asset=tracker.asset, horizon=tracker.horizon)
                or not timedelta(0) <= now - call.issued_at <= timedelta(seconds=60)
                or call.expires - now <= timedelta(seconds=60)
                or self._pinged.get(tracker.user_id) == call.slug):
            return
        quote = board.current_quote
        if (quote is None or not quote.condition_id or call.condition_id != quote.condition_id
                or call.settlement_schema != settlement_schema(quote)
                or not timedelta(0) <= now-quote.quote_at <= timedelta(seconds=25)):
            return
        from parlay_bot.perpetuals.model_league import EDGE, STRESS
        p = call.probability_up
        cost = quote.up_cost if call.direction == "UP" else quote.down_cost
        chance_now = p if call.direction == "UP" else 1-p if p is not None else None
        if (cost is None or chance_now is None or chance_now-cost-STRESS < EDGE):
            return
        self._pinged[tracker.user_id] = call.slug
        if len(self._pinged) > 2 * MAX_TRACKERS:
            self._pinged = {tracker.user_id: call.slug}
        current = await asyncio.to_thread(self.store.get, tracker.user_id)
        if current is None or (current.asset, current.horizon) != (tracker.asset, tracker.horizon):
            return
        if not await asyncio.to_thread(self.store.pings_on, tracker.user_id):
            return
        chance = (f" (model: {call.probability_up * 100:.1f}% UP)"
                  if call.probability_up is not None else "")
        quote = board.current_quote
        price = (f"Polymarket price now: UP ${quote.up_ask:.2f} • DOWN ${quote.down_ask:.2f}\n"
                 if quote is not None else "")
        text = (
            f"🔔 **{tracker.asset} {tracker.horizon}m on Polymarket — call issued: "
            f"{call.direction}**{chance}\n"
            f"{price}"
            f"{value_line(call, quote)}"
            f"Closes <t:{int(call.expires.timestamp())}:R>. This call is frozen for the "
            f"whole contract and will not flip.\n"
            f"{league_text(entry, asset=tracker.asset, horizon=tracker.horizon)}\n"
            f"[Open live card](https://discord.com/channels/@me/{tracker.channel_id}/"
            f"{tracker.message_id})"
        )
        try:
            channel = self.bot.get_partial_messageable(int(tracker.channel_id))
            await asyncio.wait_for(channel.send(content=text), timeout=5)
        except (discord.HTTPException, TimeoutError) as exc:
            _LOG.warning("Live Polymarket call ping failed user=%s reason=%s",
                         tracker.user_id, type(exc).__name__)
