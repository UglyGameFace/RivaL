"""Owner-only, on-demand CDNA public quote reader.

This personal-use quote display is deliberately isolated from RivaL model
training, prompts, predictions, PostgreSQL, and Drive. The public Crypto.com
market-data EULA separately restricts AI processing even for a single user.
A CDNA trading symbol is NOT proof that a DraftKings contract is identical.
"""
from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

_ROOT = "https://data-api.crypto.com/api/v1/predictions"
_SYMBOL = re.compile(r"[A-Za-z0-9_.:-]{2,100}\Z")
_MIN_INTERVAL = timedelta(seconds=12)
_STALE = timedelta(seconds=90)


class CDNAUnavailable(ValueError):
    """Provider quote cannot safely be represented as fresh evidence."""


@dataclass(frozen=True)
class CDNAPrice:
    symbol: str
    title: str
    bid: float
    ask: float
    updated_at: datetime
    checked_at: datetime
    status: str

    @property
    def fresh(self) -> bool:
        return (
            self.status.lower() in ("active", "open", "trading")
            and timedelta(seconds=-5)
            <= self.checked_at - self.updated_at <= _STALE
        )


def parse_cdna_price(payload: object, *, requested: str, checked_at: datetime) -> CDNAPrice:
    """Validate ONLY published quote response fields, never infer a DK match."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise CDNAUnavailable("CDNA quote response is missing data")
    entry = payload["data"]
    symbol = entry.get("symbol")
    if symbol != requested or not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise CDNAUnavailable("CDNA returned a different or invalid contract")
    title = entry.get("title")
    status = entry.get("status")
    if not isinstance(title, str) or not title.strip() or not isinstance(status, str):
        raise CDNAUnavailable("CDNA contract identity is incomplete")
    try:
        bid = float(entry["bid"])
        ask = float(entry["ask"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise CDNAUnavailable("CDNA quote has no verified bid and ask") from exc
    if not (math.isfinite(bid) and math.isfinite(ask) and 0 <= bid <= ask <= 1):
        raise CDNAUnavailable("CDNA bid/ask are invalid or crossed")
    source_time = entry.get("updated_at")
    if not isinstance(source_time, str):
        raise CDNAUnavailable("CDNA quote has no source update time")
    try:
        at = datetime.fromisoformat(source_time)
    except ValueError as exc:
        raise CDNAUnavailable("CDNA quote update time is invalid") from exc
    if at.tzinfo is None or at.utcoffset() is None:
        raise CDNAUnavailable("CDNA quote time has no UTC offset")
    return CDNAPrice(
        symbol=symbol, title=title[:180], bid=bid, ask=ask,
        updated_at=at.astimezone(UTC), checked_at=checked_at, status=status,
    )


class CDNAPersonalReader:
    """One-shot private lookup; no background worker, learning, AI, or archive."""

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(3.5), follow_redirects=False,
        )
        self._owned = client is None
        self._last: dict[str, datetime] = {}
        self._blocked_until: datetime | None = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def search_contracts(self, keyword: str) -> tuple[tuple[str, str, str], ...]:
        """Discover trading symbols from actual official event contract listings."""
        if (
            not isinstance(keyword, str) or len(keyword.strip()) < 2
            or len(keyword) > 40 or not re.fullmatch(r"[A-Za-z0-9 ]{2,40}", keyword)
        ):
            raise CDNAUnavailable("Search using a coin name or ticker")
        now = datetime.now(UTC)
        async with self._lock:
            if self._blocked_until is not None and now < self._blocked_until:
                raise CDNAUnavailable("CDNA access is paused")
            last = self._last.get("search:" + keyword.upper())
            if last is not None and now - last < _MIN_INTERVAL:
                raise CDNAUnavailable("Recent search; retry after 12 seconds")
            self._last["search:" + keyword.upper()] = now
            try:
                response = await self._client.get(
                    _ROOT + "/events/search",
                    params={"q": keyword.strip(), "limit": 5},
                )
                if response.status_code in (401, 403, 429):
                    self._blocked_until = now + timedelta(minutes=5)
                    raise CDNAUnavailable("CDNA search is denied or rate limited")
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(
                    payload.get("data"), list,
                ):
                    raise CDNAUnavailable("CDNA event search response invalid")
                matches: list[tuple[str, str, str]] = []
                for event in payload["data"][:3]:
                    if not isinstance(event, dict):
                        continue
                    event_id = event.get("id")
                    title = event.get("title")
                    if (
                        not isinstance(event_id, str)
                        or re.fullmatch(
                            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
                            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                            event_id,
                        ) is None
                        or not isinstance(title, str) or not title.strip()
                    ):
                        continue
                    contracts = await self._client.get(
                        _ROOT + f"/events/{event_id}/contracts",
                    )
                    if contracts.status_code in (401, 403, 429):
                        self._blocked_until = now + timedelta(minutes=5)
                        raise CDNAUnavailable("CDNA contract list access denied")
                    contracts.raise_for_status()
                    body = contracts.json()
                    if not isinstance(body, dict) or not isinstance(
                        body.get("data"), list,
                    ):
                        raise CDNAUnavailable("CDNA contract list invalid")
                    for row in body["data"][:10]:
                        if not isinstance(row, dict):
                            continue
                        symbol = row.get("symbol")
                        contract_title = row.get("title")
                        if (
                            isinstance(symbol, str) and _SYMBOL.fullmatch(symbol)
                            and isinstance(contract_title, str)
                            and contract_title.strip()
                        ):
                            matches.append(
                                (symbol, contract_title[:110], title[:125]),
                            )
                        if len(matches) >= 6:
                            break
                    if len(matches) >= 6:
                        break
                return tuple(matches)
            except CDNAUnavailable:
                raise
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                raise CDNAUnavailable("CDNA contract discovery unavailable") from exc

    async def discover_coin(
        self, asset: str,
    ) -> tuple[tuple[str, str, str, CDNAPrice | None], ...]:
        """One deliberate owner action: find and inspect a bounded number of
        CDNA candidates. Never identify them as DraftKings contracts.

        Coin-only matching cannot prove exact event, strike, expiry, oracle,
        venue execution price, or fees. Those remain unverified.
        """
        names = {
            "BTC": "Bitcoin", "ETH": "Ethereum",
            "SOL": "Solana", "XRP": "XRP",
        }
        if asset not in names:
            raise CDNAUnavailable("Choose BTC, ETH, SOL or XRP first")
        results = await self.search_contracts(asset)
        if not results:
            results = await self.search_contracts(names[asset])
        # Require an exact coin term instead of substring guessing:
        # BTC must never match ABTC2, and ETH must never match BETH.
        tokens = {"BTC": ("BTC", "BITCOIN"), "ETH": ("ETH", "ETHEREUM"),
                  "SOL": ("SOL", "SOLANA"), "XRP": ("XRP",)}
        aliases = tokens[asset]
        unique: list[tuple[str, str, str]] = []
        for symbol, title, event in results:
            # Match the provider's machine-readable symbol, NOT the event
            # headline. Every contract in a Bitcoin event may have a symbol
            # for a different asset or a negated/unrelated proposition.
            if not any(re.search(
                rf"(?<![A-Z0-9]){word}(?![A-Z0-9])", symbol.upper(),
            ) for word in aliases):
                continue
            if not any(row[0] == symbol for row in unique):
                unique.append((symbol, title, event))
            if len(unique) >= 2:
                break
        found: list[tuple[str, str, str, CDNAPrice | None]] = []
        for symbol, title, event in unique:
            try:
                quote = await self.quote(symbol)
            except CDNAUnavailable:
                quote = None
            found.append((symbol, title, event, quote))
        return tuple(found)

    async def quote(self, symbol: str) -> CDNAPrice:
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
            raise CDNAUnavailable("Enter a real CDNA contract trading symbol")
        async with self._lock:
            now = datetime.now(UTC)
            if self._blocked_until is not None and now < self._blocked_until:
                raise CDNAUnavailable("CDNA access is paused; no fresh quote")
            last = self._last.get(symbol)
            if last is not None and now - last < _MIN_INTERVAL:
                raise CDNAUnavailable("A recent lookup was made; retry after 12 seconds")
            self._last[symbol] = now
            if len(self._last) > 64:
                self._last = {k: v for k, v in self._last.items()
                              if now - v < timedelta(minutes=2)}
            try:
                resp = await self._client.get(
                    _ROOT + f"/contracts/{symbol}/price",
                )
            except httpx.HTTPError as exc:
                self._blocked_until = now + timedelta(seconds=30)
                raise CDNAUnavailable("CDNA quote network unavailable") from exc
            if resp.status_code in (401, 403, 429):
                self._blocked_until = now + timedelta(minutes=5)
                raise CDNAUnavailable(
                    "CDNA access denied or rate limited; no quote is available",
                )
            try:
                resp.raise_for_status()
                price = parse_cdna_price(
                    resp.json(), requested=symbol, checked_at=datetime.now(UTC),
                )
            except (httpx.HTTPError, TypeError, ValueError) as exc:
                raise CDNAUnavailable("CDNA returned no usable current quote") from exc
            return price
