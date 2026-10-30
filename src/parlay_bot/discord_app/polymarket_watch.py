"""Read-only Polymarket market watch. Zero orders, wallets, or market prediction claims.

Polymarket global Gamma/CLOB is NOT the separate Polymarket US exchange API.
Only confirmed returned event identity, outcomes, timing, and token books count.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

_LOG = logging.getLogger(__name__)
ASSETS = ("BTC", "ETH", "SOL")
HORIZONS = (5, 15)
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
MAX_BOOK_AGE = timedelta(seconds=25)
MIN_SECONDS_LEFT = 90


@dataclass(frozen=True)
class PolymarketNotice:
    asset: str
    horizon: int
    issued: datetime
    expires: datetime
    slug: str
    up_ask: float
    down_ask: float
    quote_at: datetime

    @property
    def venue(self) -> str:
        return "polymarket"

    @property
    def key(self) -> str:
        return hashlib.sha256(
            ("polymarket|" + self.slug).encode("utf-8"),
        ).hexdigest()

    @property
    def url(self) -> str:
        # Slug is always constructed by us, never interpolated from remote text.
        return f"https://polymarket.com/event/{self.slug}"


def market_slug(asset: str, horizon: int, opened: datetime) -> str:
    if asset not in ASSETS or horizon not in HORIZONS or opened.utcoffset() is None:
        raise ValueError("Unsupported Polymarket watch")
    period = horizon * 60
    bucket = int(opened.astimezone(UTC).timestamp()) // period * period
    return f"{asset.lower()}-updown-{horizon}m-{bucket}"


def _date(s: object) -> datetime:
    date = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if date.utcoffset() is None:
        raise ValueError("unverified timezone")
    return date.astimezone(UTC)


def _decode_array(value: object) -> list[str]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError("invalid outcome metadata")
    return value


def _best_ask(book: dict, *, now: datetime) -> float:
    if not isinstance(book, dict) or not book.get("timestamp"):
        raise ValueError("orderbook timestamp unavailable")
    value = float(book["timestamp"])
    if not math.isfinite(value):
        raise ValueError("orderbook timestamp invalid")
    # CLOB books use millisecond epoch timestamps.
    quoted = datetime.fromtimestamp(value / 1000, tz=UTC)
    if quoted > now + timedelta(seconds=5) or now - quoted > MAX_BOOK_AGE:
        raise ValueError("stale or future Polymarket book")
    asks = book.get("asks")
    if not isinstance(asks, list):
        raise ValueError("no asks")
    prices = []
    for level in asks:
        try:
            price = float(level["price"])
            size = float(level["size"])
            if math.isfinite(price) and math.isfinite(size) and 0 < price < 1 and size > 0:
                prices.append(price)
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    if not prices:
        raise ValueError("no quoted liquidity")
    return min(prices)


class PolymarketReader:
    """Bounded cache avoids calling public Gamma/CLOB on each subscriber."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, int], tuple[datetime, PolymarketNotice | None]] = {}
        self._lock = asyncio.Lock()

    async def market(
        self, asset: str, horizon: int, *, now: datetime | None = None,
        opened: datetime | None = None,
    ) -> PolymarketNotice | None:
        instant = (now or datetime.now(UTC)).astimezone(UTC)
        target = opened or instant
        slug = market_slug(asset, horizon, target)
        key = (asset, horizon)
        async with self._lock:
            cached = self._cache.get(key)
            if (
                opened is None and cached is not None
                and cached[0] > instant
                and (cached[1] is None or cached[1].slug == slug)
            ):
                return cached[1]
            try:
                notice = await self._lookup(
                    slug=slug, asset=asset, horizon=horizon, now=instant,
                )
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                _LOG.info(
                    "Polymarket read-only market unavailable asset=%s horizon=%s reason=%s",
                    asset, horizon, type(exc).__name__,
                )
                notice = None
            # 15s live cache, 10s negative cache. Never reuse an old window.
            if opened is None:
                self._cache[key] = (
                    instant + timedelta(seconds=15 if notice else 10), notice,
                )
            return notice

    async def _lookup(
        self, *, slug: str, asset: str, horizon: int, now: datetime,
    ) -> PolymarketNotice | None:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(3.5), follow_redirects=False,
        ) as client:
            response = await client.get(f"{GAMMA}/events/slug/{slug}")
            if response.status_code == 404:
                return None
            response.raise_for_status()
            event = response.json()
            if not isinstance(event, dict):
                return None
            if event.get("slug") != slug or event.get("closed") is not False:
                return None
            if event.get("active") is not True or event.get("archived") is True:
                return None
            markets = event.get("markets")
            if not isinstance(markets, list) or len(markets) != 1:
                return None
            market = markets[0]
            if not isinstance(market, dict):
                return None
            if market.get("closed") is not False or market.get("active") is not True:
                return None
            if market.get("acceptingOrders") is False or market.get("enableOrderBook") is False:
                return None
            expected = _date(datetime.fromtimestamp(
                int(slug.rsplit("-", 1)[-1]), tz=UTC,
            ).isoformat())
            expires = expected + timedelta(minutes=horizon)
            # eventStartTime is the WINDOW START; startDate can be listing date.
            start = _date(event.get("eventStartTime") or market.get("eventStartTime"))
            end = _date(event.get("endDate") or market.get("endDate"))
            if start != expected or end != expires:
                return None
            if not (start <= now < end and (end - now).total_seconds() >= MIN_SECONDS_LEFT):
                return None
            outcomes = _decode_array(market.get("outcomes"))
            tokens = _decode_array(market.get("clobTokenIds"))
            if outcomes != ["Up", "Down"] or len(tokens) != 2:
                return None
            if not all(x.isdecimal() and len(x) > 30 for x in tokens):
                return None
            async def book(token_id: str) -> dict:
                res = await client.get(f"{CLOB}/book", params={"token_id": token_id})
                res.raise_for_status()
                return res.json()

            up_book, down_book = await asyncio.gather(
                book(tokens[0]), book(tokens[1]),
            )
            quote_at = datetime.now(UTC)
            up = _best_ask(up_book, now=quote_at)
            down = _best_ask(down_book, now=quote_at)
            if expires - quote_at < timedelta(seconds=MIN_SECONDS_LEFT):
                return None
            return PolymarketNotice(
                asset=asset, horizon=horizon, issued=start,
                expires=expires, slug=slug,
                up_ask=up, down_ask=down, quote_at=quote_at,
            )

    async def active(self, *, now: datetime | None = None) -> list[PolymarketNotice]:
        stamp = now or datetime.now(UTC)
        # Bounded 6 markets and 18 HTTP calls per refresh, never per subscriber.
        outcomes = await asyncio.gather(
            *(self.market(asset, horizon, now=stamp)
              for asset in ASSETS for horizon in HORIZONS),
            return_exceptions=True,
        )
        return [x for x in outcomes if isinstance(x, PolymarketNotice)]
