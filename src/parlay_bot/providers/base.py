from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from parlay_bot.domain.models import AccountSnapshot, Fixture


class OddsProvider(Protocol):
    async def get_account(self, *, force: bool = False) -> AccountSnapshot: ...

    async def get_fixtures(
        self,
        *,
        sport_id: int,
        from_time: datetime,
        to_time: datetime,
        status_id: int | None = None,
        has_odds: bool | None = None,
        bookmakers: Sequence[str] | None = None,
    ) -> list[Fixture]: ...

    async def get_odds(
        self,
        fixture_id: str,
        *,
        bookmakers: Sequence[str] | None = None,
    ) -> dict: ...

    async def get_historical_odds(
        self,
        fixture_id: str,
        *,
        bookmakers: Sequence[str],
        etag: str | None = None,
    ) -> tuple[dict | None, str | None, bool]: ...
