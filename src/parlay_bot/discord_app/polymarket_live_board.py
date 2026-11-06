"""Opt-in, restart-safe, automatically edited Polymarket tracking DM.

One private message per user changes in place as genuine current/next Gamma
markets, CLOB books and separately sourced RivaL forecasts change.
No order execution, predicted Polymarket strike or fabricated win probability.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
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
) -> discord.Embed:
    current = board.current if board is not None else None
    valid = matching_call(board, snapshot, asset=asset, horizon=horizon)
    direction = snapshot.direction if valid else "WAIT"
    embed = discord.Embed(
        title=f"📡 {asset} • {horizon} min | Polymarket {snapshot.kind if valid else 'AI'} {direction}",
        description=(
            f"**Experimental contract-specific {snapshot.kind} research.** "
            "The first model forecast is frozen; live prices update independently. "
            "Accuracy and profitable edge are not established."
            if valid else "**No current contract-specific AI call.** "
            + (ai_status or "Waiting for verified contract inputs and available free AI.")
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
    if valid:
        call_label = ("🧠 First model call (frozen)"
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


class LivePolymarketDMView(discord.ui.View):
    """Restart-persistent DM controls; authorize against the active tracker."""

    def __init__(self, service: LivePolymarketBoardService) -> None:
        super().__init__(timeout=None)
        self.service = service

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
        # (asset, horizon) -> (computed_at, record line) for the ping text.
        self._records: dict[tuple[str, int], tuple[datetime, str]] = {}

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
            ), (self.research.peek(board.current.slug)
                if self.research is not None and board.current is not None else saved[2])
        call = (self.research.peek(board.current.slug)
                if self.research is not None and board.current is not None else saved[2])
        return board, call

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
            self._live_cache[(asset, horizon)] = (datetime.now(UTC), board, snapshot)
        embed = live_polymarket_embed(
            board, snapshot, asset=asset, horizon=horizon,
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
        await interaction.response.defer(ephemeral=True, thinking=True)
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

    async def cycle(self) -> int:
        """Deliver each shared market when ready; slow peers cannot delay it."""
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

    async def _record_line(self, asset: str, horizon: int) -> str:
        """The model's measured 7-day record against the market's own price."""
        now = datetime.now(UTC)
        saved = self._records.get((asset, horizon))
        if saved is not None and now - saved[0] < timedelta(minutes=10):
            return saved[1]
        line = "Track record unavailable right now. Not a proven edge."
        learner = getattr(self.research, "learner", None)
        if learner is not None:
            try:
                summary = await asyncio.wait_for(asyncio.to_thread(
                    learner.summary, asset=asset, horizon=horizon), timeout=3)
                n = int(summary.get("settled_forward_contracts") or 0)
                model, market = summary.get("forward_brier"), summary.get("market_baseline_brier")
                if n and model is not None and market is not None:
                    verdict = ("beat" if model < market else "did not beat")
                    line = (f"Last 7 days, {n:,} settled {asset} {horizon}m calls: the model "
                            f"{verdict} the market's own price (error {model:.4f} vs "
                            f"{market:.4f}, lower is better). Not a proven edge.")
                else:
                    line = "No settled calls to score yet. Not a proven edge."
            except Exception:  # noqa: BLE001 - the ping never waits on a slow record
                _LOG.info("Polymarket ping record unavailable asset=%s horizon=%s", asset, horizon)
        self._records[(asset, horizon)] = (now, line)
        return line

    async def _ping(self, tracker: LivePolymarketTracker, board, call: ContractCall) -> None:
        """One new DM (which notifies) when a contract's frozen UP/DOWN call is issued.

        Only fresh calls: issued within the last minute and with more than a
        minute left, so a restart never pings an old call and nothing pings
        in a contract's final minute. WAIT never pings. Edits to the live card
        do not notify on Discord, so this is a separate short message.
        """
        now = datetime.now(UTC)
        if (call.direction not in ("UP", "DOWN")
                or not matching_call(board, call, asset=tracker.asset, horizon=tracker.horizon)
                or now - call.issued_at > timedelta(seconds=60)
                or call.expires - now <= timedelta(seconds=60)
                or self._pinged.get(tracker.user_id) == call.slug):
            return
        self._pinged[tracker.user_id] = call.slug
        if len(self._pinged) > 2 * MAX_TRACKERS:
            self._pinged = {tracker.user_id: call.slug}
        current = await asyncio.to_thread(self.store.get, tracker.user_id)
        if current is None or (current.asset, current.horizon) != (tracker.asset, tracker.horizon):
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
            f"Closes <t:{int(call.expires.timestamp())}:R>. This call is frozen for the "
            f"whole contract and will not flip.\n"
            f"{await self._record_line(tracker.asset, tracker.horizon)}\n"
            f"[Open live card](https://discord.com/channels/@me/{tracker.channel_id}/"
            f"{tracker.message_id})"
        )
        try:
            channel = self.bot.get_partial_messageable(int(tracker.channel_id))
            await asyncio.wait_for(channel.send(content=text), timeout=5)
        except (discord.HTTPException, TimeoutError) as exc:
            _LOG.warning("Live Polymarket call ping failed user=%s reason=%s",
                         tracker.user_id, type(exc).__name__)
