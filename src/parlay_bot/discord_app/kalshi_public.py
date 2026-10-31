"""Read-only Kalshi crypto contract discovery from documented public REST data.

This is a separate venue catalog, NOT a source-matched Polymarket 5m/15m
prediction feed. Never blend contracts with different oracle or strike rules.
No API keys, accounts, scraping, orders, or synthetic odds.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

_LOG = logging.getLogger(__name__)
_ROOT = "https://external-api.kalshi.com/trade-api/v2"
_SYMBOLS = {
    "BTC": (r"\bBTC\b", r"\bBITCOIN\b"),
    "ETH": (r"\bETH\b", r"\bETHEREUM\b"),
    "SOL": (r"\bSOL\b", r"\bSOLANA\b"),
    "XRP": (r"\bXRP\b",),
}
# Series may include several tickers sharing a theme. Only the official
# discovery response determines what exists. Avoid hardcoded market IDs.
_MAX_SERIES = 4
_CACHE_TTL = timedelta(seconds=35)


def _date(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is not None:
            return parsed.astimezone(UTC)
    except ValueError:
        pass
    return None


def _ask(value: object) -> float | None:
    try:
        parsed = float(value) if isinstance(value, (str, int, float)) else float("nan")
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or not 0 < parsed < 1:
        return None
    return parsed


def matching_series(payload: object, *, asset: str) -> tuple[str, ...]:
    if asset not in _SYMBOLS:
        raise ValueError("Unknown Kalshi asset")
    if not isinstance(payload, dict) or not isinstance(payload.get("series"), list):
        raise TypeError("Unverified Kalshi series response")
    selected = []
    for row in payload["series"]:
        if not isinstance(row, dict):
            continue
        title = row.get("title") or row.get("yes_sub_title")
        ticker = row.get("ticker")
        categories = row.get("categories")
        if (
            not isinstance(title, str) or not isinstance(ticker, str)
            or not re.fullmatch(r"[A-Z0-9-]{2,80}", ticker)
            or (
                row.get("category") != "Crypto"
                and (not isinstance(categories, list) or "Crypto" not in categories)
            )
        ):
            continue
        name = title.upper()
        if any(re.search(pattern, name) for pattern in _SYMBOLS[asset]):
            selected.append(ticker)
    return tuple(dict.fromkeys(selected))[:_MAX_SERIES]


@dataclass(frozen=True)
class KalshiMarket:
    ticker: str
    title: str
    close_time: datetime
    yes_ask: float | None
    no_ask: float | None
    rules: str
    series_ticker: str | None = None


@dataclass(frozen=True)
class KalshiSnapshot:
    asset: str
    checked_at: datetime
    matched_series: int
    markets: tuple[KalshiMarket, ...]


def parse_markets(
    payload: object, *, now: datetime, series_ticker: str | None = None,
) -> tuple[KalshiMarket, ...]:
    if not isinstance(payload, dict) or not isinstance(payload.get("markets"), list):
        raise TypeError("Unverified Kalshi markets response")
    entries = []
    for row in payload["markets"]:
        if not isinstance(row, dict) or row.get("status") not in ("active", "open"):
            continue
        ticker = row.get("ticker")
        title = row.get("title")
        end = _date(row.get("close_time"))
        if (
            not isinstance(ticker, str)
            or not re.fullmatch(r"[A-Z0-9-]{2,110}", ticker)
            or not isinstance(title, str) or not title.strip()
            or end is None or end <= now
        ):
            continue
        rules = row.get("rules_primary")
        entries.append(KalshiMarket(
            ticker=ticker, title=title[:170], close_time=end,
            yes_ask=_ask(row.get("yes_ask_dollars")),
            no_ask=_ask(row.get("no_ask_dollars")),
            # Never hash only the first characters of a settlement rule.
            rules=(
                rules if isinstance(rules, str) and 0 < len(rules) <= 16000
                else "Rules not verified"
            ),
            series_ticker=series_ticker,
        ))
    return tuple(entries)


class KalshiCryptoBrowser:
    """Bounded on-demand public catalog, not a second automatic alert worker."""

    def __init__(
        self, *, client: httpx.AsyncClient | None = None, forward=None,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(3.0), follow_redirects=False,
        )
        self._owned_client = client is None
        self._forward = forward
        self._study_tasks: set[asyncio.Task] = set()
        self._cache: dict[str, tuple[datetime, KalshiSnapshot]] = {}
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        for task in tuple(self._study_tasks):
            task.cancel()
        if self._study_tasks:
            await asyncio.gather(*self._study_tasks, return_exceptions=True)
        if self._owned_client:
            await self._client.aclose()

    async def _study(self, snapshot: KalshiSnapshot) -> None:
        """Bounded independent Kalshi research; never block a Discord response."""
        if self._forward is None:
            return
        try:
            for market in snapshot.markets:
                await asyncio.to_thread(
                    self._forward.observe, asset=snapshot.asset,
                    market=market, at=snapshot.checked_at,
                )
            pending = await asyncio.to_thread(
                self._forward.pending, now=datetime.now(UTC), limit=2,
            )
            for ticker in pending:
                # Reserve the next check BEFORE network access. A contract that
                # has closed but is not yet settled must not starve the others.
                checked_at = datetime.now(UTC)
                if not await asyncio.to_thread(
                    self._forward.mark_settlement_check,
                    ticker=ticker, at=checked_at,
                ):
                    continue
                try:
                    response = await self._client.get(_ROOT + f"/markets/{ticker}")
                    response.raise_for_status()
                    await asyncio.to_thread(
                        self._forward.settle, ticker=ticker,
                        payload=response.json(), checked_at=datetime.now(UTC),
                    )
                except (httpx.HTTPError, TypeError, ValueError):
                    _LOG.warning(
                        "Kalshi settlement not yet verifiable ticker=%s", ticker,
                    )
        except (ValueError, TypeError, httpx.HTTPError, RuntimeError):
            _LOG.exception("Kalshi shadow evidence unavailable; browser stays read-only")

    async def contract_research(
        self, *, asset: str, market: KalshiMarket, observed_at: datetime,
    ) -> dict | None:
        """Issue/fetch immutable research without borrowing another venue's odds."""
        if self._forward is None:
            return None

        def read() -> dict | None:
            # The background recorder may not have run yet. The first valid
            # quote wins, and the store's unique ticker prevents re-issuance.
            self._forward.observe(asset=asset, market=market, at=observed_at)
            return self._forward.contract_preview(asset=asset, market=market)

        return await asyncio.to_thread(read)

    async def browse(self, asset: str, *, refresh: bool = False) -> KalshiSnapshot:
        if asset not in _SYMBOLS:
            raise ValueError("Unknown Kalshi crypto asset")
        async with self._lock:
            now = datetime.now(UTC)
            cached = self._cache.get(asset)
            if cached and cached[0] > now and not refresh:
                return cached[1]
            try:
                resp = await self._client.get(_ROOT + "/series", params={"category": "Crypto"})
                resp.raise_for_status()
                series = matching_series(resp.json(), asset=asset)
                # No market listing means no invented venue / timeframe match.
                async def get_markets(ticker: str) -> tuple[KalshiMarket, ...]:
                    response = await self._client.get(
                        _ROOT + "/markets",
                        params={"series_ticker": ticker, "status": "open", "limit": 50},
                    )
                    response.raise_for_status()
                    return parse_markets(response.json(), now=now, series_ticker=ticker)
                responses = await asyncio.gather(*(get_markets(ticker) for ticker in series))
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                _LOG.warning("Kalshi crypto discovery unavailable asset=%s error=%s",
                             asset, type(exc).__name__)
                raise ValueError("Kalshi public market data unavailable") from exc
            unique = {m.ticker: m for group in responses for m in group}
            markets = tuple(sorted(unique.values(), key=lambda m: m.close_time))[:4]
            observed_at = datetime.now(UTC)
            snapshot = KalshiSnapshot(
                asset=asset, checked_at=observed_at,
                matched_series=len(series), markets=markets,
            )
            self._cache[asset] = (observed_at + _CACHE_TTL, snapshot)
            if self._forward is not None:
                task = asyncio.create_task(self._study(snapshot))
                self._study_tasks.add(task)
                task.add_done_callback(self._study_tasks.discard)
            return snapshot
