"""Restart-safe dYdX Chain v4 Indexer perpetual 5m history recovery.

The live collector fetches recent candles by limit only; this recovery pages
the same candles/perpetualMarkets endpoint backward with the indexer's
documented fromISO/toISO range parameters, one bounded page per asset per
cycle, so it can walk the six-month window. NOTE: fromISO/toISO are not
used anywhere else in the repo, so they are not proven by the collector.
The window validation below is strict: any bar outside [cursor, until) or
unaligned fails the whole page with no ingestion and a retry, so a param
mismatch degrades to loud inaction, never to wrong-window history. The
per-cycle log line (received/inserted/through) settles within one live
cycle whether the range params are honored. Backfilled bars keep their
actual retrieval time as first_observed_at and can never become past
signals.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from parlay_bot.perpetuals.history_inventory import six_complete_months_start
from parlay_bot.perpetuals.history_recovery import (
    align_forward_month_cursor,
    _ALLOWED,
    _STEP,
    GateHistoryRecovery,
    _num,
    _utc,
    contiguous_forward_next,
)
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle, MarketDataError
from parlay_bot.perpetuals.market_store import PerpetualMarketStore

_LOG = logging.getLogger(__name__)
_INDEXER = "https://indexer.dydx.trade/v4"


class DydxHistoryRecovery(GateHistoryRecovery):
    """One bounded dYdX indexer page per asset per cycle."""

    def __init__(
        self, market: PerpetualMarketStore, *,
        client: httpx.AsyncClient | None = None,
        assets: tuple[str, ...] = _ALLOWED,
        page_bars: int = 96,
    ) -> None:
        # The indexer caps a page at 100 rows (collector MAX_BARS 99 + 1).
        if not 12 <= page_bars <= 99:
            raise ValueError("dYdX historical page must be 12..99 bars")
        super().__init__(market, client=client, assets=assets, page_bars=page_bars)
        self._metadata: dict[str, datetime] = {}

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        try:
            resp = await self._client.get(
                f"{_INDEXER}/{path}", params=params, follow_redirects=False,
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MarketDataError("dYdX historical API unavailable") from exc

    def _position(self, asset: str, now: datetime) -> dict:
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT * FROM perp_history_recovery
                   WHERE venue='dydx' AND asset=?""",
                (asset,),
            ).fetchone()
            if row is None:
                start = six_complete_months_start(now).isoformat()
                conn.execute(
                    """INSERT INTO perp_history_recovery
                       (venue,asset,started_from,next_opened_at)
                       VALUES ('dydx',?,?,?) ON CONFLICT DO NOTHING""",
                    (asset, start, start),
                )
                row = conn.execute(
                    """SELECT * FROM perp_history_recovery
                       WHERE venue='dydx' AND asset=?""",
                    (asset,),
                ).fetchone()
        assert row is not None
        return align_forward_month_cursor(
            conn, venue="dydx", asset=asset, now=now, row=dict(row),
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
                   WHERE venue='dydx' AND asset=?""",
                (
                    next_open.isoformat() if next_open else None,
                    next_open.isoformat() if next_open else None,
                    now.isoformat(), inserted, int(empty), status,
                    retry_after.isoformat() if retry_after else None, asset,
                ),
            )

    async def _market(self, ticker: str, now: datetime) -> None:
        checked = self._metadata.get(ticker)
        if checked and timedelta(0) <= now - checked < timedelta(hours=1):
            return
        markets_response = await self._get("perpetualMarkets", {})
        if (
            not isinstance(markets_response, dict)
            or not isinstance(markets_response.get("markets"), dict)
        ):
            raise MarketDataError("invalid dYdX perpetual markets response")
        market = markets_response["markets"].get(ticker)
        if (
            not isinstance(market, dict)
            or market.get("ticker") != ticker
            or market.get("status") != "ACTIVE"
        ):
            raise MarketDataError(f"active dYdX perpetual unavailable: {ticker}")
        self._metadata[ticker] = now

    async def fetch_asset(
        self, asset: str, *, now: datetime | None = None,
    ) -> int:
        if asset not in self.assets:
            raise ValueError("unconfigured dYdX recovery asset")
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
        if cursor >= cutoff:
            await asyncio.to_thread(
                self._advance, asset, now=clock, next_open=None,
                status="caught_up",
            )
            return 0
        until = min(cursor + self.page_bars * _STEP, cutoff)
        ticker = f"{asset}-USD"
        try:
            await self._market(ticker, clock)
            result = await self._get(
                f"candles/perpetualMarkets/{ticker}",
                {
                    "resolution": "5MINS",
                    "limit": self.page_bars + 1,
                    "fromISO": cursor.isoformat(),
                    "toISO": until.isoformat(),
                },
            )
            rows = result.get("candles") if isinstance(result, dict) else None
            if not isinstance(rows, list) or len(rows) > self.page_bars + 1:
                raise MarketDataError("invalid dYdX candle response")
            if not rows:
                await asyncio.to_thread(
                    self._advance, asset, now=clock, next_open=None,
                    empty=True, status="upstream_empty",
                    retry_after=clock + timedelta(hours=6),
                )
                _LOG.warning(
                    "RivaL dYdX historical recovery empty page asset=%s",
                    asset,
                )
                return 0
            candles: list[MarketCandle] = []
            observed = datetime.now(UTC)
            previous: datetime | None = None
            for row in sorted(
                rows,
                key=lambda x: x.get("startedAt", "") if isinstance(x, dict) else "",
            ):
                if not isinstance(row, dict):
                    raise MarketDataError("invalid dYdX candle")
                returned = row.get("ticker", row.get("market"))
                if returned != ticker or row.get("resolution") != "5MINS":
                    raise MarketDataError("mismatched dYdX candle contract or interval")
                raw_started = row.get("startedAt")
                if not isinstance(raw_started, str):
                    raise MarketDataError("invalid dYdX candle timestamp")
                try:
                    opened = datetime.fromisoformat(raw_started)
                except ValueError as exc:
                    raise MarketDataError("invalid dYdX candle timestamp") from exc
                if opened.tzinfo is None:
                    raise MarketDataError("naive dYdX candle timestamp")
                opened = opened.astimezone(UTC)
                if previous is not None and opened <= previous:
                    raise MarketDataError("duplicate/out-of-order dYdX candle")
                previous = opened
                if opened == until:
                    continue
                if (
                    not cursor <= opened < until
                    or int(opened.timestamp()) % 300
                ):
                    raise MarketDataError("unaligned or out-of-window dYdX candle")
                op = _num(row.get("open"), positive=True)
                high = _num(row.get("high"), positive=True)
                low = _num(row.get("low"), positive=True)
                close = _num(row.get("close"), positive=True)
                if low > min(op, close) or high < max(op, close):
                    raise MarketDataError("inconsistent dYdX OHLC")
                volume = _num(row.get("baseTokenVolume"))
                if volume < 0:
                    raise MarketDataError("negative dYdX base volume")
                candles.append(MarketCandle(
                    "dydx", ticker, "USD", 5, opened,
                    opened + _STEP - timedelta(milliseconds=1),
                    observed, op, high, low, close, volume, -1,
                ))
            if not candles:
                raise MarketDataError("no in-window dYdX bars")
            batch = MarketBatch((), tuple(candles), 5, observed)
            ingested = await asyncio.to_thread(self.market.ingest, batch)
            added = ingested.inserted_candles
            next_open = contiguous_forward_next(
                cursor, until, [candle.opened_at for candle in candles],
            )
            await asyncio.to_thread(
                self._advance, asset, now=clock, next_open=next_open,
                inserted=added, status="progress",
            )
            _LOG.info(
                "RivaL dYdX historical recovery asset=%s received=%d "
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
            _LOG.warning("RivaL dYdX historical recovery failed asset=%s reason=%s "
                         "error=%s",
                         asset, reason, type(exc).__name__)
            return 0

    async def run_once(self, *, now: datetime | None = None) -> int:
        async with self._lock:
            clock = _utc(now or datetime.now(UTC))
            total = 0
            for asset in self.assets:
                total += await self.fetch_asset(asset, now=clock)
            return total

    def progress(self) -> list[dict]:
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM perp_history_recovery
                   WHERE venue='dydx' ORDER BY asset"""
            ).fetchall()
        return [dict(row) for row in rows]
