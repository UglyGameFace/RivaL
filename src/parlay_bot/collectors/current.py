from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from parlay_bot.ingestion.sportsgameodds import normalize_event
from parlay_bot.providers.sportsgameodds import SportsGameOddsClient
from parlay_bot.storage.hot import RelationalHotStore

_STATE_KEY = "sportsgameodds:last_successful_refresh"


@dataclass(frozen=True)
class CurrentRefreshResult:
    refreshed: bool
    events_seen: int
    observations_seen: int
    new_states: int
    repeated_states: int
    stale_states_ignored: int
    pages_fetched: int
    truncated: bool
    notice: str | None = None


class CurrentBoardCollector:
    """Refresh one shared current board instead of polling once per Discord user."""

    def __init__(
        self,
        *,
        client: SportsGameOddsClient,
        store: RelationalHotStore,
        league_ids: tuple[str, ...] = ("NBA", "NFL"),
        bookmaker_ids: tuple[str, ...] = ("draftkings", "fanduel"),
        refresh_interval: timedelta = timedelta(minutes=10),
        event_limit: int = 25,
        max_pages: int = 1,
    ) -> None:
        if not league_ids:
            raise ValueError("At least one league is required")
        if not bookmaker_ids:
            raise ValueError("At least one bookmaker is required")
        if refresh_interval <= timedelta(0):
            raise ValueError("refresh_interval must be positive")
        if event_limit < 1:
            raise ValueError("event_limit must be positive")
        if max_pages < 1:
            raise ValueError("max_pages must be positive")

        self.client = client
        self.store = store
        self.league_ids = league_ids
        self.bookmaker_ids = bookmaker_ids
        self.refresh_interval = refresh_interval
        self.event_limit = event_limit
        self.max_pages = max_pages
        self._lock = asyncio.Lock()

    def _last_refresh(self) -> datetime | None:
        raw = self.store.get_runtime_state(_STATE_KEY)
        if raw is None:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC)

    def is_due(self, *, now: datetime | None = None) -> bool:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        last = self._last_refresh()
        return last is None or current - last >= self.refresh_interval

    async def refresh_if_due(self, *, force: bool = False) -> CurrentRefreshResult:
        async with self._lock:
            if not force and not self.is_due():
                return CurrentRefreshResult(
                    refreshed=False,
                    events_seen=0,
                    observations_seen=0,
                    new_states=0,
                    repeated_states=0,
                    stale_states_ignored=0,
                    pages_fetched=0,
                    truncated=False,
                )

            cursor: str | None = None
            events_seen = 0
            observations_seen = 0
            new_states = 0
            repeated_states = 0
            stale_states_ignored = 0
            pages_fetched = 0
            notice: str | None = None

            for _ in range(self.max_pages):
                events, next_cursor, page_notice = await self.client.get_events(
                    league_ids=self.league_ids,
                    bookmaker_ids=self.bookmaker_ids,
                    limit=self.event_limit,
                    cursor=cursor,
                )
                pages_fetched += 1
                events_seen += len(events)
                if page_notice:
                    notice = page_notice

                for event in events:
                    board = normalize_event(event)
                    result = self.store.ingest_board(board)
                    observations_seen += result.observations_seen
                    new_states += result.new_states
                    repeated_states += result.repeated_states
                    stale_states_ignored += result.stale_states_ignored

                cursor = next_cursor
                if cursor is None:
                    break

            now = datetime.now(UTC)
            self.store.set_runtime_state(_STATE_KEY, now.isoformat())

            return CurrentRefreshResult(
                refreshed=True,
                events_seen=events_seen,
                observations_seen=observations_seen,
                new_states=new_states,
                repeated_states=repeated_states,
                stale_states_ignored=stale_states_ignored,
                pages_fetched=pages_fetched,
                truncated=cursor is not None,
                notice=notice,
            )
