"""Live Binance market context for the pooled model (read-only public data).

Fetches exactly the series the Drive warehouse archives for training:
5m klines (volume, taker-buy volume, trade count), 5m premium-index klines and
settled funding rates, plus open interest for RivaL Intelligence context.
Only CLOSED bars are kept. Never places orders; no keys are used.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, datetime, timedelta

import httpx

from parlay_bot.perpetuals.pooled_features import ASSETS, WINDOW

_LOG = logging.getLogger(__name__)
BASE = "https://fapi.binance.com"
LIMIT = WINDOW + 5
STALE_AFTER = timedelta(minutes=3)


def _ms(value) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


def parse_klines(rows, now: datetime) -> list[tuple]:
    """(close_at, volume, taker_buy_volume, trades) for closed bars only."""
    out = []
    for row in rows:
        close_at = _ms(row[0]) + timedelta(minutes=5)
        if close_at > now:
            continue  # still forming
        volume, taker, trades = float(row[5]), float(row[9]), float(row[8])
        if not all(math.isfinite(x) and x >= 0 for x in (volume, taker, trades)):
            continue
        out.append((close_at, volume, taker, trades))
    return sorted(out)


def parse_premium(rows, now: datetime) -> dict:
    return {
        _ms(row[0]) + timedelta(minutes=5): float(row[4])
        for row in rows
        if _ms(row[0]) + timedelta(minutes=5) <= now and math.isfinite(float(row[4]))
    }


def parse_funding(rows) -> list[tuple]:
    return sorted((_ms(r["fundingTime"]), float(r["fundingRate"])) for r in rows
                  if math.isfinite(float(r["fundingRate"])))


class BinanceContextFeed:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._bundles: dict[str, dict] = {}
        self.failures = 0

    async def _get(self, path: str, params: dict):
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10)
        response = await self._client.get(BASE + path, params=params)
        response.raise_for_status()
        return response.json()

    async def refresh_asset(self, asset: str) -> None:
        symbol = f"{asset}USDT"
        now = datetime.now(UTC)
        klines, premium, funding, oi = await asyncio.gather(
            self._get("/fapi/v1/klines", {"symbol": symbol, "interval": "5m", "limit": LIMIT}),
            self._get("/fapi/v1/premiumIndexKlines",
                      {"symbol": symbol, "interval": "5m", "limit": LIMIT}),
            self._get("/fapi/v1/fundingRate", {"symbol": symbol, "limit": 10}),
            self._get("/futures/data/openInterestHist",
                      {"symbol": symbol, "period": "5m", "limit": 13}),
            return_exceptions=True,
        )
        if isinstance(klines, BaseException):
            raise klines
        bundle = {
            "fetched_at": now,
            "klines": parse_klines(klines, now),
            "premium": {} if isinstance(premium, BaseException) else parse_premium(premium, now),
            "funding": [] if isinstance(funding, BaseException) else parse_funding(funding),
            "open_interest": [] if isinstance(oi, BaseException) else sorted(
                (_ms(r["timestamp"]), float(r["sumOpenInterest"])) for r in oi),
        }
        self._bundles[asset] = bundle

    async def refresh(self) -> None:
        results = await asyncio.gather(*(self.refresh_asset(a) for a in ASSETS),
                                       return_exceptions=True)
        failed = [a for a, r in zip(ASSETS, results) if isinstance(r, BaseException)]
        self.failures = self.failures + 1 if failed else 0
        if failed and self.failures in (1, 10) or (failed and self.failures % 120 == 0):
            _LOG.warning("Binance context refresh failed for %s (%d in a row)",
                         ",".join(failed), self.failures)

    def bundle(self, asset: str) -> dict | None:
        bundle = self._bundles.get(asset)
        if bundle is None or datetime.now(UTC) - bundle["fetched_at"] > STALE_AFTER:
            return None
        return bundle

    def numbers(self, asset: str) -> dict:
        """Numeric market context for RivaL Intelligence ({} when unavailable)."""
        bundle = self.bundle(asset)
        if not bundle or len(bundle["klines"]) < 12:
            return {}
        bars = bundle["klines"][-12:]
        out = {}
        volume = sum(b[1] for b in bars)
        if volume > 0:
            out["taker_buy_share_1h"] = round(sum(b[2] for b in bars) / volume, 4)
        premium = bundle["premium"].get(bars[-1][0])
        if premium is not None:
            out["premium_bps"] = round(premium * 1e4, 2)
        if bundle["funding"]:
            out["funding_bps"] = round(bundle["funding"][-1][1] * 1e4, 3)
        oi = bundle["open_interest"]
        if len(oi) >= 2 and oi[0][1] > 0:
            out["open_interest_change_1h"] = round(oi[-1][1] / oi[0][1] - 1, 5)
        return out

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
