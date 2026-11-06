"""Independent USD spot books with a three-source quorum and time-aligned minute candles.

Coinbase, Bitstamp, Gemini and Bitfinex public APIs; no USDT/USD parity assumption and
no perpetual-to-spot substitution. A median is a proxy, never a Chainlink
receipt. Missing quorum, stale timestamps, gaps or source divergence abstain.
"""
from __future__ import annotations

import asyncio
import math
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from itertools import pairwise
from statistics import median

import httpx

from parlay_bot.perpetuals.price_formula import microprice

VENUES = ("coinbase", "bitstamp", "gemini", "bitfinex")
QUORUM = 3


def clean_candles(rows, *, now: datetime) -> list[tuple[int, float, float]] | None:
    """Closed, consecutive one-minute bars only; never bridge missing bars."""
    valid = sorted((int(t), float(o), float(c)) for t, o, c in rows
                   if int(t) + 60 <= now.timestamp())[-240:]
    if (len(valid) < 120 or now.timestamp() - valid[-1][0] > 150
            or any(t % 60 or not all(math.isfinite(x) and x > 0 for x in (o, c))
                   for t, o, c in valid)
            or any(b[0] - a[0] != 60 for a, b in pairwise(valid))):
        return None
    return valid


def combine(sources: dict, *, opens: datetime, now: datetime) -> dict | None:
    usable = {k: v for k, v in sources.items()
              if k in VENUES and v is not None
              and timedelta(0) <= now - v["at"] <= timedelta(seconds=15)}
    if len(usable) < QUORUM:
        return None
    prices = [v["price"] for v in usable.values()]
    if any(not math.isfinite(p) or p <= 0 for p in prices):
        return None
    price = median(prices)
    if max(prices) / min(prices) > 1.01:
        return None
    by_source = {k: {t: (o, c) for t, o, c in v["candles"]} for k, v in usable.items()}
    stamps = sorted(set.intersection(*(set(x) for x in by_source.values())))
    rows = [(t, median(x[t][0] for x in by_source.values()),
             median(x[t][1] for x in by_source.values())) for t in stamps]
    rows = clean_candles(rows, now=now)
    if rows is None:
        return None
    openings = [x[int(opens.timestamp())][0] for x in by_source.values() if int(opens.timestamp()) in x]
    opening = median(openings) if len(openings) >= QUORUM else None
    # Never substitute the latest minute for the contract's opening minute.
    pressure = [v["microprice"] for v in usable.values() if v["microprice"] is not None]
    return {"price": price, "open": opening, "closes": [c for _, _, c in rows],
            "quote_currency": "USD", "reference_label": "median of 3+ USD spot exchanges",
            "microprice": median(pressure) if len(pressure) >= QUORUM else None,
            "at": min(v["at"] for v in usable.values()),
            "sources": {k: {"price": v["price"], "at": v["at"].isoformat(),
                            "clock": v["clock"]} for k, v in usable.items()},
            "candles": rows}


