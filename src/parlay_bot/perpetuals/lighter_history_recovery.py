"""Restart-safe Lighter USD perpetual 5m history recovery.

Uses the same public endpoints the live collector already calls: orderBooks
to resolve each asset's perp market_id, then candles with a true
start/end range, so one bounded page per asset per cycle can walk the
six-month window. Backfilled bars keep their actual retrieval time as
first_observed_at and can never become past signals.
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
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle, MarketDataError
from parlay_bot.perpetuals.market_store import PerpetualMarketStore

_LOG = logging.getLogger(__name__)
_ORDER_BOOKS = "https://mainnet.zklighter.elliot.ai/api/v1/orderBooks"
_CANDLES = "https://mainnet.zklighter.elliot.ai/api/v1/candles"


class LighterHistoryRecovery(GateHistoryRecovery):
    """One bounded Lighter candle page per asset per cycle."""

    def __init__(
        self, market: PerpetualMarketStore, *,
        client: httpx.AsyncClient | None = None,
        assets: tuple[str, ...] = _ALLOWED,
        page_bars: int = 600,
    ) -> None:
        if not 12 <= page_bars <= 600:
            raise ValueError("Lighter historical page must be 12..600 bars")
        super().__init__(market, client=client, assets=assets, page_bars=page_bars)
        self._market_ids: dict[str, int] = {}
        self._market_ids_checked: datetime | None = None

    async def _get(self, url: str, params: dict[str, Any]) -> Any:
        try:
            resp = await self._client.get(
                url, params=params, follow_redirects=False,
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MarketDataError("Lighter historical API unavailable") from exc

    def _position(self, asset: str, now: datetime) -> dict:
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT * FROM perp_history_recovery
                   WHERE venue='lighter' AND asset=?""",
                (asset,),
            ).fetchone()
            if row is None:
                start = six_complete_months_start(now).isoformat()
                conn.execute(
                    """INSERT INTO perp_history_recovery
                       (venue,asset,started_from,next_opened_at)
                       VALUES ('lighter',?,?,?) ON CONFLICT DO NOTHING""",
                    (asset, start, start),
                )
                row = conn.execute(
                    """SELECT * FROM perp_history_recovery
                       WHERE venue='lighter' AND asset=?""",
                    (asset,),
                ).fetchone()
            assert row is not None
            return align_forward_month_cursor(
                conn, venue="lighter", asset=asset, now=now, row=dict(row),
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
                   WHERE venue='lighter' AND asset=?""",
                (
                    next_open.isoformat() if next_open else None,
                    next_open.isoformat() if next_open else None,
                    now.isoformat(), inserted, int(empty), status,
                    retry_after.isoformat() if retry_after else None, asset,
                ),
            )

    async def _market_id(self, asset: str, now: datetime) -> int:
        if (
            asset in self._market_ids and self._market_ids_checked is not None
            and timedelta(0) <= now - self._market_ids_checked < timedelta(hours=1)
        ):
            return self._market_ids[asset]
        meta = await self._get(_ORDER_BOOKS, {})
        entries = meta.get("order_books") if isinstance(meta, dict) else None
        if (
            not isinstance(meta, dict) or meta.get("code") != 200
            or not isinstance(entries, list)
        ):
            raise MarketDataError("Lighter public perp markets unavailable")
        found: dict[str, int] = {}
        for entry in entries:
            if (
                isinstance(entry, dict)
                and entry.get("market_type") == "perp"
                and entry.get("status") == "active"
                and isinstance(entry.get("symbol"), str)
                and isinstance(entry.get("market_id"), int)
                and not isinstance(entry.get("market_id"), bool)
            ):
                found[entry["symbol"]] = entry["market_id"]
        if asset not in found:
            raise MarketDataError(f"Lighter active perp missing: {asset}")
        self._market_ids = found
        self._market_ids_checked = now
        return found[asset]

    async def fetch_asset(
        self, asset: str, *, now: datetime | None = None,
    ) -> int:
        if asset not in self.assets:
            raise ValueError("unconfigured Lighter recovery asset")
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
        try:
            market_id = await self._market_id(asset, clock)
            result = await self._get(_CANDLES, {
                "market_id": market_id, "resolution": "5m",
                "start_timestamp": int(cursor.timestamp()),
                "end_timestamp": int(until.timestamp()),
                "count_back": self.page_bars,
            })
            rows = result.get("c") if isinstance(result, dict) else None
            if (
                not isinstance(result, dict) or result.get("code") != 200
                or result.get("r") != "5m"
                or not isinstance(rows, list) or len(rows) > self.page_bars + 1
            ):
                raise MarketDataError("invalid Lighter candle response")
            if not rows:
                await asyncio.to_thread(
                    self._advance, asset, now=clock, next_open=None,
                    empty=True, status="upstream_empty",
                    retry_after=clock + timedelta(hours=6),
                )
                _LOG.warning(
                    "RivaL Lighter historical recovery empty page asset=%s",
                    asset,
                )
                return 0
            candles: list[MarketCandle] = []
            observed = datetime.now(UTC)
            previous = -1
            for row in sorted(
                rows,
                key=lambda x: x.get("t", -1) if isinstance(x, dict) else -1,
            ):
                if not isinstance(row, dict):
                    raise MarketDataError("invalid Lighter row")
                raw = row.get("t")
                if isinstance(raw, bool) or not isinstance(raw, int):
                    raise MarketDataError("invalid Lighter timestamp")
                if raw <= previous:
                    raise MarketDataError("duplicate Lighter candle")
                previous = raw
                opened = datetime.fromtimestamp(raw / 1000, UTC)
                if opened == until:
                    continue
                if not cursor <= opened < until or raw % 300_000:
                    raise MarketDataError("unaligned or out-of-window Lighter candle")
                op = _num(row.get("o"), positive=True)
                high = _num(row.get("h"), positive=True)
                low = _num(row.get("l"), positive=True)
                close = _num(row.get("c"), positive=True)
                if low > min(op, close) or high < max(op, close):
                    raise MarketDataError("invalid Lighter OHLC")
                candles.append(MarketCandle(
                    "lighter", asset, "USD", 5, opened,
                    opened + _STEP - timedelta(milliseconds=1),
                    observed, op, high, low, close,
                    _num(row.get("v")), -1,
                ))
            if not candles:
                raise MarketDataError("no in-window Lighter bars")
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
                "RivaL Lighter historical recovery asset=%s received=%d "
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
            _LOG.warning("RivaL Lighter historical recovery failed asset=%s reason=%s "
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
                   WHERE venue='lighter' ORDER BY asset"""
            ).fetchall()
        return [dict(row) for row in rows]
