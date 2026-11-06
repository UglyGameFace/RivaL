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


MAX_CHANGE_ALERTS = 3  # per tracker per contract; after that the card says it keeps changing
# Heads-up BEFORE a flip: the call sits this close to 50/50, so a small move
# in Polymarket's prices flips it. A starting threshold, not a measured one.
EDGE_MARGIN = 0.05
HEADS_UP_MIN_LEFT = timedelta(seconds=60)  # less than this: too late to act on


def changes_text(changes) -> str:
    """'DOWN → UP 2 minutes ago' lines, newest first (Discord-relative times)."""
    return "\n".join(
        f"**{old} → {new}** <t:{int(at.timestamp())}:R>" for at, old, new in reversed(changes[-3:])
    )


def live_polymarket_embed(
    board: PolymarketWindowBoard | None, snapshot: ContractCall | None, *,
    asset: str, horizon: int, ai_status: str | None = None, changes=(),
) -> discord.Embed:
    current = board.current if board is not None else None
    valid = matching_call(board, snapshot, asset=asset, horizon=horizon)
    direction = snapshot.direction if valid else "WAIT"
    embed = discord.Embed(
        title=f"⚡ {asset} • {horizon} min | Polymarket {snapshot.kind if valid else 'AI'} {direction}",
        description=(
            f"**Experimental contract-specific {snapshot.kind} research.** "
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
    if valid and changes:
        embed.add_field(
            name=f"⚠️ Call changed {len(changes)}× during this contract",
            value=changes_text(changes) + "\nThe call is re-made from live prices, so "
            "it can flip; it is not a trading signal.",
            inline=False,
        )
    if valid:
        embed.add_field(name=f"🧠 {snapshot.kind} contract call", value=f"**{direction}** • {snapshot.thesis}", inline=False)
        embed.add_field(name="Call timing", value=(
            f"Issued <t:{int(snapshot.issued_at.timestamp())}:R> for this contract only. "
            "Live quotes update separately; this is not a new AI call every tick."
        ), inline=False)
        embed.add_field(name="Research limitation", value=snapshot.risk, inline=False)
        if ai_status:
            embed.add_field(name="AI refresh", value=ai_status, inline=False)
    if board is not None and board.next_window is not None:
        next_window = board.next_window
        embed.add_field(name="⏭️ Next verified contract", value=(
            f"[Next event]({next_window.url}) • opens <t:{int(next_window.opens.timestamp())}:R>"
        ), inline=False)
    embed.set_footer(text="Automatic ~15s market updates • AI calls show their own issue time • No orders")
    return embed


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
        self._lock = asyncio.Lock()
        self._cycle_lock = asyncio.Lock()
        self._user_locks: dict[str, asyncio.Lock] = {}
        # In-memory verified source/evidence snapshots. The guided menu
        # renders these without another network or PostgreSQL request.
        self._live_cache: dict[
            tuple[str, int], tuple[datetime, PolymarketWindowBoard, ContractCall | None]
        ] = {}
        self._prime_pending: dict[tuple[str, int], asyncio.Task] = {}
        # (user_id, slug) -> how many call changes that user was already
        # alerted about (or had already happened when they first saw it).
        self._alerted: dict[tuple[str, str], int] = {}

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
            self._live_cache[(asset, horizon)] = (datetime.now(UTC), board, snapshot)
        embed = live_polymarket_embed(
            board, snapshot, asset=asset, horizon=horizon,
            changes=self.call_changes(board),
            ai_status=(self.research.refresh_note(board.current.slug) if snapshot is not None
                       else self.research.status(board.current.slug)
                       if self.research is not None and board is not None and board.current is not None else None),
        )
        # No live clock in the fingerprint: unchanged markets never spam edits.
        digest = hashlib.sha256(
            str(embed.to_dict()).encode("utf-8"),
        ).hexdigest()
        return embed, digest

    def call_changes(self, board) -> list:
        changes = getattr(self.research, "changes", None)
        if not callable(changes) or board is None or board.current is None:
            return []
        return changes(board.current.slug)

    async def _alert_change(self, tracker, board, snapshot, changes) -> None:
        """A NEW DM (edits don't notify) when the call flips mid-contract."""
        if (board is None or board.current is None
                or not matching_call(board, snapshot, asset=tracker.asset,
                                     horizon=tracker.horizon)):
            return
        await self._heads_up(tracker, board, snapshot)
        key = (tracker.user_id, board.current.slug)
        if key not in self._alerted:
            # First look at this contract: earlier flips are on the card;
            # only alert for flips from now on.
            self._alerted[key] = len(changes)
            return
        if len(changes) <= self._alerted[key] or self._alerted[key] >= MAX_CHANGE_ALERTS:
            return
        self._alerted[key] = len(changes)
        if board.current.closes - datetime.now(UTC) < HEADS_UP_MIN_LEFT:
            # Under a minute left there is no time to act safely; a ping would
            # only invite a rushed trade. The card still shows the change.
            return
        _, old, new = changes[-1]
        quote = board.current_quote
        detail = (
            f"[Open contract]({board.current.url}) • closes "
            f"<t:{int(board.current.closes.timestamp())}:R>"
            + (f"\nUP ask ${quote.up_ask:.2f} • DOWN ask ${quote.down_ask:.2f}"
               if quote is not None else "")
            + "\nRivaL re-makes this call from live prices during the contract, "
              "so it can flip. Its measured record counts only its first call per "
              "contract, and that record is not better than Polymarket's own odds. "
              "Not a trading signal."
        )
        if self._alerted[key] >= MAX_CHANGE_ALERTS:
            detail += "\nThis call keeps changing; no more alerts for this contract."
        try:
            channel = self.bot.get_partial_messageable(int(tracker.channel_id))
            await asyncio.wait_for(channel.send(
                content=(f"⚠️ **RivaL changed its {tracker.asset} {tracker.horizon}m "
                         f"Polymarket call: {old} → {new}**"),
                embed=discord.Embed(description=detail, color=discord.Color.red()),
            ), timeout=5)
        except (discord.HTTPException, TimeoutError) as exc:
            _LOG.warning("Polymarket call-change alert failed user=%s reason=%s",
                         tracker.user_id, type(exc).__name__)
        if len(self._alerted) > 2000:
            self._alerted = dict(list(self._alerted.items())[-1000:])

    async def _heads_up(self, tracker, board, snapshot) -> None:
        """Warn early, once per contract, while the call is close to flipping."""
        p = snapshot.probability_up
        left = board.current.closes - datetime.now(UTC)
        key = (tracker.user_id, board.current.slug, "edge")
        if (p is None or abs(p - 0.5) >= EDGE_MARGIN or left < HEADS_UP_MIN_LEFT
                or key in self._alerted):
            return
        self._alerted[key] = 1
        detail = (
            f"[Open contract]({board.current.url}) • closes "
            f"<t:{int(board.current.closes.timestamp())}:R>\n"
            f"The model is only {max(p, 1 - p):.0%} sure of {snapshot.direction}: a small "
            "move in Polymarket's prices can flip this call before the contract ends. "
            "You'll get another message if it does flip. Not a trading signal."
        )
        try:
            channel = self.bot.get_partial_messageable(int(tracker.channel_id))
            await asyncio.wait_for(channel.send(
                content=(f"🟡 **Heads-up: RivaL's {tracker.asset} {tracker.horizon}m "
                         f"Polymarket call ({snapshot.direction}) could flip**"),
                embed=discord.Embed(description=detail, color=discord.Color.gold()),
            ), timeout=5)
        except (discord.HTTPException, TimeoutError) as exc:
            _LOG.warning("Polymarket heads-up failed user=%s reason=%s",
                         tracker.user_id, type(exc).__name__)

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
                if previous is not None:
                    # Re-target the same existing message, never send a new
                    # notification after an ambiguous Discord edit failure.
                    if int(previous.channel_id) != channel.id:
                        return False, "Old tracker channel unavailable; stop first."
                    await asyncio.wait_for(
                        channel.get_partial_message(
                            int(previous.message_id)
                        ).edit(embed=embed), timeout=5,
                    )
                    message_id = previous.message_id
                else:
                    sent = await asyncio.wait_for(
                        channel.send(embed=embed), timeout=5,
                    )
                    message_id = str(sent.id)
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
                f"Auto-tracking {asset} {horizon}m in one continuously edited "
                "DM. It updates without pressing Refresh."
            )

    async def stop(self, user_id: int) -> bool:
        async with self._user_locks.setdefault(str(user_id), asyncio.Lock()):
            previous = await asyncio.to_thread(self.store.get, str(user_id))
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
                        )), timeout=5,
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
                            channel.get_partial_message(int(tracker.message_id)).edit(embed=embed),
                            timeout=5,
                        )
                    except discord.NotFound:
                        await asyncio.to_thread(self.store.stop, tracker.user_id)
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
                    cached = self._live_cache.get(key)
                    if cached is not None:
                        _, board, snapshot = cached
                        # Every cycle, so the baseline is set before the first
                        # flip and that first flip alerts too.
                        changes = self.call_changes(board)
                        await asyncio.gather(*(
                            self._alert_change(tracker, board, snapshot, changes)
                            for tracker in members
                        ))
                    # Release the quote-read budget before Discord edits.
                    # One slow DM must not block another verified book.
                    return sum(await asyncio.gather(*(
                        update(tracker, embed, fingerprint) for tracker in members
                    )))
                except Exception:  # noqa: BLE001 - independent markets stay live
                    _LOG.exception("Live Polymarket board update failed asset=%s horizon=%s", *key)
                    return 0

            return sum(await asyncio.gather(*(
                observe(key, members) for key, members in by_key.items()
            )))
