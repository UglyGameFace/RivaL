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
import re
import time
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
READ_TIMEOUT = 4.0


class MarketUnavailable(ValueError):
    """Fixed diagnostic code; never log remote payloads as error messages."""


def unavailable_text(reason: str | None) -> str:
    return {
        "book_stale": "The exchange order book is too old.",
        "book_future": "The exchange order-book clock is ahead.",
        "no_liquidity": "No verified sell-side liquidity is available.",
        "event_not_found": "This contract is not listed yet.",
        "orders_unavailable": "This contract is not accepting verified orders.",
        "window_not_open": "The contract window is not open.",
        "too_late": "Less than 90 seconds remain for a new alert.",
        "request_timeout": "The market-data request timed out.",
        "http_429": "The provider is limiting market-data requests.",
        "http_403": "The provider denied this market-data request.",
    }.get(reason, "The provider response did not pass market verification.")


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
    received_at: datetime | None = None  # local receipt, NOT the CLOB timestamp

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


def _book_time(book: dict, *, now: datetime) -> datetime:
    if not isinstance(book, dict) or not book.get("timestamp"):
        raise MarketUnavailable("book_timestamp_missing")
    try:
        value = float(book["timestamp"])
        if not math.isfinite(value):
            raise ValueError
        quoted = datetime.fromtimestamp(value / 1000, tz=UTC)
    except (ValueError, TypeError, OverflowError, OSError):
        raise MarketUnavailable("book_timestamp_invalid") from None
    if quoted > now + timedelta(seconds=5):
        raise MarketUnavailable("book_future")
    if now - quoted > MAX_BOOK_AGE:
        raise MarketUnavailable("book_stale")
    return quoted


def _ask_levels(book: dict, *, now: datetime) -> list[tuple[float, float]]:
    _book_time(book, now=now)
    asks = book.get("asks")
    if not isinstance(asks, list):
        raise MarketUnavailable("no_asks")
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
        raise MarketUnavailable("no_liquidity")
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
    quote_unavailable_reason: str | None = None


