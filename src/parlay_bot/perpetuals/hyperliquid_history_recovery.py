"""Bounded, restart-safe Hyperliquid perpetual history recovery.

Seeds its cursor six months back and pages forward 600 bars per coin per
cycle, exactly like the Gate recovery, so the six-month auditor can
eventually certify the Hyperliquid streams — the primary signal venue,
which previously had no backfill path at all and only grew forward from
first collection. Source history is tagged with its ACTUAL retrieval
time. Backfilled bars are never reclassified as contemporaneous trading
predictions.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from parlay_bot.perpetuals.history_inventory import six_complete_months_start
from parlay_bot.perpetuals.history_recovery import (
    _ALLOWED,
    _STEP,
    GateHistoryRecovery,
    _num,
    _utc,
    align_forward_month_cursor,
    contiguous_forward_next,
)
from parlay_bot.perpetuals.hyperliquid import (
    MarketBatch,
    MarketCandle,
    MarketDataError,
)
from parlay_bot.perpetuals.market_store import PerpetualMarketStore

_LOG = logging.getLogger(__name__)
_INFO_URL = "https://api.hyperliquid.xyz/info"


class HyperliquidHistoryRecovery(GateHistoryRecovery):
    def __init__(
        self, market: PerpetualMarketStore, *,
        client: httpx.AsyncClient | None = None,
        assets: tuple[str, ...] = _ALLOWED,
        page_bars: int = 600,
    ) -> None:
        if not 12 <= page_bars <= 600:
            raise ValueError("invalid Hyperliquid history recovery configuration")
        super().__init__(market, client=client, assets=assets, page_bars=page_bars)
        self._retention_ready = False

    def initialize(self) -> None:
        super().initialize()
        if self._retention_ready:
            return
        with self.store.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS perp_history_api_retention (
                    venue TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    interval_minutes INTEGER NOT NULL,
                    requested_from TEXT NOT NULL,
                    resumed_at TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    PRIMARY KEY (venue,asset,interval_minutes)
                );
            """)
        self._retention_ready = True

    def _record_retention_limit(
        self, asset: str, *, requested_from: datetime,
        resumed_at: datetime, now: datetime,
    ) -> None:
        # An API cap is not recovered history; retain the missing period as
        # a durable, explicit gap rather than claiming six complete months.
        with self.store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_history_api_retention
                   (venue,asset,interval_minutes,requested_from,resumed_at,
                    observed_at,reason)
                   VALUES ('hyperliquid',?,5,?,?,?,'candleSnapshot_latest_5000')
                   ON CONFLICT(venue,asset,interval_minutes) DO UPDATE SET
                     requested_from=excluded.requested_from,
                     resumed_at=excluded.resumed_at,
                     observed_at=excluded.observed_at,
                     reason=excluded.reason""",
                (asset, requested_from.isoformat(), resumed_at.isoformat(),
                 now.isoformat()),
            )

    def progress(self) -> list[dict]:
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT r.venue,r.asset,r.started_from,r.next_opened_at,
                          r.attempted_at,r.inserted_bars,r.empty_windows,
                          r.status,r.retry_after,
                          x.requested_from AS retention_requested_from,
                          x.resumed_at AS retention_resumed_at,
                          x.reason AS retention_reason
                   FROM perp_history_recovery r
                   LEFT JOIN perp_history_api_retention x
                     ON x.venue=r.venue AND x.asset=r.asset
                    AND x.interval_minutes=5
                   WHERE r.venue='hyperliquid' ORDER BY r.asset"""
            ).fetchall()
        return [dict(x) for x in rows]

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
                   WHERE venue='hyperliquid' AND asset=?""",
                (
                    next_open.isoformat() if next_open else None,
                    next_open.isoformat() if next_open else None,
                    now.isoformat(), inserted, int(empty), status,
                    retry_after.isoformat() if retry_after else None, asset,
                ),
            )

    async def _info(self, payload: dict[str, Any]) -> Any:
        try:
            response = await self._client.post(
                _INFO_URL, json=payload,
                headers={"Content-Type": "application/json"},
                follow_redirects=False,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MarketDataError("Hyperliquid historical read unavailable") from exc

    def _position(self, asset: str, now: datetime) -> dict:
        self.initialize()
        with self.store.connect() as conn:
            existing = conn.execute(
                """SELECT * FROM perp_history_recovery
                   WHERE venue='hyperliquid' AND asset=?""",
                (asset,),
            ).fetchone()
            if existing is not None:
                return align_forward_month_cursor(
                    conn, venue="hyperliquid", asset=asset, now=now,
                    row=dict(existing),
                )
            initial = six_complete_months_start(now).isoformat()
            conn.execute(
                """INSERT INTO perp_history_recovery
                   (venue,asset,started_from,next_opened_at)
                   VALUES ('hyperliquid',?,?,?)
                   ON CONFLICT(venue,asset) DO NOTHING""",
                (asset, initial, initial),
            )
            row = conn.execute(
                """SELECT * FROM perp_history_recovery
                   WHERE venue='hyperliquid' AND asset=?""",
                (asset,),
            ).fetchone()
        return dict(row)

    async def fetch_asset(
        self, asset: str, *, now: datetime | None = None,
    ) -> int:
        if asset not in self.assets:
            raise ValueError("invalid Hyperliquid recovery asset")
        clock = _utc(now or datetime.now(UTC))
        position = await asyncio.to_thread(self._position, asset, clock)
        if position["retry_after"] and _utc(datetime.fromisoformat(
            str(position["retry_after"])
        )) > clock:
            return 0
        cursor = _utc(datetime.fromisoformat(str(position["next_opened_at"])))
        end = clock.replace(minute=clock.minute // 5 * 5,
                            second=0, microsecond=0) - timedelta(hours=4)
        if cursor >= end:
            await asyncio.to_thread(
                self._advance, asset, now=clock, next_open=None, status="caught_up",
            )
            return 0
        until = min(cursor + self.page_bars * _STEP, end)
        try:
            rows = await self._info({
                "type": "candleSnapshot",
                "req": {
                    "coin": asset, "interval": "5m",
                    "startTime": int(cursor.timestamp() * 1000),
                    "endTime": int(until.timestamp() * 1000),
                },
            })
            if not isinstance(rows, list) or len(rows) > self.page_bars + 1:
                raise MarketDataError("oversized or invalid Hyperliquid page")
            candles: list[MarketCandle] = []
            seen: set[int] = set()
            observed = datetime.now(UTC)
            for row in rows:
                if not isinstance(row, dict):
                    raise MarketDataError("invalid historical Hyperliquid bar")
                if row.get("s") != asset or row.get("i") != "5m":
                    raise MarketDataError("wrong Hyperliquid candle contract")
                try:
                    stamp = int(row["t"])
                except (TypeError, ValueError) as exc:
                    raise MarketDataError(
                        "invalid historical Hyperliquid timestamp") from exc
                opened = datetime.fromtimestamp(stamp / 1000, UTC)
                # The boundary bar belongs to the next window; a duplicate
                # is an API quirk. Skipping either cannot stall the cursor,
                # and the contiguous advance re-fetches anything skipped.
                if stamp in seen or not cursor <= opened < until:
                    continue
                if stamp % 300_000:
                    raise MarketDataError(
                        "unaligned Hyperliquid historical candle")
                seen.add(stamp)
                op, high, low, close = (
                    _num(row.get(key), positive=True)
                    for key in ("o", "h", "l", "c")
                )
                if low > min(op, close) or high < max(op, close):
                    raise MarketDataError("invalid historical Hyperliquid OHLC")
                try:
                    volume = float(row["v"])
                    trades = int(row["n"])
                except (TypeError, ValueError) as exc:
                    raise MarketDataError(
                        "invalid historical Hyperliquid volume") from exc
                if volume < 0 or trades < 0:
                    raise MarketDataError(
                        "invalid historical Hyperliquid volume")
                candles.append(MarketCandle(
                    "hyperliquid", asset, "USDC", 5, opened,
                    opened + _STEP - timedelta(milliseconds=1),
                    observed, op, high, low, close, volume, trades,
                ))
            if not candles:
                # Hyperliquid documents only the newest 5,000 candles per
                # interval. A six-month 5m cursor predates that API horizon;
                # blindly retrying it every six hours never makes progress.
                # Probe a conservative recent window first. Advance ONLY if
                # we observed actual source bars there; keep the older gap
                # explicit and never count it as verified historical data.
                ancient = until <= clock - 5000 * _STEP
                if ancient:
                    recoverable = (
                        clock.replace(minute=clock.minute // 5 * 5,
                                      second=0, microsecond=0)
                        - 4500 * _STEP
                    )
                    probe_until = min(recoverable + self.page_bars * _STEP, end)
                    if probe_until > recoverable:
                        recent = await self._info({
                            "type": "candleSnapshot",
                            "req": {
                                "coin": asset, "interval": "5m",
                                "startTime": int(recoverable.timestamp() * 1000),
                                "endTime": int(probe_until.timestamp() * 1000),
                            },
                        })
                        verified = isinstance(recent, list) and any(
                            isinstance(bar, dict)
                            and bar.get("s") == asset and bar.get("i") == "5m"
                            and isinstance(bar.get("t"), int)
                            and int(recoverable.timestamp() * 1000) <= bar["t"]
                            < int(probe_until.timestamp() * 1000)
                            and bar["t"] % 300_000 == 0
                            for bar in recent
                        )
                        if verified:
                            await asyncio.to_thread(
                                self._record_retention_limit, asset,
                                requested_from=cursor, resumed_at=recoverable,
                                now=clock,
                            )
                            await asyncio.to_thread(
                                self._advance, asset, now=clock,
                                next_open=recoverable, empty=True,
                                status="api_retention_limited",
                            )
                            _LOG.warning(
                                "RivaL Hyperliquid historical source API limit "
                                "asset=%s older 5m candles unavailable via "
                                "candleSnapshot; resume from verified window %s",
                                asset, recoverable.isoformat(),
                            )
                            return 0
                # An empty page inside the supported window, or a failed
                # recent probe, is not proof of retention loss.
                await asyncio.to_thread(
                    self._advance, asset, now=clock, next_open=None,
                    empty=True, status="upstream_empty",
                    retry_after=clock + timedelta(hours=6),
                )
                _LOG.warning(
                    "RivaL Hyperliquid historical recovery empty page asset=%s",
                    asset,
                )
                return 0
            added = 0
            if candles:
                batch = MarketBatch((), tuple(candles), 5, observed)
                result = await asyncio.to_thread(self.market.ingest, batch)
                added = result.inserted_candles
            next_open = contiguous_forward_next(
                cursor, until, [candle.opened_at for candle in candles],
            )
            await asyncio.to_thread(
                self._advance, asset, now=clock, next_open=next_open,
                inserted=added, empty=not bool(rows),
                status="progress",
            )
            _LOG.info(
                "RivaL Hyperliquid history asset=%s received=%d "
                "inserted=%d through=%s",
                asset, len(rows), added, next_open.date(),
            )
            return added
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            reason = (
                "blocked" if isinstance(exc, MarketDataError)
                and isinstance(exc.__cause__, httpx.HTTPStatusError)
                and exc.__cause__.response.status_code in (401, 403, 451)
                else "retry"
            )
            pause = timedelta(hours=6) if reason == "blocked" else timedelta(minutes=20)
            await asyncio.to_thread(
                self._advance, asset, now=clock, next_open=None,
                status=reason, retry_after=clock + pause,
            )
            _LOG.warning(
                "RivaL Hyperliquid historical recovery failed asset=%s reason=%s",
                asset, reason)
            return 0
