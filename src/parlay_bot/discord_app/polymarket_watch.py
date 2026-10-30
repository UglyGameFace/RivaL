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
ASSETS = ("BTC", "ETH", "SOL", "XRP")
HORIZONS = (5, 15)
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
MAX_BOOK_AGE = timedelta(seconds=25)
MIN_SECONDS_LEFT = 90
REFERENCE_SHARES = 10.0


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
    up_average_ask: float | None = None  # depth for ten shares, before fees
    down_average_ask: float | None = None

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
    date = datetime.fromisoformat(str(s))
    if date.utcoffset() is None:
        raise ValueError("unverified timezone")
    return date.astimezone(UTC)


def _decode_array(value: object) -> list[str]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError("invalid outcome metadata")
    return value


def _ask_levels(book: dict, *, now: datetime) -> list[tuple[float, float]]:
    if not isinstance(book, dict) or not book.get("timestamp"):
        raise ValueError("orderbook timestamp unavailable")
    value = float(book["timestamp"])
    if not math.isfinite(value):
        raise ValueError("orderbook timestamp invalid")
    # CLOB book timestamps are milliseconds.
    quoted = datetime.fromtimestamp(value / 1000, tz=UTC)
    if quoted > now + timedelta(seconds=5) or now - quoted > MAX_BOOK_AGE:
        raise ValueError("stale or future Polymarket book")
    asks = book.get("asks")
    if not isinstance(asks, list):
        raise TypeError("no asks")
    levels = []
    for level in asks:
        try:
            price = float(level["price"])
            size = float(level["size"])
            if math.isfinite(price) and math.isfinite(size) and 0 < price < 1 and size > 0:
                levels.append((price, size))
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    if not levels:
        raise ValueError("no quoted liquidity")
    return sorted(levels)


def _best_ask(book: dict, *, now: datetime) -> float:
    """Top-of-book only; not a guaranteed fill price."""
    return _ask_levels(book, now=now)[0][0]


def _average_ask(
    book: dict, *, now: datetime, shares: float = REFERENCE_SHARES,
) -> float | None:
    """Walk order-book depth, returning None when the requested size cannot fill.

    Excludes trading fees, latency and actual execution uncertainty.
    """
    if not math.isfinite(shares) or shares <= 0:
        raise ValueError("reference shares must be finite and positive")
    remaining = shares
    spent = 0.0
    for price, amount in _ask_levels(book, now=now):
        taken = min(amount, remaining)
        spent += taken * price
        remaining -= taken
        if remaining <= 1e-9:
            return spent / shares
    return None


@dataclass(frozen=True)
class PolymarketListedWindow:
    """Verified Gamma identity, NOT a claim that trading has begun."""

    asset: str
    horizon: int
    slug: str
    opens: datetime
    closes: datetime
    accepting_orders: bool
    price_to_beat: float | None

    @property
    def url(self) -> str:
        # Constructed from our verified deterministic event slug only.
        return f"https://polymarket.com/event/{self.slug}"


@dataclass(frozen=True)
class PolymarketWindowBoard:
    checked_at: datetime
    current: PolymarketListedWindow | None
    next_window: PolymarketListedWindow | None
    current_quote: PolymarketNotice | None