class PolymarketReader:
    """One bounded, source-matched Gamma/book snapshot for all consumers."""

    def __init__(self) -> None:
        self._cache: dict[
            tuple[str, int], tuple[datetime, str, PolymarketNotice | None]
        ] = {}
        self._locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._event_cache: dict[str, tuple[datetime, dict | None]] = {}
        self._event_pending: dict[str, asyncio.Task] = {}
        self._reasons: dict[tuple[str, int], tuple[str, str]] = {}
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(3.5), follow_redirects=False,
                limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
            )
        return self._client

    async def aclose(self) -> None:
        pending = list(self._event_pending.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._event_pending.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _event(self, slug: str, *, now: datetime, refresh: bool) -> dict:
        cached = self._event_cache.get(slug)
        if not refresh and cached is not None and cached[0] > now:
            event = cached[1]
        else:
            pending = self._event_pending.get(slug)
            if pending is None:
                async def fetch() -> dict | None:
                    response = await self._http().get(f"{GAMMA}/events/slug/{slug}")
                    if response.status_code == 404:
                        result = None
                    else:
                        response.raise_for_status()
                        result = response.json()
                        if not isinstance(result, dict):
                            raise MarketUnavailable("event_shape")
                    # Cache only bounded raw metadata, then validate it for the
                    # caller's current window. Pre-open flags never stay frozen
                    # when a previously-next contract becomes current.
                    self._event_cache[slug] = (
                        now + timedelta(seconds=5), result,
                    )
                    while len(self._event_cache) > 48:
                        self._event_cache.pop(next(iter(self._event_cache)))
                    return result

                pending = asyncio.create_task(fetch())
                self._event_pending[slug] = pending

                def completed(task: asyncio.Task) -> None:
                    if self._event_pending.get(slug) is task:
                        self._event_pending.pop(slug, None)
                    if not task.cancelled():
                        task.exception()  # consume a failure after a caller timed out

                pending.add_done_callback(completed)
            # One timing-out board must not cancel the alert's shared request.
            event = await asyncio.shield(pending)
        if event is None:
            raise MarketUnavailable("event_not_found")
        return event

    @staticmethod
    def _window(event: dict, *, asset: str, horizon: int,
                start: datetime, now: datetime) -> PolymarketListedWindow:
        slug = market_slug(asset, horizon, start)
        if event.get("slug") != slug:
            raise MarketUnavailable("event_identity")
        if event.get("closed") is not False or event.get("archived") is True:
            raise MarketUnavailable("event_closed")
        markets = event.get("markets")
        if not isinstance(markets, list) or len(markets) != 1 or not isinstance(markets[0], dict):
            raise MarketUnavailable("market_count")
        market = markets[0]
        opens = _date(event.get("eventStartTime") or market.get("eventStartTime"))
        closes = _date(event.get("endDate") or market.get("endDate"))
        if opens != start or closes != start + timedelta(minutes=horizon):
            raise MarketUnavailable("window_mismatch")
        if market.get("closed") is not False:
            raise MarketUnavailable("market_closed")
        if _decode_array(market.get("outcomes")) != ["Up", "Down"]:
            raise MarketUnavailable("outcome_mapping")
        active = (
            event.get("active") is True and market.get("active") is True
            and market.get("acceptingOrders") is True
            and market.get("enableOrderBook") is True
            and start <= now < closes
        )
        target = None
        metadata = event.get("eventMetadata")
        if start <= now < closes and isinstance(metadata, dict):
            try:
                candidate = float(metadata.get("priceToBeat"))
                if math.isfinite(candidate) and candidate > 0:
                    target = candidate
            except (TypeError, ValueError, OverflowError):
                pass
        return PolymarketListedWindow(
            asset=asset, horizon=horizon, slug=slug, opens=opens, closes=closes,
            accepting_orders=active, price_to_beat=target,
        )

    @staticmethod
    def _reason(exc: Exception) -> str:
        if isinstance(exc, MarketUnavailable):
            return str(exc)
        if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
            return "request_timeout"
        if isinstance(exc, httpx.HTTPStatusError):
            return f"http_{exc.response.status_code}"
        return "invalid_metadata" if isinstance(exc, (ValueError, TypeError, KeyError)) else "http_error"

    async def listed(
        self, asset: str, horizon: int, opened: datetime, *,
        now: datetime | None = None, refresh: bool = False,
    ) -> PolymarketListedWindow | None:
        instant = (now or datetime.now(UTC)).astimezone(UTC)
        start = opened.astimezone(UTC)
        slug = market_slug(asset, horizon, start)
        if start.timestamp() % (horizon * 60) != 0:
            raise ValueError("Unaligned window")
        try:
            event = await asyncio.wait_for(
                self._event(slug, now=instant, refresh=refresh), timeout=READ_TIMEOUT,
            )
            return self._window(event, asset=asset, horizon=horizon, start=start,
                                now=now or datetime.now(UTC))
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, KeyError) as exc:
            _LOG.info(
                "Polymarket verified listing unavailable asset=%s horizon=%s reason=%s",
                asset, horizon, self._reason(exc),
            )
            return None

    async def window_board(
        self, asset: str, horizon: int, *, now: datetime | None = None,
        refresh: bool = False,
    ) -> PolymarketWindowBoard:
        stamp = (now or datetime.now(UTC)).astimezone(UTC)
        if asset not in ASSETS or horizon not in HORIZONS:
            raise ValueError("Choose BTC, ETH, SOL or XRP and 5m/15m")
        period = horizon * 60
        start = datetime.fromtimestamp(int(stamp.timestamp()) // period * period, tz=UTC)
        current, upcoming, quote = await asyncio.gather(
            self.listed(asset, horizon, start, now=now, refresh=refresh),
            self.listed(asset, horizon, start + timedelta(seconds=period),
                        now=now, refresh=refresh),
            self.market(asset, horizon, now=now, refresh=refresh, display_only=True),
        )
        reason = self._reasons.get((asset, horizon))
        reason_code = reason[1] if reason and reason[0] == market_slug(asset, horizon, stamp) else None
        # Check against the completed read, not the request's starting time.
        if current is None or quote is None or quote.slug != current.slug:
            quote = None
        elif not self._usable(quote, now=now or datetime.now(UTC), display_only=True):
            quote, reason_code = None, "book_stale"
        return PolymarketWindowBoard(
            checked_at=stamp, current=current, next_window=upcoming,
            current_quote=quote, quote_unavailable_reason=reason_code,
        )

    @staticmethod
    def _usable(notice: PolymarketNotice, *, now: datetime, display_only: bool) -> bool:
        return (
            -timedelta(seconds=5) <= now - notice.quote_at <= MAX_BOOK_AGE
            and notice.issued <= now < notice.expires
            and (display_only or notice.expires - now >= timedelta(seconds=MIN_SECONDS_LEFT))
        )

    async def market(
        self, asset: str, horizon: int, *, now: datetime | None = None,
        opened: datetime | None = None, refresh: bool = False,
        display_only: bool = False,
    ) -> PolymarketNotice | None:
        instant = (now or datetime.now(UTC)).astimezone(UTC)
        slug = market_slug(asset, horizon, opened or instant)
        key = (asset, horizon)
        started = time.monotonic()
        async with self._locks.setdefault(key, asyncio.Lock()):
            checked = now or datetime.now(UTC)
            cached = self._cache.get(key)
            if (
                not refresh and opened is None and cached is not None
                and cached[0] > checked and cached[1] == slug
            ):
                candidate = cached[2]
                if candidate is None:
                    return None
                if self._usable(candidate, now=checked, display_only=display_only):
                    return candidate
                # Do not refresh a display quote just to bypass alert timing.
                if candidate.expires - checked < timedelta(seconds=MIN_SECONDS_LEFT) and not display_only:
                    self._reasons[key] = (slug, "too_late")
                    return None
            try:
                # Validate one book snapshot for both callers. Eligibility for
                # a late-window alert is checked separately, never cached as
                # "no market" over an otherwise valid display-only snapshot.
                notice = await asyncio.wait_for(
                    self._lookup(slug=slug, asset=asset, horizon=horizon,
                                 now=checked, refresh=refresh or opened is not None),
                    timeout=READ_TIMEOUT,
                )
                self._reasons.pop(key, None)
            except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError) as exc:
                reason = self._reason(exc)
                self._reasons[key] = (slug, reason)
                _LOG.info(
                    "Polymarket read-only market unavailable asset=%s horizon=%s reason=%s request_ms=%d",
                    asset, horizon, reason, int((time.monotonic() - started) * 1000),
                )
                notice = None
            if opened is None:
                self._cache[key] = (checked + timedelta(seconds=5), slug, notice)
            completed = now or datetime.now(UTC)
            if notice is not None and self._usable(notice, now=completed, display_only=display_only):
                return notice
            if notice is not None:
                self._reasons[key] = (slug, "too_late" if not display_only else "book_stale")
            return None

    async def _lookup(
        self, *, slug: str, asset: str, horizon: int, now: datetime,
        refresh: bool = False,
    ) -> PolymarketNotice:
        event = await self._event(slug, now=now, refresh=refresh)
        start = datetime.fromtimestamp(int(slug.rsplit("-", 1)[-1]), tz=UTC)
        window = self._window(event, asset=asset, horizon=horizon, start=start, now=now)
        if not start <= now < window.closes:
            raise MarketUnavailable("window_not_open")
        if not window.accepting_orders:
            raise MarketUnavailable("orders_unavailable")
        market = event["markets"][0]
        tokens = _decode_array(market.get("clobTokenIds"))
        if len(tokens) != 2 or tokens[0] == tokens[1] or not all(
            x.isascii() and x.isdecimal() and len(x) > 30 for x in tokens
        ):
            raise MarketUnavailable("token_mapping")
        condition = market.get("conditionId")
        if not isinstance(condition, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", condition):
            raise MarketUnavailable("condition_id")
        async def book(token_id: str) -> dict:
            res = await self._http().get(f"{CLOB}/book", params={"token_id": token_id})
            res.raise_for_status()
            result = res.json()
            if (
                not isinstance(result, dict) or result.get("asset_id") != token_id
                or result.get("market") != condition
            ):
                raise MarketUnavailable("book_identity")
            return result

        tasks = [asyncio.create_task(book(token)) for token in tokens]
        try:
            up_book, down_book = await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        received = datetime.now(UTC)
        # Use the OLDER source book clock. A local HTTP receipt must not reset
        # source age to zero and permit 25 more seconds of stale delivery.
        quoted = min(_book_time(up_book, now=received), _book_time(down_book, now=received))
        if window.closes <= received:
            raise MarketUnavailable("window_not_open")
        return PolymarketNotice(
            asset=asset, horizon=horizon, issued=start, expires=window.closes,
            slug=slug, up_ask=_best_ask(up_book, now=received),
            down_ask=_best_ask(down_book, now=received), quote_at=quoted,
            up_average_ask=_average_ask(up_book, now=received),
            down_average_ask=_average_ask(down_book, now=received), received_at=received,
        )

    async def active(self, *, now: datetime | None = None) -> list[PolymarketNotice]:
        # Eight markets, at most 24 shared HTTP reads, never per subscriber.
        outcomes = await asyncio.gather(
            *(self.market(asset, horizon, now=now)
              for asset in ASSETS for horizon in HORIZONS), return_exceptions=True,
        )
        return [x for x in outcomes if isinstance(x, PolymarketNotice)]
