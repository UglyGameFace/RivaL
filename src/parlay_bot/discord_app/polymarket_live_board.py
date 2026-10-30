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
        title=f"⚡ {asset} • {horizon} min | Polymarket AI {direction}",
        description=(
            "**Experimental contract-specific AI research.** "
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
        embed.add_field(name="Settlement source", value=(
            f"Binance {asset}/USDT hourly candle, as specified by this Polymarket event"
            if horizon == 60 else "The Chainlink reference in this event's published rules"
        ), inline=False)
    if valid:
        embed.add_field(name="🧠 AI contract call", value=f"**{direction}** • {snapshot.thesis}", inline=False)
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
            except Exception:  # noqa: BLE001
                _LOG.warning(
                    "Live Polymarket verified window unavailable asset=%s "
                    "horizon=%dm", asset, horizon,
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
            ai_status=(self.research.refresh_note(board.current.slug) if snapshot is not None
                       else self.research.status(board.current.slug)
                       if self.research is not None and board is not None and board.current is not None else None),
        )
        # No live clock in the fingerprint: unchanged markets never spam edits.
        digest = hashlib.sha256(
            str(embed.to_dict()).encode("utf-8"),
        ).hexdigest()
        return embed, digest

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
                    embed, fingerprint = await self._observation(*key)
                    return sum(await asyncio.gather(*(
                        update(tracker, embed, fingerprint) for tracker in members
                    )))
                except Exception:  # noqa: BLE001 - independent markets stay live
                    _LOG.exception("Live Polymarket board update failed asset=%s horizon=%s", *key)
                    return 0

            return sum(await asyncio.gather(*(
                observe(key, members) for key, members in by_key.items()
            )))
