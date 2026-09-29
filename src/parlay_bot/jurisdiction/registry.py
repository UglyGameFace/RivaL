from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime

from parlay_bot.jurisdiction.models import SportsbookAvailability
from parlay_bot.jurisdiction.states import normalize_state


class JurisdictionRegistry:
    """Resolve books explicitly verified for a selected U.S. jurisdiction."""

    def __init__(self, entries: Iterable[SportsbookAvailability]) -> None:
        self._entries = tuple(entries)

    def eligible_books(self, state: str, *, on_date: date | None = None) -> list[str]:
        code = normalize_state(state)
        when = on_date or datetime.now(UTC).date()

        books: list[str] = []
        seen: set[str] = set()
        for entry in self._entries:
            if (
                entry.jurisdiction.upper() != code
                or not entry.online_available
                or (entry.effective_from is not None and entry.effective_from > when)
                or (entry.effective_until is not None and when > entry.effective_until)
                or entry.sportsbook in seen
            ):
                continue
            seen.add(entry.sportsbook)
            books.append(entry.sportsbook)

        return books

    def has_verified_online_books(self, state: str, *, on_date: date | None = None) -> bool:
        return bool(self.eligible_books(state, on_date=on_date))