class SpotReference:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client
        self._own = client is None
        self._candles: dict[tuple, tuple[float, list]] = {}
        self._blocked: dict[tuple, float] = {}
        self._native_opens: dict[tuple, float] = {}

    def _http(self):
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=8, follow_redirects=False,
                                            limits=httpx.Limits(max_connections=12))
        return self._client

    async def aclose(self):
        if self._own and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, venue, asset, url, **kwargs):
        key = (venue, asset)
        if self._blocked.get(key, 0) > time.monotonic():
            raise ValueError("source cooldown")
        response = await self._http().get(url, **kwargs)
        if response.status_code in (403, 404, 429, 451):
            self._blocked[key] = time.monotonic() + 3600
        response.raise_for_status()
        return response

    async def _source(self, venue, asset, *, candles=None):
        if candles is None:
            candles = await self._bars(venue, asset)
        if candles is None:
            return None
        try:
            symbol = asset.lower() + "usd"
            if venue == "coinbase":
                base = f"https://api.exchange.coinbase.com/products/{asset}-USD"
                response = await self._get(venue, asset, base + "/book", params={"level": 1})
                book = response.json()
                at = datetime.fromisoformat(book["time"])
                if book.get("auction_mode") is True:
                    return None
                bid, ask = book["bids"][0], book["asks"][0]
                values = (float(bid[0]), float(ask[0]), float(bid[1]), float(ask[1]))
                clock = "book"
            elif venue == "bitstamp":
                base = f"https://www.bitstamp.net/api/v2/order_book/{symbol}/"
                response = await self._get(venue, asset, base)
                book = response.json()
                at = datetime.fromtimestamp(float(book["timestamp"]), UTC)
                bid, ask = book["bids"][0], book["asks"][0]
                values = (float(bid[0]), float(ask[0]), float(bid[1]), float(ask[1]))
                clock = "book"
            elif venue == "binance_spot":
                response = await self._get(venue, asset, "https://api.binance.com/api/v3/depth",
                                           params={"symbol": asset+"USDT", "limit": 5})
                book = response.json()
                bid, ask = book["bids"][0], book["asks"][0]
                values = (float(bid[0]), float(ask[0]), float(bid[1]), float(ask[1]))
                at = parsedate_to_datetime(response.headers["Date"])
                if int(response.headers.get("Age", "0")) != 0:
                    return None
                clock = "http-snapshot"
            elif venue == "gemini":
                response = await self._get(venue, asset, f"https://api.gemini.com/v1/book/{symbol}",
                                           params={"limit_bids": 1, "limit_asks": 1})
                book = response.json()
                # Gemini explicitly documents level timestamps as dummy values.
                # Require a fresh uncached server HTTP snapshot instead, label it.
                at = parsedate_to_datetime(response.headers["Date"])
                if int(response.headers.get("Age", "0")) != 0:
                    return None
                bid, ask = book["bids"][0], book["asks"][0]
                values = (float(bid["price"]), float(ask["price"]),
                          float(bid["amount"]), float(ask["amount"]))
                clock = "http-snapshot"
            else:
                response = await self._get(venue, asset, f"https://api-pub.bitfinex.com/v2/book/t{asset}USD/P0",
                                           params={"len": 1})
                levels = response.json()
                bid = max((r for r in levels if r[2] > 0 and r[1] > 0), key=lambda r:r[0])
                ask = min((r for r in levels if r[2] < 0 and r[1] > 0), key=lambda r:r[0])
                values = (float(bid[0]), float(ask[0]), float(bid[2]), -float(ask[2]))
                at = parsedate_to_datetime(response.headers["Date"])
                if int(response.headers.get("Age", "0")) != 0:
                    return None
                clock = "http-snapshot"
            now = datetime.now(UTC)
            if (at.utcoffset() is None or not timedelta(0) <= now-at <= timedelta(seconds=15)
                    or values[1] / values[0] > 1.01):
                return None
            pressure = microprice(*values)
            if pressure is None:
                return None
            return None if candles is None else {
                "price": (values[0] + values[1]) / 2, "microprice": pressure,
                "at": at, "candles": candles, "clock": clock}
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError, ZeroDivisionError, OverflowError):
            return None

    async def _bars(self, venue, asset):
        try:
            symbol = asset.lower()+"usd"
            now = datetime.now(UTC)
            key = (venue, asset)
            saved = self._candles.get(key)
            if saved is None or time.monotonic() - saved[0] >= 45:
                if venue == "coinbase":
                    # Ask for the exact closed-bar interval. The unbounded
                    # default endpoint can serve a lagging cached tail.
                    end = datetime.fromtimestamp(int(now.timestamp())//60*60, UTC)
                    response = await self._get(venue, asset, f"https://api.exchange.coinbase.com/products/{asset}-USD/candles", params={
                        "granularity": 60, "start": (end-timedelta(hours=5)).isoformat(),
                        "end": end.isoformat(),
                    })
                    rows = [(r[0], r[3], r[4]) for r in response.json()]
                elif venue == "bitstamp":
                    response = await self._get(venue, asset, f"https://www.bitstamp.net/api/v2/ohlc/{symbol}/",
                                               params={"step": 60, "limit": 300, "exclude_current_candle": "true"})
                    rows = [(r["timestamp"], r["open"], r["close"]) for r in response.json()["data"]["ohlc"]]
                elif venue == "binance_spot":
                    response = await self._get(venue, asset, "https://api.binance.com/api/v3/klines",
                                               params={"symbol": asset+"USDT", "interval": "1m", "limit": 300})
                    rows = [(int(r[0])//1000, r[1], r[4]) for r in response.json()]
                elif venue == "gemini":
                    response = await self._get(venue, asset, f"https://api.gemini.com/v2/candles/{symbol}/1m")
                    rows = [(int(r[0]) // 1000, r[1], r[4]) for r in response.json()]
                else:
                    response = await self._get(venue, asset,
                        f"https://api-pub.bitfinex.com/v2/candles/trade:1m:t{asset}USD/hist",
                        params={"limit": 300, "sort": -1})
                    rows = [(int(r[0])//1000, r[1], r[2]) for r in response.json()]
                self._candles[key] = (time.monotonic(), rows)
            else:
                rows = saved[1]
            return clean_candles(rows, now=datetime.now(UTC))
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError, OverflowError):
            return None

    async def prepare(self, assets):
        keys = [(a, v) for a in sorted(set(assets)) for v in VENUES]
        return keys, await asyncio.gather(*(self._bars(v, a) for a, v in keys))

    async def snapshots(self, assets) -> dict:
        # Warm/revalidate historical bars BEFORE taking fresh books. A slow
        # candle provider must not age the other providers' live prices.
        keys, bars = await self.prepare(assets)
        ready = [(a, v, b) for (a, v), b in zip(keys, bars, strict=True) if b is not None]
        results = await asyncio.gather(*(self._source(v, a, candles=b) for a, v, b in ready))
        out: dict = {a: {v: None for v in VENUES} for a in assets}
        for (asset, venue, _), result in zip(ready, results, strict=True):
            out[asset][venue] = result
        return out

    async def _hour_open(self, asset: str, opened: int) -> float | None:
        key = (asset, opened)
        if key in self._native_opens:
            return self._native_opens[key]
        try:
            response = await self._get("binance_spot", asset, "https://api.binance.com/api/v3/klines",
                                       params={"symbol": asset+"USDT", "interval": "1h",
                                               "startTime": opened*1000, "limit": 1})
            rows = response.json()
            if not rows or int(rows[0][0]) != opened*1000:
                return None
            value = float(rows[0][1])  # original O only; NEVER its forming/future close
            if not math.isfinite(value) or value <= 0:
                return None
            self._native_opens[key] = value
            self._native_opens = {k: v for k, v in self._native_opens.items() if k[1] >= opened-7200}
            return value
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError, OverflowError):
            return None

    async def prepare_native(self, assets):
        opened = int(datetime.now(UTC).timestamp())//3600*3600
        return await asyncio.gather(
            *(self._bars("binance_spot", a) for a in assets),
            *(self._hour_open(a, opened) for a in assets),
        )

    async def native_snapshots(self, assets) -> dict:
        """Exact Binance SPOT/USDT inputs for its hourly O/C contracts.

        No cross-venue USD median, futures substitution, proxy rotation or
        geographic bypass. Provider denial enters the normal source cooldown.
        """
        opened = int(datetime.now(UTC).timestamp())//3600*3600
        bars = await asyncio.gather(*(self._bars("binance_spot", a) for a in assets))
        ready = [(a, b) for a, b in zip(assets, bars, strict=True)
                 if b is not None and (a, opened) in self._native_opens]
        books = await asyncio.gather(*(self._source("binance_spot", a, candles=b) for a, b in ready))
        result = {}
        for (asset, rows), book in zip(ready, books, strict=True):
            if book is None:
                continue
            result[asset] = {"price": book["price"], "microprice": book["microprice"],
                             "open": self._native_opens[asset, opened], "opens_at": opened,
                             "quote_currency": "USDT", "native_venue": "binance_spot",
                             "open_source": "binance-hourly-open", "reference_label": "Binance spot (USDT)",
                             "closes": [r[2] for r in rows], "candles": rows,
                             "at": book["at"], "sources": {"binance_spot": {
                                 "price": book["price"], "at": book["at"].isoformat(),
                                 "clock": book["clock"], "quote_currency": "USDT"}}}
        return result
