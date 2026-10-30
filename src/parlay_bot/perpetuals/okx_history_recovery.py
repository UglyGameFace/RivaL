"""Six-calendar-month OKX SWAP 5m historical repair, read only.

OKX history-candles pages BACKWARDS using `after` in milliseconds.
The cursor is an exclusive upper bound and is persisted per asset. Retrieved
history retains its actual first-observed time, never becomes a past forecast.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from parlay_bot.perpetuals.history_inventory import six_months_prior
from parlay_bot.perpetuals.history_recovery import (
    _ALLOWED,
    _STEP,
    GateHistoryRecovery,
    _num,
    _utc,
    contiguous_backward_next,
)
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle, MarketDataError
from parlay_bot.perpetuals.market_store import PerpetualMarketStore

_LOG = logging.getLogger(__name__)
_URL = "https://www.okx.com/api/v5/"
_META_TTL = timedelta(hours=1)


class OkxHistoryRecovery(GateHistoryRecovery):
    """One bounded backwards page per asset per five-minute history cycle."""

    def __init__(
        self, market: PerpetualMarketStore, *,
        client: httpx.AsyncClient | None = None,
        assets: tuple[str, ...] = _ALLOWED,
        page_bars: int = 100,
    ) -> None:
        if not 12 <= page_bars <= 100:
            raise ValueError("OKX history page must be between 12 and 100")
        super().__init__(market, client=client, assets=assets, page_bars=page_bars)
        self._metadata: dict[str, tuple[float, datetime]] = {}

    async def _public(self, path: str, params: dict[str, Any]) -> Any:
        try:
            response = await self._client.get(
                _URL + path, params=params, follow_redirects=False,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MarketDataError("OKX history public request unavailable") from exc

    def _position(self, asset: str, now: datetime) -> dict:
        self.initialize()
        with self.store.connect() as conn:
            existing = conn.execute(
                """SELECT * FROM perp_history_recovery
                   WHERE venue='okx' AND asset=?""",
                (asset,),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            cutoff = now.replace(
                minute=now.minute // 5 * 5, second=0, microsecond=0,
            ) - timedelta(hours=4)
            # The descending cursor is the first bar NOT included on the next
            # page. Endpoint's 'after' means strictly older than this instant.
            anchor = cutoff.isoformat()
            conn.execute(
                """INSERT INTO perp_history_recovery
                   (venue,asset,started_from,next_opened_at)
                   VALUES ('okx',?,?,?) ON CONFLICT DO NOTHING""",
                (asset, anchor, anchor),
            )
            row = conn.execute(
                """SELECT * FROM perp_history_recovery
                   WHERE venue='okx' AND asset=?""",
                (asset,),
            ).fetchone()
        assert row is not None
        return dict(row)

    def _advance(
        self, asset: str, *, now: datetime, next_open: datetime | None,
        inserted: int = 0, empty: bool = False,
        status: str = "progress", retry_after: datetime | None = None,
    ) -> None:
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE perp_history_recovery SET
                   next_opened_at=CASE
                     WHEN CAST(? AS TEXT) IS NOT NULL
                          AND CAST(? AS TEXT) < next_opened_at
                     THEN CAST(? AS TEXT)
                     ELSE next_opened_at END,
                   attempted_at=?,inserted_bars=inserted_bars+?,
                   empty_windows=empty_windows+?,status=?,retry_after=?
                   WHERE venue='okx' AND asset=?""",
                (
                    next_open.isoformat() if next_open else None,
                    next_open.isoformat() if next_open else None,
                    next_open.isoformat() if next_open else None,
                    now.isoformat(), inserted, int(empty), status,
                    retry_after.isoformat() if retry_after else None, asset,
                ),
            )

    async def _multiplier(self, asset: str, contract: str, now: datetime) -> float:
        cached = self._metadata.get(contract)
        if cached and now - cached[1] < _META_TTL:
            return cached[0]
        raw = await self._public(
            "public/instruments", {"instType": "SWAP", "instId": contract},
        )
        records = raw.get("data") if isinstance(raw, dict) else None
        if (
            not isinstance(raw, dict) or raw.get("code") != "0"
            or not isinstance(records, list) or len(records) != 1
            or not isinstance(records[0], dict)
        ):
            raise MarketDataError("OKX perp instrument details unavailable")
        inst = records[0]
        if (
            inst.get("instId") != contract or inst.get("instType") != "SWAP"
            or inst.get("state") != "live" or inst.get("ctType") != "linear"
            or inst.get("settleCcy") != "USDT"
            or inst.get("ctValCcy") != asset
        ):
            raise MarketDataError("OKX historical instrument is not an active linear swap")
        factor = _num(inst.get("ctVal"), positive=True) * _num(
            inst.get("ctMult"), positive=True,
        )
        if cached is not None and factor != cached[0]:
            self._metadata.pop(contract, None)
            raise MarketDataError("OKX contract size changed; manual audit required")
        self._metadata[contract] = (factor, now)
        return factor

    async def fetch_asset(
        self, asset: str, *, now: datetime | None = None,
    ) -> int:
        if asset not in self.assets:
            raise ValueError("unconfigured OKX recovery asset")
        clock = _utc(now or datetime.now(UTC))
        position = await asyncio.to_thread(self._position, asset, clock)
        if (
            position["retry_after"] and
            _utc(datetime.fromisoformat(str(position["retry_after"]))) > clock
        ):
            return 0
        cursor = _utc(datetime.fromisoformat(str(position["next_opened_at"])))
        start = six_months_prior(clock).replace(
            minute=six_months_prior(clock).minute // 5 * 5,
            second=0, microsecond=0,
        )
        if cursor <= start:
            await asyncio.to_thread(
                self._advance, asset, now=clock, next_open=None,
                status="caught_up",
            )
            return 0
        symbol = f"{asset}-USDT-SWAP"
        try:
            multiplier = await self._multiplier(asset, symbol, clock)
            data = await self._public(
                "market/history-candles", {
                    "instId": symbol, "bar": "5m",
                    "after": str(int(cursor.timestamp() * 1000)),
                    "limit": str(self.page_bars),
                },
            )
            rows = data.get("data") if isinstance(data, dict) else None
            if (
                not isinstance(data, dict) or data.get("code") != "0"
                or not isinstance(rows, list) or len(rows) > self.page_bars
            ):
                raise MarketDataError("invalid OKX historical page")
            candles: list[MarketCandle] = []
            seen: set[int] = set()
            oldest: datetime | None = None
            observed = datetime.now(UTC)
            for row in rows:
                if not isinstance(row, list) or len(row) < 9:
                    raise MarketDataError("invalid OKX historical bar")
                raw = row[0]
                if (
                    isinstance(raw, bool) or not isinstance(raw, (int, str))
                    or not str(raw).isascii() or not str(raw).isdecimal()
                    or len(str(raw)) != 13
                ):
                    raise MarketDataError("invalid OKX historical timestamp")
                stamp = int(raw)
                opened = datetime.fromtimestamp(stamp / 1000, UTC)
                if (
                    stamp in seen or stamp % 300_000 or opened >= cursor
                    or opened > clock
                ):
                    raise MarketDataError("duplicate or forward OKX historical candle")
                seen.add(stamp)
                oldest = min(oldest, opened) if oldest else opened
                if opened < start:
                    continue
                if row[8] != "1":
                    raise MarketDataError("unconfirmed OKX historical candle")
                op, high, low, close = (
                    _num(x, positive=True) for x in row[1:5]
                )
                if low > min(op, close) or high < max(op, close):
                    raise MarketDataError("inconsistent OKX historical OHLC")
                candles.append(MarketCandle(
                    "okx", symbol, "USDT", 5, opened,
                    opened + _STEP - timedelta(milliseconds=1),
                    observed, op, high, low, close,
                    _num(row[5]) * multiplier, -1,
                ))
            if oldest is None:
                # Never move cursor on an empty or unsupported history page.
                await asyncio.to_thread(
                    self._advance, asset, now=clock, next_open=None,
                    empty=True, status="upstream_empty",
                    retry_after=clock + timedelta(hours=6),
                )
                return 0
            added = 0
            if candles:
                result = await asyncio.to_thread(
                    self.market.ingest,
                    MarketBatch((), tuple(candles), 5, observed),
                )
                added = result.inserted_candles
            next_cursor = (
                contiguous_backward_next(
                    cursor, [candle.opened_at for candle in candles],
                )
                if candles else oldest
            )
            await asyncio.to_thread(
                self._advance, asset, now=clock, next_open=next_cursor,
                inserted=added,
                status="caught_up" if next_cursor <= start else "progress",
            )
            _LOG.info(
                "RivaL OKX historical recovery asset=%s rows=%d inserted=%d",
                asset, len(rows), added,
            )
            return added
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - history must not stop live feeds
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
                "RivaL OKX history recovery paused asset=%s reason=%s",
                asset, "access" if blocked else "validation/network",
            )
            return 0

    def progress(self) -> list[dict]:
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM perp_history_recovery
                   WHERE venue='okx' ORDER BY asset""",
            ).fetchall()
        return [dict(row) for row in rows]
