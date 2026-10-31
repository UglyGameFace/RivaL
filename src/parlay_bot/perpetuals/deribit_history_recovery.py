"""Restart-safe Deribit USDC linear perpetual 5m history recovery.

Uses the same public endpoints the live collector already calls:
get_instrument for the active-perp check and get_tradingview_chart_data
with a true start/end range, so one bounded page per asset per cycle can
walk the six-month window. Backfilled bars keep their actual retrieval
time as first_observed_at and can never become past signals.
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
_BASE = "https://www.deribit.com/api/v2/public/"


class DeribitHistoryRecovery(GateHistoryRecovery):
    """One bounded Deribit chart page per asset per cycle."""

    def __init__(
        self, market: PerpetualMarketStore, *,
        client: httpx.AsyncClient | None = None,
        assets: tuple[str, ...] = _ALLOWED,
        page_bars: int = 600,
    ) -> None:
        if not 12 <= page_bars <= 600:
            raise ValueError("Deribit historical page must be 12..600 bars")
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
            raise MarketDataError("Deribit historical API unavailable") from exc

    def _position(self, asset: str, now: datetime) -> dict:
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT * FROM perp_history_recovery
                   WHERE venue='deribit' AND asset=?""",
                (asset,),
            ).fetchone()
            if row is None:
                start = six_complete_months_start(now).isoformat()
                conn.execute(
                    """INSERT INTO perp_history_recovery
                       (venue,asset,started_from,next_opened_at)
                       VALUES ('deribit',?,?,?) ON CONFLICT DO NOTHING""",
                    (asset, start, start),
                )
                row = conn.execute(
                    """SELECT * FROM perp_history_recovery
                       WHERE venue='deribit' AND asset=?""",
                    (asset,),
                ).fetchone()
            assert row is not None
            return align_forward_month_cursor(
                conn, venue="deribit", asset=asset, now=now, row=dict(row),
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
                   WHERE venue='deribit' AND asset=?""",
                (
                    next_open.isoformat() if next_open else None,
                    next_open.isoformat() if next_open else None,
                    now.isoformat(), inserted, int(empty), status,
                    retry_after.isoformat() if retry_after else None, asset,
                ),
            )

    async def _instrument(self, asset: str, symbol: str, now: datetime) -> None:
        checked = self._metadata.get(symbol)
        if checked and timedelta(0) <= now - checked < timedelta(hours=1):
            return
        metadata = await self._public("get_instrument", {"instrument_name": symbol})
        instrument = metadata.get("result") if isinstance(metadata, dict) else None
        if (
            not isinstance(instrument, dict)
            or instrument.get("instrument_name") != symbol
            or instrument.get("kind") != "future"
            or instrument.get("settlement_period") != "perpetual"
            or instrument.get("instrument_type") != "linear"
            or instrument.get("counter_currency") != "USDC"
            or instrument.get("settlement_currency") != "USDC"
            or instrument.get("is_active") is not True
            or instrument.get("state", "open") != "open"
        ):
            raise MarketDataError("Deribit active linear USDC perp unavailable")
        self._metadata[symbol] = now

    async def fetch_asset(
        self, asset: str, *, now: datetime | None = None,
    ) -> int:
        if asset not in self.assets:
            raise ValueError("unconfigured Deribit recovery asset")
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
        symbol = f"{asset}_USDC-PERPETUAL"
        try:
            await self._instrument(asset, symbol, clock)
            data = await self._public("get_tradingview_chart_data", {
                "instrument_name": symbol,
                "start_timestamp": int(cursor.timestamp() * 1000),
                "end_timestamp": int(until.timestamp() * 1000) - 1,
                "resolution": "5",
            })
            result = data.get("result") if isinstance(data, dict) else None
            if not isinstance(result, dict) or result.get("status") != "ok":
                raise MarketDataError("Deribit chart unavailable")
            names = ("ticks", "open", "high", "low", "close", "volume")
            series = [result.get(name) for name in names]
            if (
                not all(isinstance(values, list) for values in series)
                or not 0 <= len(series[0]) <= self.page_bars + 1
                or any(len(values) != len(series[0]) for values in series)
            ):
                raise MarketDataError("Deribit chart arrays missing or misaligned")
            ticks, opens, highs, lows, closes, volumes = series
            if not ticks:
                await asyncio.to_thread(
                    self._advance, asset, now=clock, next_open=None,
                    empty=True, status="upstream_empty",
                    retry_after=clock + timedelta(hours=6),
                )
                _LOG.warning(
                    "RivaL Deribit historical recovery empty page asset=%s",
                    asset,
                )
                return 0
            candles: list[MarketCandle] = []
            observed = datetime.now(UTC)
            previous = -1
            for i, raw in enumerate(ticks):
                if isinstance(raw, bool) or not isinstance(raw, int) or raw < 10**12:
                    raise MarketDataError("invalid Deribit chart timestamp")
                if raw <= previous:
                    raise MarketDataError("duplicate/out-of-order Deribit bar")
                previous = raw
                opened = datetime.fromtimestamp(raw / 1000, UTC)
                if opened == until:
                    continue
                if not cursor <= opened < until or raw % 300_000:
                    raise MarketDataError("unaligned or out-of-window Deribit candle")
                op = _num(opens[i], positive=True)
                high = _num(highs[i], positive=True)
                low = _num(lows[i], positive=True)
                close = _num(closes[i], positive=True)
                if low > min(op, close) or high < max(op, close):
                    raise MarketDataError("invalid Deribit OHLC")
                candles.append(MarketCandle(
                    "deribit", symbol, "USDC", 5, opened,
                    opened + _STEP - timedelta(milliseconds=1),
                    observed, op, high, low, close,
                    _num(volumes[i]), -1,
                ))
            if not candles:
                raise MarketDataError("no in-window Deribit bars")
            batch = MarketBatch((), tuple(candles), 5, observed)
            result = await asyncio.to_thread(self.market.ingest, batch)
            added = result.inserted_candles
            next_open = contiguous_forward_next(
                cursor, until, [candle.opened_at for candle in candles],
            )
            await asyncio.to_thread(
                self._advance, asset, now=clock, next_open=next_open,
                inserted=added, status="progress",
            )
            _LOG.info(
                "RivaL Deribit historical recovery asset=%s received=%d "
                "inserted=%d through=%s",
                asset, len(ticks), added, next_open.date(),
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
            _LOG.warning("RivaL Deribit historical recovery failed asset=%s reason=%s "
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
                   WHERE venue='deribit' ORDER BY asset"""
            ).fetchall()
        return [dict(row) for row in rows]
