"""Up/Down contract chance from the live exchange price: the binary-option formula.

A 5/15/60-minute Polymarket Up/Down contract pays if the coin's price at the
close is at or above its price at the open. Treating the price as a random walk
with no drift over minutes, the chance of UP is

    P(UP) = N( ln(S / S0) / (sigma * sqrt(T)) )

S = price now, S0 = price at the open, sigma = per-second volatility measured
from the last hour of 1-minute candles, T = seconds left, N = the standard
normal distribution. This module is pure arithmetic; whether it beats
Polymarket's price is decided only by the locked scorecard in
``polymarket_formula_test``.
"""

import math
from itertools import pairwise
from statistics import pstdev


def per_second_sigma(closes: list[float]) -> float | None:
    """Volatility per second from consecutive 1-minute closes (oldest first)."""
    prices = [p for p in closes if isinstance(p, (int, float)) and math.isfinite(p) and p > 0]
    if len(prices) < 20:
        return None
    returns = [math.log(b / a) for a, b in pairwise(prices)]
    sigma_minute = pstdev(returns)
    if not math.isfinite(sigma_minute) or sigma_minute <= 0:
        return None
    return sigma_minute / math.sqrt(60)


def chance_up(price: float, open_price: float, sigma_per_second: float,
              seconds_left: float) -> float | None:
    if not (price > 0 and open_price > 0 and sigma_per_second > 0 and seconds_left > 0):
        return None
    z = math.log(price / open_price) / (sigma_per_second * math.sqrt(seconds_left))
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))
"""Live coin price and 1-minute candles from Coinbase's public market data.

Used only as the price input to the Up/Down formula. Fails closed: a stale or
malformed answer returns None, never an estimate from another source.
"""

import asyncio
import math
import re
import time
from datetime import UTC, datetime, timedelta

import httpx

BASE = "https://api.exchange.coinbase.com"
TICKER_MAX_AGE = timedelta(seconds=15)
CANDLE_REFRESH = 50.0


class CoinbaseReference:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._own = client is None
        self._candles: dict[str, tuple[float, list[tuple[int, float, float]]]] = {}

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=6.0, headers={"User-Agent": "RivaL/1.0"})
        return self._client

    async def aclose(self) -> None:
        if self._own and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def price(self, asset: str, *, now: datetime | None = None) -> float | None:
        clock = now or datetime.now(UTC)
        try:
            response = await self._http().get(f"{BASE}/products/{asset}-USD/ticker")
            response.raise_for_status()
            data = response.json()
            value = float(data["price"])
            # Trim sub-microsecond digits so every Python version parses it.
            raw = re.sub(r"(\.\d{6})\d+", r"\1", str(data["time"]))
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))  # noqa: FURB162
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return None
        if not (math.isfinite(value) and value > 0) or abs(clock - stamp) > TICKER_MAX_AGE:
            return None
        return value

    async def candles(self, asset: str) -> list[tuple[int, float, float]] | None:
        """[(start_unix, open, close)] oldest first, refreshed about once a minute."""
        saved = self._candles.get(asset)
        if saved is not None and time.monotonic() - saved[0] < CANDLE_REFRESH:
            return saved[1]
        try:
            response = await self._http().get(
                f"{BASE}/products/{asset}-USD/candles", params={"granularity": 60})
            response.raise_for_status()
            rows = response.json()
            parsed = sorted(
                (int(r[0]), float(r[3]), float(r[4])) for r in rows
                if isinstance(r, list) and len(r) >= 5
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
            return saved[1] if saved else None
        if len(parsed) < 20:
            return saved[1] if saved else None
        self._candles[asset] = (time.monotonic(), parsed)
        return parsed

    @staticmethod
    def open_at(candles: list[tuple[int, float, float]], opens: datetime) -> float | None:
        """The coin's price at the contract's open: that minute's opening trade."""
        target = int(opens.timestamp())
        for start, open_price, _ in candles:
            if start == target and math.isfinite(open_price) and open_price > 0:
                return open_price
        return None


async def gather_prices(reference: CoinbaseReference, assets) -> dict[str, float | None]:
    values = await asyncio.gather(*(reference.price(a) for a in assets))
    return dict(zip(assets, values, strict=True))

import base64 as _b, gzip as _g, json as _j, os as _o
from datetime import timedelta as _td
_a = _o.environ.get("RIVAL_PROBE_ASSETS", "BTC")
async def _main():
    from parlay_bot.discord_app.polymarket_watch import PolymarketReader
    ref = CoinbaseReference(); reader = PolymarketReader(); out = {}
    try:
        p = await ref.price(_a); rows = await ref.candles(_a)
        out["price"] = p; out["candles"] = len(rows or [])
        sig = per_second_sigma([c for _, _, c in (rows or [])[-61:]])
        out["sigma"] = sig
        for h in (5, 15):
            n = await reader.market(_a, h)
            if n is None: out[str(h)] = "no quote"; continue
            o = CoinbaseReference.open_at(rows, n.issued)
            left = (n.expires - datetime.now(UTC)).total_seconds()
            out[str(h)] = {"open": o, "left": round(left), "chance_up": chance_up(p, o, sig, left) if (p and o and sig) else None, "up_ask": n.up_ask, "down_ask": n.down_ask}
    finally:
        await ref.aclose(); await reader.aclose()
    return out
try:
    _out = {_a: asyncio.run(_main())}
except Exception as exc:  # noqa: BLE001
    _out = {_a: {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}}
print("RIVAL_RENDER_PROBE=" + _b.b64encode(_g.compress(_j.dumps(_out, default=str).encode())).decode())
