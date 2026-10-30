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
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import discord

from parlay_bot.discord_app.polymarket_watch import (
    ASSETS,
    HORIZONS,
    PolymarketReader,
    PolymarketWindowBoard,
)
from parlay_bot.discord_app.targeting import TargetSnapshot, target_snapshot
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


def live_polymarket_embed(
    board: PolymarketWindowBoard | None, snapshot: TargetSnapshot | None, *,
    asset: str, horizon: int,
) -> discord.Embed:
    """Never present exchange-candle model research as Polymarket-specific odds."""
    embed = discord.Embed(
        title=f"⚡ Live Polymarket Tracker | {asset} {horizon}m",
        description=(
            "**Auto-updating** current/next verified global Polymarket "
            "markets and separate RivaL forward research. "
            "This does not place a bet, establish an edge, or promise a win."
        ), color=discord.Color.blurple(),
    )
    if board is None or board.current is None:
        current = "No current listing verified. No invented target or price."
    else:
        listing = board.current
        current = (
            f"[Open verified event]({listing.url}) • "
            + ("Accepting orders" if listing.accepting_orders else
               "Order acceptance unverified")
            + f" • Closes <t:{int(listing.closes.timestamp())}:R>\n"
            + "Price to beat: "
            + (
                f"${listing.price_to_beat:,.4f} (Gamma metadata)"
                if listing.price_to_beat is not None
                else "Not verified by Polymarket"
            )
        )
        if board.current_quote:
            quote = board.current_quote
            current += (
                f"\n**Up ask {quote.up_ask:.1%}** • "
                f"**Down ask {quote.down_ask:.1%}** "
                f"• Book <t:{int(quote.quote_at.timestamp())}:R>"
            )
        else:
            current += "\nNo fresh verified CLOB book."
    embed.add_field(name="Current event", value=current, inline=False)
    if board is None or board.next_window is None:
        upcoming = "Not yet confirmed listed; automatically checking again."
    else:
        nxt = board.next_window
        upcoming = (
            f"[Open next listed event]({nxt.url})"
            f" • Opens <t:{int(nxt.opens.timestamp())}:R>\n"
            "Next price to beat unknown until Polymarket publishes it."
        )
    embed.add_field(name="Next event", value=upcoming, inline=False)
    if snapshot is None or snapshot.status != "ok":
        forecast = (
            "WAIT • No currently verified RivaL forward forecast. "
            "Updating automatically when a new eligible model result arrives."
        )
    else:
        end = (
            f" • Expires <t:{int(snapshot.ends_at.timestamp())}:R>"
            if snapshot.ends_at else ""
        )
        forecast = (
            f"**{snapshot.direction}** (unverified model lean, "
            f"not a Polymarket prediction) • "
            f"Model score {snapshot.probability:.1%} • "
            f"Exchange {snapshot.venue}{end}"
        )
    embed.add_field(
        name="RivaL's latest exchange-candle direction",
        value=forecast, inline=False,
    )
    embed.set_footer(text="Auto-updates about every 15s; stops with /rival → Live Mode")
    return embed


class LivePolymarketBoardService:
    def __init__(
        self, *, bot: discord.Client, store: LivePolymarketBoardStore,
        reader: PolymarketReader, market: PerpetualMarketStore | None,
    ) -> None:
        self.bot = bot
        self.store = store
        self.reader = reader
        self.market = market
        self._lock = asyncio.Lock()
        self._model_gate = asyncio.Semaphore(2)
        # A to_thread timeout does not terminate its PostgreSQL query.
        # Retain that task until completion rather than starting duplicate
        # database work every 15 seconds during a slow checkout.
        self._model_pending: dict[
            tuple[str, int], asyncio.Task[TargetSnapshot]
        ] = {}

    async def _observation(
        self, asset: str, horizon: int,
    ) -> tuple[discord.Embed, str]:
        async def load_board():
            try:
                return await asyncio.wait_for(
                    self.reader.window_board(asset, horizon), timeout=4.6,
                )
            except Exception:  # noqa: BLE001
                _LOG.warning(
                    "Live Polymarket verified window unavailable asset=%s "
                    "horizon=%dm", asset, horizon,
                )
                return None

        async def load_model():
            key = (asset, horizon)

            async def read_once() -> TargetSnapshot:
                async with self._model_gate:
                    return await asyncio.to_thread(
                        target_snapshot, self.market,
                        asset=asset, horizon_minutes=horizon,
                    )

            pending = self._model_pending.get(key)
            if pending is None:
                pending = asyncio.create_task(read_once())
                self._model_pending[key] = pending
            try:
                return await asyncio.wait_for(
                    asyncio.shield(pending), timeout=2.5,
                )
            except Exception:  # noqa: BLE001
                _LOG.warning(
                    "Live Polymarket forecast evidence unavailable asset=%s "
                    "horizon=%dm; next cycle will reuse in-flight read",
                    asset, horizon,
                )
                return None
            finally:
                if pending.done():
                    self._model_pending.pop(key, None)

        board, snapshot = await asyncio.gather(load_board(), load_model())
        embed = live_polymarket_embed(
            board, snapshot, asset=asset, horizon=horizon,
        )
        # No live clock in the fingerprint: unchanged markets never spam edits.
        digest = hashlib.sha256(
            str(embed.to_dict()).encode("utf-8"),
        ).hexdigest()
        return embed, digest

    async def start(self, user: discord.abc.User, *,
                    asset: str, horizon: int) -> tuple[bool, str]:
        if asset not in ASSETS or horizon not in HORIZONS:
            return False, "Select BTC, ETH, SOL or XRP and 5m/15m."
        async with self._lock:
            previous = await asyncio.to_thread(self.store.get, str(user.id))
            if not await asyncio.to_thread(self.store.can_start, str(user.id)):
                return False, "The automatic live tracker is at capacity."
            embed, fingerprint = await self._observation(asset, horizon)
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
        async with self._lock:
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
        """Shared market reads, one edited DM per active member on change."""
        async with self._lock:
            trackers = await asyncio.to_thread(self.store.active)
            if not trackers:
                return 0
            keys = {(t.asset, t.horizon) for t in trackers}
            # At most eight verified market pairs for 100 subscribers.
            results = await asyncio.gather(*(
                self._observation(asset, horizon)
                for asset, horizon in sorted(keys)
            ), return_exceptions=True)
            by_key = {
                key: result for key, result in zip(sorted(keys), results, strict=True)
                if not isinstance(result, BaseException)
            }
            edited = 0
            for tracker in trackers:
                observed = by_key.get((tracker.asset, tracker.horizon))
                if observed is None:
                    continue
                embed, fingerprint = observed
                if tracker.fingerprint == fingerprint:
                    continue
                try:
                    channel = self.bot.get_partial_messageable(
                        int(tracker.channel_id),
                    )
                    await asyncio.wait_for(
                        channel.get_partial_message(
                            int(tracker.message_id)
                        ).edit(embed=embed), timeout=5,
                    )
                except discord.NotFound:
                    # A user deleted the card; never recreate/send without
                    # a fresh button tap and DM consent.
                    await asyncio.to_thread(
                        self.store.stop, tracker.user_id,
                    )
                except (discord.HTTPException, TimeoutError) as exc:
                    _LOG.warning(
                        "Live Polymarket DM update failed user=%s reason=%s",
                        tracker.user_id, type(exc).__name__,
                    )
                else:
                    await asyncio.to_thread(
                        self.store.changed, tracker, fingerprint,
                    )
                    edited += 1
            return edited
