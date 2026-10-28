"""Bounded, restart-safe KuCoin USDTM 5m historical futures recovery.

One window <=200 bars/coin/cycle from KuCoin's public UTA V2 market API.
A recovered old bar records *today's first observation*, never a fake live
prediction from months ago. Separate venue cursor shares Gate's audited table.
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
)
from parlay_bot.perpetuals.hyperliquid import MarketBatch, MarketCandle, MarketDataError
from parlay_bot.perpetuals.market_store import PerpetualMarketStore

_LOG = logging.getLogger(__name__)


class KucoinHistoryRecovery(GateHistoryRecovery):
    """KuCoin UTA public trade klines, isolated from live KuCoin polling."""

    def __init__(
        self, market: PerpetualMarketStore, *,
        client: httpx.AsyncClient | None = None,
        assets: tuple[str, ...] = _ALLOWED,
        page_bars: int = 200,
    ) -> None:
        if not 12 <= page_bars <= 200:
            raise ValueError("KuCoin Futures history page cannot exceed 200 bars")
        super().__init__(market, client=client, assets=assets, page_bars=page_bars)
        # A short metadata cache saves repeat contract-lookups; never cache
        # candles or successful source statuses here.
        self._metadata: dict[str, tuple[float, datetime]] = {}

    async def _public(self, url: str, params: dict[str, Any]) -> Any:
        try:
            response = await self._client.get(
                url, params=params, follow_redirects=False,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MarketDataError("KuCoin public futures history unavailable") from exc

    def _position(self, asset: str, now: datetime) -> dict:
        self.initialize()  # Same durable schema used by Gate, different venue key.
        with self.store.connect() as conn:
            row = conn.execute(
                """SELECT * FROM perp_history_recovery
                   WHERE venue='kucoin' AND asset=?""",
                (asset,),
            ).fetchone()
            if row is None:
                before = six_months_prior(now)
                start = before.replace(
                    minute=before.minute // 5 * 5, second=0, microsecond=0,
                ).isoformat()
                conn.execute(
                    """INSERT INTO perp_history_recovery
                       (venue,asset,started_from,next_opened_at)
                       VALUES ('kucoin',?,?,?) ON CONFLICT DO NOTHING""",
                    (asset, start, start),
                )
                row = conn.execute(
                    """SELECT * FROM perp_history_recovery
                       WHERE venue='kucoin' AND asset=?""",
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
                   next_opened_at=CASE WHEN ? > next_opened_at THEN ?
                                       ELSE next_opened_at END,
                   attempted_at=?,inserted_bars=inserted_bars+?,
                   empty_windows=empty_windows+?,status=?,retry_after=?
                   WHERE venue='kucoin' AND asset=?""",
                (next_open.isoformat() if next_open else None,
                 next_open.isoformat() if next_open else None,
                 now.isoformat(), inserted, int(empty), status,
                 retry_after.isoformat() if retry_after else None, asset),
            )

    async def _multiplier(self, asset: str, symbol: str, now: datetime) -> float:
        cached = self._metadata.get(symbol)
        if cached and timedelta(0) <= now - cached[1] < timedelta(hours=1):
            return cached[0]
        payload = await self._public(
            f"https://api-futures.kucoin.com/api/v1/contracts/{symbol}", {}
        )
        meta = payload.get("data") if isinstance(payload, dict) else None
        base = "XBT" if asset == "BTC" else asset
        if (
            not isinstance(payload, dict) or payload.get("code") != "200000"
            or not isinstance(meta, dict) or meta.get("symbol") != symbol
            or meta.get("baseCurrency") != base
            or meta.get("quoteCurrency") != "USDT"
            or meta.get("settleCurrency") != "USDT"
            or meta.get("isInverse") is not False
            or meta.get("status") != "Open"
            or meta.get("expireDate") is not None
        ):
            raise MarketDataError("KuCoin active linear perpetual unavailable")
        factor = _num(meta.get("multiplier"), positive=True)
        if cached is not None and cached[0] != factor:
            # Metadata may have changed since older history was recorded.
            # Don't silently rewrite quantities with a new scale.
            self._metadata.clear()
            raise MarketDataError("KuCoin contract multiplier changed; audit required")
        self._metadata[symbol] = (factor, now)
        return factor

    async def fetch_asset(
        self, asset: str, *, now: datetime | None = None,
    ) -> int:
        if asset not in self.assets:
            raise ValueError("unconfigured KuCoin recovery asset")
        clock = _utc(now or datetime.now(UTC))
        position = await asyncio.to_thread(self._position, asset, clock)
        if position["retry_after"] and _utc(datetime.fromisoformat(
            str(position["retry_after"])
        )) > clock:
            return 0
        cursor = _utc(datetime.fromisoformat(str(position["next_opened_at"])))
        end = clock.replace(
            minute=clock.minute // 5 * 5, second=0, microsecond=0,
        ) - timedelta(hours=4)
        if cursor >= end:
            await asyncio.to_thread(
                self._advance, asset, now=clock, next_open=None,
                status="caught_up",
            )
            return 0
        until = min(cursor + self.page_bars * _STEP, end)
        base = "XBT" if asset == "BTC" else asset
        symbol = f"{base}USDTM"
        try:
            factor = await self._multiplier(asset, symbol, clock)
            data = await self._public(
                "https://api.kucoin.com/api/ua/v2/market/kline",
                {
                    "tradeType": "FUTURES", "symbol": symbol,
                    "klineType": "TRADE", "interval": "5min",
                    "startAt": int(cursor.timestamp()),
                    "endAt": int((until - _STEP).timestamp()),
                },
            )
            result = data.get("data") if isinstance(data, dict) else None
            if (
                not isinstance(data, dict) or data.get("code") != "200000"
                or not isinstance(result, dict)
                or result.get("symbol") != symbol
                or result.get("tradeType") != "FUTURES"
                or not isinstance(result.get("list"), list)
                or len(result["list"]) > self.page_bars
            ):
                raise MarketDataError("invalid KuCoin futures history response")
            candles = []
            seen: set[int] = set()
            observed = datetime.now(UTC)
            for row in result["list"]:
                if not isinstance(row, list) or len(row) < 7:
                    raise MarketDataError("invalid KuCoin historical candle")
                raw = row[0]
                if isinstance(raw, bool) or not isinstance(raw, (int, str)):
                    raise MarketDataError("invalid KuCoin timestamp")
                value = str(raw)
                if not value.isascii() or not value.isdecimal():
                    raise MarketDataError("invalid KuCoin timestamp")
                timestamp = int(value)
                if len(value) == 13:
                    if timestamp % 1000:
                        raise MarketDataError("non-second historical timestamp")
                    timestamp //= 1000
                elif len(value) != 10:
                    raise MarketDataError("unexpected KuCoin timestamp unit")
                opened = datetime.fromtimestamp(timestamp, UTC)
                if (
                    timestamp in seen or not cursor <= opened < until
                    or timestamp % 300
                ):
                    raise MarketDataError("out-of-window or duplicate KuCoin bar")
                seen.add(timestamp)
                op, high, low, close = (
                    _num(v, positive=True) for v in row[1:5]
                )
                if low > min(op, close) or high < max(op, close):
                    raise MarketDataError("inconsistent KuCoin historical OHLC")
                candles.append(MarketCandle(
                    "kucoin", symbol, "USDT", 5, opened,
                    opened + _STEP - timedelta(milliseconds=1),
                    observed, op, high, low, close,
                    _num(row[5]) * factor, -1,
                ))
            added = 0
            if candles:
                saved = await asyncio.to_thread(
                    self.market.ingest, MarketBatch((), tuple(candles), 5, observed)
                )
                added = saved.inserted_candles
            await asyncio.to_thread(
                self._advance, asset, now=clock, next_open=until,
                inserted=added, empty=not bool(candles),
                status="progress",
            )
            _LOG.info(
                "RivaL KuCoin history asset=%s rows=%d inserted=%d",
                asset, len(candles), added,
            )
            return added
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one provider never stalls live data
            blocked = (
                isinstance(exc, MarketDataError)
                and isinstance(exc.__cause__, httpx.HTTPStatusError)
                and exc.__cause__.response.status_code in (401, 403, 429, 451)
            )
            reason = "blocked" if blocked else "retry"
            pause = timedelta(hours=6) if blocked else timedelta(minutes=20)
            await asyncio.to_thread(
                self._advance, asset, now=clock,
                next_open=None, status=reason, retry_after=clock + pause,
            )
            _LOG.warning(
                "RivaL KuCoin history asset=%s unavailable status=%s",
                asset, reason,
            )
            return 0

    def progress(self) -> list[dict]:
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM perp_history_recovery
                   WHERE venue='kucoin' ORDER BY asset"""
            ).fetchall()
        return [dict(r) for r in rows]
