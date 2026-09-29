from __future__ import annotations

import re
from typing import Self

import httpx

from parlay_bot.jurisdiction.states import normalize_state

_ZIP_PATTERN = re.compile(r"^\d{5}$")


class ZipLookupError(ValueError):
    """A ZIP code could not be resolved to one supported U.S. jurisdiction."""


class ZipStateResolver:
    """Resolve a user-entered ZIP to a state without persisting the ZIP."""

    def __init__(
        self,
        *,
        base_url: str = "https://api.zippopotam.us",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(10.0),
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def resolve(self, zip_code: str) -> str:
        cleaned = zip_code.strip()
        if _ZIP_PATTERN.fullmatch(cleaned) is None:
            raise ZipLookupError("ZIP code must contain exactly five digits")

        response = await self._client.get(f"/us/{cleaned}")
        if response.status_code == 404:
            raise ZipLookupError("ZIP code was not found")
        response.raise_for_status()

        payload = response.json()
        places = payload.get("places") if isinstance(payload, dict) else None
        if not isinstance(places, list) or not places:
            raise ZipLookupError("ZIP lookup returned no U.S. state")

        codes = {
            str(place.get("state abbreviation", "")).upper()
            for place in places
            if isinstance(place, dict) and place.get("state abbreviation")
        }
        if len(codes) != 1:
            raise ZipLookupError("ZIP lookup returned an ambiguous state")

        try:
            return normalize_state(codes.pop())
        except ValueError as exc:
            raise ZipLookupError("ZIP lookup returned an unsupported jurisdiction") from exc
