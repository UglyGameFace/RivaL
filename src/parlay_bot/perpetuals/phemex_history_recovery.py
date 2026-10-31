"""Restart-safe Phemex USDT perpetual 5m history recovery.

The live collector reads only the latest klines (/kline/last); this recovery
pages the range variant of the same kline family (/kline with from/to) with
the same row layout the collector already parses, one bounded page per asset
per cycle, so it can walk the six-month window. NOTE: the range endpoint is
not used anywhere else in the repo, so it is not proven by the collector.
The window validation below is strict: any row outside [cursor, until) or
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
_PRODUCTS = "https://api.phemex.com/public/products"
_KLINE = "https://api.phemex.com/exchange/public/md/v2/kline/list"


class PhemexHistoryRecovery(GateHistoryRecovery):
    """One bounded Phemex kline page per asset per cycle."""

    def __init__(
        self, market: PerpetualMarketStore, *,
        client: httpx.AsyncClient | None = None,
        assets: tuple[str, ...] = _ALLOWED,
        page_bars: int = 600,
    ) -> None:
        if not 12 <= page_bars <= 600:
            raise ValueError("Phemex historical page must be 12..600 bars")
        super().__init__(market, client=client, assets=assets, page_bars=page_bars)
        self._metadata: dict[str, datetime] = {}

    async def _get(self, url: str, params: dict[str, Any]) -> Any:
        try:
            resp = await self._client.get(
                url, params=params, follow_redirects=False,
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MarketDataError("Phemex historical API unavailable") from exc

    def _position(self, asset: str, now: datetime) -> dict:
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT * FROM perp_history_recovery
                   WHERE venue='phemex' AND asset=?""",
                (asset,),
            ).fetchone()
            if row is None:
                start = six_complete_months_start(now).isoformat()
                conn.execute(
                    """INSERT INTO perp_history_recovery
                       (venue,asset,started_from,next_opened_at)
                       VALUES ('phemex',?,?,?) ON CONFLICT DO NOTHING""",
                    (asset, start, start),
                )
                row = conn.execute(
                    """SELECT * FROM perp_history_recovery
                       WHERE venue='phemex' AND asset=?""",
                    (asset,),
                ).fetchone()
            assert row is not None
            return align_forward_month_cursor(
                conn, venue="phemex", asset=asset, now=now, row=dict(row),
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
                   WHERE venue='phemex' AND asset=?""",
                (
                    next_open.isoformat() if next_open else None,
                    next_open.isoformat() if next_open else None,
                    now.isoformat(), inserted, int(empty), status,
                    retry_after.isoformat() if retry_after else None, asset,
                ),
            )

    async def _product(self, asset: str, symbol: str, now: datetime) -> None:
        checked = self._metadata.get(symbol)
        if checked and timedelta(0) <= now - checked < timedelta(hours=1):
            return
        meta = await self._get(_PRODUCTS, {})
        data = meta.get("data") if isinstance(meta, dict) else None
        products = data.get("perpProductsV2") if isinstance(data, dict) else None
        if (
            not isinstance(meta, dict) or meta.get("code") != 0
            or not isinstance(products, list)
            or len([
                item for item in products if isinstance(item, dict)
                and item.get("symbol") == symbol
                and item.get("type") == "PerpetualV2"
                and item.get("status") == "Listed"
                and item.get("baseCurrency") == asset
                and item.get("quoteCurrency") == "USDT"
                and item.get("settleCurrency") == "USDT"
            ]) != 1
        ):
            raise MarketDataError("Phemex USDT perpetual unavailable")
        self._metadata[symbol] = now

    async def fetch_asset(
        self, asset: str, *, now: datetime | None = None,
    ) -> int:
        if asset not in self.assets:
            raise ValueError("unconfigured Phemex recovery asset")
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
        symbol = f"{asset}USDT"
        try:
            await self._product(asset, symbol, clock)
            response = await self._get(_KLINE, {
                "symbol": symbol,
                "from": int(cursor.timestamp()),
                "to": int(until.timestamp()),
                "resolution": 300,
            })
            body = response.get("data") if isinstance(response, dict) else None
            rows = body.get("rows") if isinstance(body, dict) else None
            if (
                not isinstance(response, dict) or response.get("code") != 0
                or not isinstance(rows, list) or len(rows) > self.page_bars + 1
            ):
                raise MarketDataError("invalid Phemex futures kline response")
            if not rows:
                await asyncio.to_thread(
                    self._advance, asset, now=clock, next_open=None,
                    empty=True, status="upstream_empty",
                    retry_after=clock + timedelta(hours=6),
                )
                _LOG.warning(
                    "RivaL Phemex historical recovery empty page asset=%s",
                    asset,
                )
                return 0
            candles: list[MarketCandle] = []
            observed = datetime.now(UTC)
            previous = -1
            for row in sorted(
                rows,
                key=lambda x: x[0] if isinstance(x, list) and x else -1,
            ):
                if not isinstance(row, list) or len(row) < 9:
                    raise MarketDataError("invalid Phemex candle")
                stamp = row[0]
                if isinstance(stamp, bool) or not isinstance(stamp, int):
                    raise MarketDataError("invalid Phemex candle timestamp")
                if stamp <= previous:
                    raise MarketDataError("duplicate Phemex candle timestamp")
                previous = stamp
                if row[1] != 300:
                    raise MarketDataError("Phemex candle interval mismatch")
                opened = datetime.fromtimestamp(stamp, UTC)
                if opened == until:
                    continue
                if not cursor <= opened < until or stamp % 300:
                    raise MarketDataError("unaligned or out-of-window Phemex candle")
                op = _num(row[3], positive=True)
                high = _num(row[4], positive=True)
                low = _num(row[5], positive=True)
                close = _num(row[6], positive=True)
                if low > min(op, close) or high < max(op, close):
                    raise MarketDataError("invalid Phemex OHLC")
                candles.append(MarketCandle(
                    "phemex", symbol, "USDT", 5, opened,
                    opened + _STEP - timedelta(milliseconds=1),
                    observed, op, high, low, close,
                    _num(row[7]), -1,
                ))
            if not candles:
                raise MarketDataError("no in-window Phemex bars")
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
                "RivaL Phemex historical recovery asset=%s received=%d "
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
            _LOG.warning("RivaL Phemex historical recovery failed asset=%s reason=%s "
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
                   WHERE venue='phemex' ORDER BY asset"""
            ).fetchall()
        return [dict(row) for row in rows]
