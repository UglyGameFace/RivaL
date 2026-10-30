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
    PolymarketReader,
    PolymarketWindowBoard,
    unavailable_text,
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
    """One compact live card; exchange direction never masquerades as oracle odds.

    5m/15m Polymarket settles on the contract's Chainlink BTC/USD etc TWAP.
    The existing exchange model predicts a FIVE-minute candle at a target
    that can be 5 or 15 minutes away, not that contract's oracle outcome.
    """
    current = board.current if board is not None else None
    observed = board.checked_at if board is not None else datetime.now(UTC)
    valid_lean = (
        snapshot is not None
        and snapshot.status == "ok"
        and snapshot.asset == asset
        and snapshot.horizon_minutes == horizon
        and snapshot.direction in ("UP", "DOWN")
        and snapshot.probability is not None
        and 0.5 < snapshot.probability < 1
        and snapshot.issued_at is not None
        and snapshot.issued_at <= observed
        and snapshot.ends_at is not None
        and snapshot.ends_at > observed
        and bool(snapshot.venue)
    )
    direction = snapshot.direction if valid_lean else "WAIT"
    embed = discord.Embed(
        title=f"⚡ {asset} • {horizon} min | Polymarket WAIT",
        description=(
            f"**Exchange model: {direction} "
            + ("LEAN (research only)**" if valid_lean
               else "• NO FRESH FORECAST**")
            + "\n**Polymarket contract: WAIT (edge unverified).** "
            "A model lean is not a calibrated contract win probability."
        ),
        color=discord.Color.orange(),
    )
    if current is not None and current.price_to_beat is not None:
        reference = f"${current.price_to_beat:,.4f} • Polymarket strike"
    elif current is not None and board.current_quote is not None:
        reference = (
            f"Up ${board.current_quote.up_ask:.2f}/share • "
            f"Down ${board.current_quote.down_ask:.2f}/share"
        )
    else:
        reference = "Price not verified"
    expiry = (
        f"<t:{int(current.closes.timestamp())}:R>"
        if current is not None else "No verified market"
    )
    strength = (
        f"{snapshot.probability:.1%} raw model score"
        if valid_lean else "No fresh model score"
    )
    # A 7-day exchange-candle hit rate and a contract-specific win record
    # are different metrics. No contract-outcome ledger exists in this path,
    # so showing 58% 'accuracy' from unrelated labels would be dishonest.
    accuracy = "Polymarket: not validated"
    for index, (name, value) in enumerate((
        ("💰 Price / Share Cost", reference),
        ("⏱️ Expires", expiry),
        ("⚡ Model Strength", strength),
        ("📊 Accuracy", accuracy),
    )):
        embed.add_field(name=name, value=value, inline=True)
        if index in (1, 3):
            embed.add_field(name="\u200b", value="\u200b", inline=True)
    if current is None:
        market = "No current verified Gamma listing."
    else:
        market = (
            f"[Current verified event]({current.url}) • "
            + ("Accepting orders" if current.accepting_orders
               else "Order acceptance unverified")
        )
        if board.current_quote is not None:
            market += (
                f"\nLive asks: Up ${board.current_quote.up_ask:.2f}/share"
                f" • Down ${board.current_quote.down_ask:.2f}/share"
                f" • observed <t:{int(board.current_quote.quote_at.timestamp())}:R>"
            )
        else:
            market += "\n" + unavailable_text(board.quote_unavailable_reason)
    embed.add_field(name="📊 Current Market", value=market, inline=False)
    if board is None or board.next_window is None:
        future = "Next window not listed or not verified yet."
    else:
        next_window = board.next_window
        future = (
            f"[Next verified event]({next_window.url})"
            f" • Opens <t:{int(next_window.opens.timestamp())}:R>"
            " • Next strike unknown until Polymarket publishes it"
        )
    embed.add_field(name="⏭️ Next Market", value=future, inline=False)

    if valid_lean:
        aligned = (
            current is not None
            and current.accepting_orders
            and board.current_quote is not None
            and horizon == 5
            and snapshot.ends_at == current.closes
            and current.opens <= snapshot.issued_at < current.closes
        )
        target = discord.utils.format_dt(snapshot.ends_at, style="R")
        if aligned:
            evidence = (
                f"**{direction}** research lean for a 5m exchange candle "
                f"with the same closing time ({target})."
            )
        else:
            evidence = (
                f"**{direction}** research lean for a separate future "
                f"5m exchange candle ending {target}, **not this "
                f"{horizon}m Polymarket contract**."
            )
        evidence += (
            f" Source: {snapshot.venue}."
            + (
                f" {snapshot.training_evaluated} walk-forward examples"
                " evaluated on exchange candles."
                if snapshot.training_evaluated else
                " Walk-forward example count not verified."
            )
            + " Oracle and settlement calibration not established."
        )
    else:
        reason = (
            snapshot.detail if snapshot is not None else
            "Forecast lookup unavailable"
        )
        evidence = f"**WAIT • {reason}** No stale direction reused."
    embed.add_field(
        name="🧠 RivaL Model Research (separate from Polymarket)",
        value=evidence, inline=False,
    )
    embed.add_field(
        name="What determines an actual contract call?",
        value=(
            "Verified Chainlink TWAP settlement labels, correctly matched "
            "market windows, prospective calibrated predictions and "
            "after-cost results. Until those exist, contract verdict is WAIT. "
            "Share asks are prices, not odds generated by RivaL."
        ),
        inline=False,
    )
    embed.set_footer(
        text="Automatic ~15s updates • No bet placed or profitable edge proven",
    )
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
        self._cycle_lock = asyncio.Lock()
        self._user_locks: dict[str, asyncio.Lock] = {}
        self._model_gate = asyncio.Semaphore(2)
        # A to_thread timeout does not terminate its PostgreSQL query.
        # Retain that task until completion rather than starting duplicate
        # database work every 15 seconds during a slow checkout.
        self._model_pending: dict[
            tuple[str, int], asyncio.Task[TargetSnapshot]
        ] = {}
        # In-memory verified source/evidence snapshots. The guided menu
        # renders these without another network or PostgreSQL request.
        self._live_cache: dict[
            tuple[str, int], tuple[datetime, PolymarketWindowBoard, TargetSnapshot | None]
        ] = {}
        self._prime_pending: dict[tuple[str, int], asyncio.Task] = {}

    def peek(self, asset: str, horizon: int) -> tuple[PolymarketWindowBoard, TargetSnapshot | None] | None:
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

    async def preview(self, asset: str, horizon: int) -> tuple[PolymarketWindowBoard, TargetSnapshot | None] | None:
        """One short warmup read; never block menu on the model DB pool."""
        cached = self.peek(asset, horizon)
        if cached is not None:
            return cached
        key = (asset, horizon)
        task = self._prime_pending.get(key)
        if task is None or task.done():
            task = asyncio.create_task(self._observation(asset, horizon))
            self._prime_pending[key] = task
            task.add_done_callback(
                lambda finished, k=key: self._prime_pending.pop(k, None)
                if self._prime_pending.get(k) is finished else None
            )
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=1.2)
        except (TimeoutError, Exception):  # noqa: BLE001 - warmup best effort
            return None
        return self.peek(asset, horizon)

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
        if board is not None:
            self._live_cache[(asset, horizon)] = (
                datetime.now(UTC), board, snapshot,
            )
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
        async with self._lock, self._user_locks.setdefault(str(user.id), asyncio.Lock()):
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