class PolymarketReader:
    """Bounded cache avoids calling public Gamma/CLOB on each subscriber."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, int], tuple[datetime, PolymarketNotice | None]] = {}
        self._locks: dict[tuple[str, int], asyncio.Lock] = {}
        # Display-only reads must share a SHORT, slug-specific in-flight
        # result; bypassing every cache doubled CLOB/Gamma work for the
        # same 5m/15m contract. Never reuse an expired previous window.
        self._display_cache: dict[
            tuple[str, int], tuple[datetime, str, PolymarketNotice | None]
        ] = {}
        self._listed_cache: dict[
            str, tuple[datetime, PolymarketListedWindow | None]
        ] = {}
        self._listed_locks: dict[str, asyncio.Lock] = {}

    async def listed(
        self, asset: str, horizon: int, opened: datetime, *,
        now: datetime | None = None, refresh: bool = False,
    ) -> PolymarketListedWindow | None:
        """Look up an actual current or next Gamma listing, never a guessed URL.

        Future listings are shown as pre-open targets only; no book quote,
        price-to-beat, or executable trading status is inferred from them.
        """
        instant = (now or datetime.now(UTC)).astimezone(UTC)
        start = opened.astimezone(UTC)
        slug = market_slug(asset, horizon, start)
        if start != datetime.fromtimestamp(
            int(start.timestamp()) // (horizon * 60) * (horizon * 60),
            tz=UTC,
        ):
            raise ValueError("Unaligned window")
        # Bound the per-contract locks over months of 5-minute rollover.
        # Never evict an active lock (which may have concurrent waiters).
        if len(self._listed_locks) > 64:
            for old, active in list(self._listed_locks.items()):
                if not active.locked():
                    self._listed_locks.pop(old, None)
                if len(self._listed_locks) <= 32:
                    break
        # Gamma listing checks from the live board and Auto-Hunt share a
        # slug-keyed request, even when both start during the same second.
        async with self._listed_locks.setdefault(slug, asyncio.Lock()):
            cache = self._listed_cache.get(slug)
            if not refresh and cache is not None and cache[0] > instant:
                return cache[1]
            listing = None
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(3.5), follow_redirects=False,
                ) as client:
                    res = await client.get(f"{GAMMA}/events/slug/{slug}")
                    if res.status_code != 404:
                        res.raise_for_status()
                        event = res.json()
                        if isinstance(event, dict) and event.get("slug") == slug:
                            markets = event.get("markets")
                            if (
                                event.get("closed") is False
                                and event.get("archived") is not True
                                and isinstance(markets, list) and len(markets) == 1
                                and isinstance(markets[0], dict)
                            ):
                                m = markets[0]
                                opens = _date(
                                    event.get("eventStartTime")
                                    or m.get("eventStartTime")
                                )
                                closes = _date(
                                    event.get("endDate") or m.get("endDate")
                                )
                                if (
                                    opens == start
                                    and closes == start + timedelta(minutes=horizon)
                                    and m.get("closed") is False
                                    and _decode_array(m.get("outcomes"))
                                    == ["Up", "Down"]
                                ):
                                    active = bool(
                                        event.get("active") is True
                                        and m.get("active") is True
                                        and m.get("acceptingOrders") is True
                                        and m.get("enableOrderBook") is True
                                        and start <= instant < closes
                                    )
                                    target = None
                                    metadata = event.get("eventMetadata")
                                    if (
                                        start <= instant < closes
                                        and isinstance(metadata, dict)
                                    ):
                                        try:
                                            candidate = float(
                                                metadata.get("priceToBeat")
                                            )
                                            if (
                                                math.isfinite(candidate)
                                                and candidate > 0
                                            ):
                                                target = candidate
                                        except (TypeError, ValueError, OverflowError):
                                            pass
                                    listing = PolymarketListedWindow(
                                        asset=asset, horizon=horizon,
                                        slug=slug, opens=opens, closes=closes,
                                        accepting_orders=active,
                                        price_to_beat=target,
                                    )
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                _LOG.info(
                    "Polymarket verified listing unavailable asset=%s "
                    "horizon=%s reason=%s",
                    asset, horizon, type(exc).__name__,
                )
            self._listed_cache[slug] = (
                instant + timedelta(seconds=10 if listing else 5),
                listing,
            )
            return listing

    async def window_board(
        self, asset: str, horizon: int, *, now: datetime | None = None,
        refresh: bool = False,
    ) -> PolymarketWindowBoard:
        """Shared read-only current/next market view for one selected window."""
        stamp = (now or datetime.now(UTC)).astimezone(UTC)
        if asset not in ASSETS or horizon not in HORIZONS:
            raise ValueError("Choose BTC, ETH, SOL or XRP and 5m/15m")
        period = horizon * 60
        start = datetime.fromtimestamp(
            int(stamp.timestamp()) // period * period, tz=UTC,
        )
        next_start = start + timedelta(seconds=period)
        current, upcoming, quote = await asyncio.gather(
            self.listed(asset, horizon, start, now=stamp, refresh=refresh),
            self.listed(asset, horizon, next_start, now=stamp, refresh=refresh),
            self.market(
                asset, horizon, now=stamp,
                refresh=refresh, display_only=True,
            ),
        )
        # One market can close between Gamma and CLOB reads. Do not show a
        # quote for a missing/expired or different verified listing.
        if (
            current is None or quote is None
            or quote.slug != current.slug
            or quote.expires <= datetime.now(UTC)
        ):
            quote = None
        return PolymarketWindowBoard(
            checked_at=stamp, current=current,
            next_window=upcoming, current_quote=quote,
        )

    async def market(
        self, asset: str, horizon: int, *, now: datetime | None = None,
        opened: datetime | None = None,
        refresh: bool = False,
        display_only: bool = False,
    ) -> PolymarketNotice | None:
        instant = (now or datetime.now(UTC)).astimezone(UTC)
        target = opened or instant
        slug = market_slug(asset, horizon, target)
        key = (asset, horizon)
        # Different asset/horizon reads run concurrently; one sluggish
        # market never blocks all other Polymarket notifications.
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._cache.get(key)
            display = self._display_cache.get(key)
            if (
                display_only and not refresh and opened is None
                and display is not None and display[0] > instant
                and display[1] == slug
            ):
                candidate = display[2]
                return candidate if candidate is None or candidate.expires > datetime.now(UTC) else None
            if (
                not (refresh or display_only)
                and opened is None and cached is not None
                and cached[0] > instant
                and (cached[1] is None or cached[1].slug == slug)
            ):
                return cached[1]
            # A validated fresh display quote also serves normal research.
            # A negative display result does not overrule the independent
            # eligibility rules of normal Auto-Hunt alerts.
            if (
                not display_only and not refresh and opened is None
                and display is not None and display[0] > instant
                and display[1] == slug and display[2] is not None
                and (display[2].expires - instant).total_seconds() >= MIN_SECONDS_LEFT
            ):
                return display[2]
            try:
                notice = await self._lookup(
                    slug=slug, asset=asset, horizon=horizon, now=instant,
                    min_seconds_left=0 if display_only else MIN_SECONDS_LEFT,
                )
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                _LOG.info(
                    "Polymarket read-only market unavailable asset=%s horizon=%s reason=%s",
                    asset, horizon, type(exc).__name__,
                )
                notice = None
            # 5s display cache reduces duplicated Gamma/CLOB burst traffic
            # while keeping a genuinely short freshness window for quotes.
            if opened is None and display_only:
                self._display_cache[key] = (
                    instant + timedelta(seconds=5), slug, notice,
                )
            elif opened is None:
                self._cache[key] = (
                    instant + timedelta(seconds=15 if notice else 10), notice,
                )
            return notice

    async def _lookup(
        self, *, slug: str, asset: str, horizon: int, now: datetime,
        min_seconds_left: int = MIN_SECONDS_LEFT,
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
            if not (start <= now < end and (end - now).total_seconds() >= min_seconds_left):
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
            if expires <= quote_at or (
                expires - quote_at < timedelta(seconds=min_seconds_left)
            ):
                return None
            return PolymarketNotice(
                asset=asset, horizon=horizon, issued=start,
                expires=expires, slug=slug,
                up_ask=up, down_ask=down, quote_at=quote_at,
                up_average_ask=_average_ask(up_book, now=quote_at),
                down_average_ask=_average_ask(down_book, now=quote_at),
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
