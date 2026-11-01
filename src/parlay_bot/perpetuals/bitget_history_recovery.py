"""Restart-safe Bitget V3 USDT perpetual 5m history recovery.

Official history-candles returns at most 100 bars per bounded window and can
retrieve older-than-90-day data. Keep historical retrieval separate from the
live collector: an older candle first seen today cannot become a past signal.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from parlay_bot.perpetuals.history_inventory import (
    six_complete_months_start,
    six_months_prior,
)
from parlay_bot.perpetuals.history_recovery import (
    _ALLOWED,
    _STEP,
    GateHistoryRecovery,
    _num,
    _utc,
    align_forward_month_cursor,
    contiguous_forward_next,
)
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle, MarketDataError
from parlay_bot.perpetuals.market_store import PerpetualMarketStore

_LOG = logging.getLogger(__name__)
_BASE = "https://api.bitget.com/api/v3/market/"
# Four small source-native repairs per asset per scheduled cycle, only after
# the forward cursor has caught up. Maximum 16 HTTP gap reads per cycle.
_GAP_REPAIR_BURST = 4


class BitgetHistoryRecovery(GateHistoryRecovery):
    """One small, individually validated Bitget history page per asset/cycle."""

    def __init__(
        self, market: PerpetualMarketStore, *,
        client: httpx.AsyncClient | None = None,
        assets: tuple[str, ...] = _ALLOWED,
        page_bars: int = 100,
    ) -> None:
        if not 12 <= page_bars <= 100:
            raise ValueError("Bitget historical page cannot exceed 100 bars")
        super().__init__(market, client=client, assets=assets, page_bars=page_bars)
        self._metadata: dict[str, datetime] = {}

    async def _public(self, path: str, params: dict[str, Any]) -> Any:
        try:
            resp = await self._client.get(
                _BASE + path, params=params, follow_redirects=False,
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MarketDataError("Bitget historical futures API unavailable") from exc

    def _position(self, asset: str, now: datetime) -> dict:
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT * FROM perp_history_recovery
                   WHERE venue='bitget' AND asset=?""",
                (asset,),
            ).fetchone()
            if row is None:
                start = six_complete_months_start(now).isoformat()
                conn.execute(
                    """INSERT INTO perp_history_recovery
                       (venue,asset,started_from,next_opened_at)
                       VALUES ('bitget',?,?,?) ON CONFLICT DO NOTHING""",
                    (asset, start, start),
                )
                row = conn.execute(
                    """SELECT * FROM perp_history_recovery
                       WHERE venue='bitget' AND asset=?""",
                    (asset,),
                ).fetchone()
            assert row is not None
            return align_forward_month_cursor(
                conn, venue="bitget", asset=asset, now=now, row=dict(row),
            )

    def _advance(
        self, asset: str, *, now: datetime, next_open: datetime | None,
        inserted: int = 0, empty: bool = False,
        status: str = "progress", retry_after: datetime | None = None,
    ) -> None:
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE perp_history_recovery SET
                   next_opened_at=CASE WHEN ? > next_opened_at THEN ?
                                       ELSE next_opened_at END,
                   attempted_at=?,inserted_bars=inserted_bars+?,
                   empty_windows=empty_windows+?,status=?,retry_after=?
                   WHERE venue='bitget' AND asset=?""",
                (
                    next_open.isoformat() if next_open else None,
                    next_open.isoformat() if next_open else None,
                    now.isoformat(), inserted, int(empty), status,
                    retry_after.isoformat() if retry_after else None, asset,
                ),
            )

    def _pending_gaps(self, asset: str, *, now: datetime) -> int:
        floor = six_months_prior(now).isoformat()
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n FROM perp_history_gap_repair
                   WHERE venue='bitget' AND asset=? AND opened_at>=?""",
                (asset, floor),
            ).fetchone()
        return int(row["n"])

    def _claim_due_gap(
        self, asset: str, *, now: datetime, end: datetime,
    ) -> datetime | None:
        """Claim one audited, source-exact gap, never rewind the bulk cursor.

        The large Bitget historical ledger may have filled an audited slot
        since the hourly audit. Remove those obsolete queue items before
        issuing another API request. No other venue may satisfy this claim.
        """
        floor = six_months_prior(now).isoformat()
        with self.store.connect() as conn:
            conn.execute(
                """DELETE FROM perp_history_gap_repair
                   WHERE venue='bitget' AND asset=? AND opened_at>=?
                     AND EXISTS (
                       SELECT 1 FROM perp_market_candles AS c
                       WHERE c.venue='bitget' AND c.contract=?
                         AND c.quote='USDT' AND c.interval_minutes=5
                         AND c.opened_at=perp_history_gap_repair.opened_at
                     )""",
                (asset, floor, f"{asset}USDT"),
            )
            row = conn.execute(
                """UPDATE perp_history_gap_repair
                   SET attempted_at=?, retry_after=?
                   WHERE venue='bitget' AND asset=?
                     AND opened_at=(
                       SELECT opened_at FROM perp_history_gap_repair
                       WHERE venue='bitget' AND asset=? AND opened_at>=?
                         AND opened_at<? AND retry_after<=?
                       ORDER BY opened_at LIMIT 1
                     ) AND retry_after<=?
                   RETURNING opened_at""",
                (now.isoformat(), (now + timedelta(hours=6)).isoformat(),
                 asset, asset, floor, end.isoformat(), now.isoformat(),
                 now.isoformat()),
            ).fetchone()
        return _utc(datetime.fromisoformat(str(row["opened_at"]))) if row else None

    def _complete_gap(self, asset: str, opened: datetime) -> None:
        with self.store.connect() as conn:
            conn.execute(
                """DELETE FROM perp_history_gap_repair
                   WHERE venue='bitget' AND asset=? AND opened_at=?""",
                (asset, opened.isoformat()),
            )

    async def _instrument(self, asset: str, symbol: str, now: datetime) -> None:
        checked = self._metadata.get(symbol)
        if checked and timedelta(0) <= now - checked < timedelta(hours=1):
            return
        result = await self._public(
            "instruments", {"category": "USDT-FUTURES", "symbol": symbol},
        )
        entries = result.get("data") if isinstance(result, dict) else None
        if (
            not isinstance(result, dict) or result.get("code") != "00000"
            or not isinstance(entries, list)
            or len([
                x for x in entries if isinstance(x, dict)
                and x.get("symbol") == symbol
                and x.get("category") == "USDT-FUTURES"
                and x.get("type") == "perpetual"
                and x.get("status") == "online"
                and x.get("baseCoin") == asset
                and x.get("quoteCoin") == "USDT"
            ]) != 1
        ):
            raise MarketDataError("Bitget active USDT perpetual unavailable")
        self._metadata[symbol] = now

    async def fetch_asset(
        self, asset: str, *, now: datetime | None = None,
    ) -> int:
        if asset not in self.assets:
            raise ValueError("unconfigured Bitget recovery asset")
        clock = _utc(now or datetime.now(UTC))
        position = await asyncio.to_thread(self._position, asset, clock)
        if (
            position["retry_after"]
            and _utc(datetime.fromisoformat(str(position["retry_after"]))) > clock
        ):
            return 0
        cursor = _utc(datetime.fromisoformat(str(position["next_opened_at"])))
        cutoff = clock.replace(
            minute=clock.minute // 5 * 5, second=0, microsecond=0,
        ) - timedelta(hours=4)
        repairing = False
        if cursor >= cutoff:
            gap = await asyncio.to_thread(
                self._claim_due_gap, asset, now=clock, end=cutoff,
            )
            if gap is None:
                pending = await asyncio.to_thread(
                    self._pending_gaps, asset, now=clock,
                )
                await asyncio.to_thread(
                    self._advance, asset, now=clock, next_open=None,
                    status="gaps_pending" if pending else "caught_up",
                )
                return 0
            cursor = gap
            repairing = True
        # A gap retry requests only the audited 5m slot. Even if Bitget
        # includes an adjacent bar, only an exact target can clear the queue.
        until = min(cursor + (1 if repairing else self.page_bars) * _STEP, cutoff)
        symbol = f"{asset}USDT"
        try:
            await self._instrument(asset, symbol, clock)
            # endTime is strictly before the next grid boundary. Bitget
            # documents an extra candle when endTime is just past a boundary.
            result = await self._public(
                "history-candles", {
                    "category": "USDT-FUTURES", "symbol": symbol,
                    "interval": "5m", "type": "market",
                    "startTime": str(int(cursor.timestamp() * 1000) - 1),
                    "endTime": str(int(until.timestamp() * 1000) - 1),
                    "limit": str(2 if repairing else self.page_bars),
                },
            )
            rows = result.get("data") if isinstance(result, dict) else None
            if (
                not isinstance(result, dict) or result.get("code") != "00000"
                or not isinstance(rows, list)
                or len(rows) > (2 if repairing else self.page_bars + 1)
            ):
                raise MarketDataError("invalid Bitget historical page")
            candles = []
            seen: set[int] = set()
            observed = datetime.now(UTC)
            for row in rows:
                if not isinstance(row, list) or len(row) < 7:
                    raise MarketDataError("invalid Bitget history row")
                raw = row[0]
                if (
                    isinstance(raw, bool) or not isinstance(raw, (str, int))
                    or not str(raw).isascii() or not str(raw).isdecimal()
                    or len(str(raw)) != 13
                ):
                    raise MarketDataError("invalid Bitget millisecond timestamp")
                stamp = int(raw)
                if stamp in seen or stamp % 300_000:
                    raise MarketDataError("duplicate or unaligned Bitget candle")
                seen.add(stamp)
                opened = datetime.fromtimestamp(stamp / 1000, UTC)
                # Provider may round down one extra older candle. It cannot
                # silently shift the cursor or supply out-of-window future bars.
                if cursor - _STEP <= opened < cursor:
                    continue
                if not cursor <= opened < until:
                    raise MarketDataError("out-of-window Bitget history row")
                op, high, low, close = (
                    _num(value, positive=True) for value in row[1:5]
                )
                if low > min(op, close) or high < max(op, close):
                    raise MarketDataError("invalid historical Bitget OHLC")
                candles.append(MarketCandle(
                    "bitget", symbol, "USDT", 5, opened,
                    opened + _STEP - timedelta(milliseconds=1),
                    observed, op, high, low, close, _num(row[5]), -1,
                ))
            if not candles and repairing:
                # The source did not return this exact slot. Preserve the
                # six-hour queue deadline; no synthetic candle or cursor move.
                await asyncio.to_thread(
                    self._advance, asset, now=clock, next_open=None,
                    status="gaps_pending",
                )
                _LOG.info(
                    "RivaL Bitget exact-slot repair unavailable asset=%s slot=%s",
                    asset, cursor.isoformat(),
                )
                return 0
            if not candles:
                # Never advance past an empty window: an upstream page
                # with no in-window bars is an API gap, not coverage.
                # Retrying later preserves the cursor (mirrors OKX).
                await asyncio.to_thread(
                    self._advance, asset, now=clock, next_open=None,
                    empty=True, status="upstream_empty",
                    retry_after=clock + timedelta(hours=6),
                )
                _LOG.warning(
                    "RivaL Bitget historical recovery empty page asset=%s",
                    asset,
                )
                return 0
            added = 0
            if candles:
                saved = await asyncio.to_thread(
                    self.market.ingest,
                    MarketBatch((), tuple(candles), 5, observed),
                )
                added = saved.inserted_candles
            if repairing:
                exact = len(candles) == 1 and candles[0].opened_at == cursor
                accepted = (
                    exact and
                    saved.inserted_candles + saved.unchanged_candles == 1
                )
                if accepted:
                    await asyncio.to_thread(self._complete_gap, asset, cursor)
                await asyncio.to_thread(
                    self._advance, asset, now=clock, next_open=None,
                    inserted=added,
                    status="gap_repair" if accepted else "gaps_pending",
                )
                _LOG.info(
                    "RivaL Bitget exact-slot repair asset=%s accepted=%s inserted=%d",
                    asset, accepted, added,
                )
            else:
                next_open = contiguous_forward_next(
                    cursor, until, [candle.opened_at for candle in candles],
                )
                await asyncio.to_thread(
                    self._advance, asset, now=clock, next_open=next_open,
                    inserted=added, status="progress",
                )
                _LOG.info(
                    "RivaL Bitget history asset=%s received=%d inserted=%d",
                    asset, len(rows), added,
                )
            return added
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolate a failing provider
            code = (
                exc.__cause__.response.status_code
                if isinstance(exc, MarketDataError)
                and isinstance(exc.__cause__, httpx.HTTPStatusError)
                else None
            )
            blocked = code in (401, 403, 429, 451)
            await asyncio.to_thread(
                self._advance, asset, now=clock, next_open=None,
                status="blocked" if blocked else "retry",
                retry_after=clock + (
                    timedelta(hours=6) if blocked else timedelta(minutes=20)
                ),
            )
            _LOG.warning(
                "RivaL Bitget historical fetch failed asset=%s status=%s",
                asset, "blocked" if blocked else "retry",
            )
            return 0

    async def run_once(self, *, now: datetime | None = None) -> int:
        """Use a small sequential repair burst without speeding bulk imports.

        The parent's scheduler rotates only two venues per five-minute cycle.
        One gap per Bitget turn would take days to refill 339 isolated gaps.
        Four successful exact-slot repairs per asset are still far below the
        provider's public API budget, and any failure/backoff stops the burst.
        """
        async with self._lock:
            clock = _utc(now or datetime.now(UTC))
            total = 0
            for asset in self.assets:
                for _ in range(_GAP_REPAIR_BURST):
                    added = await self.fetch_asset(asset, now=clock)
                    total += added
                    # Never multiply long-range page acquisition; only a
                    # successfully accepted exact-slot repair earns another
                    # bounded request in this cycle.
                    if not added:
                        break
                    position = await asyncio.to_thread(self._position, asset, clock)
                    if position["status"] != "gap_repair":
                        break
            return total

    def progress(self) -> list[dict]:
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM perp_history_recovery
                   WHERE venue='bitget' ORDER BY asset"""
            ).fetchall()
        return [dict(row) for row in rows]
