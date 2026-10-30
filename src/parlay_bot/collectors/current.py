from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from parlay_bot.ingestion.sportsgameodds import normalize_event
from parlay_bot.providers.errors import QuotaReserveReached
from parlay_bot.providers.sportsgameodds import SportsGameOddsClient
from parlay_bot.research.sports_results import SportsResultStore
from parlay_bot.storage.hot import RelationalHotStore

_STATE_KEY = "sportsgameodds:last_successful_refresh"
_NOTICE_KEY = "sportsgameodds:last_refresh_notice"
_LOG = logging.getLogger(__name__)


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
        self.results: SportsResultStore | None = None
        self.league_ids = league_ids
        self.bookmaker_ids = bookmaker_ids
        self.refresh_interval = refresh_interval
        self.event_limit = event_limit
        self.max_pages = max_pages
        self._lock = asyncio.Lock()
        self._league_cursor = 0
        self._forecast_semaphore = asyncio.Semaphore(4)
        self._forecast_inflight: set[str] = set()

    @staticmethod
    def _log_forecast_outcome(task: asyncio.Task[object]) -> None:
        if task.cancelled():
            return
        try:
            outcome = task.result()
        except Exception:  # noqa: BLE001
            _LOG.exception("RivaL optional sports forecast failed; odds intake unaffected")
        else:
            _LOG.info("RivaL sports forecast outcome=%s", outcome)

    async def _run_forecast(
        self, results: SportsResultStore, event: dict, *,
        observed_at: datetime,
    ) -> str:
        """One bounded forecast job: capped concurrency, deduped, shed loudly.

        Without a bound, every fetched event spawns a thread job that
        repeats a chronological backtest over many games; jobs pile up,
        contend for the database, and delay predictions toward kickoff.
        At most 4 run at once and at most 8 are in flight (running or
        queued); anything beyond that is shed with a distinct log line
        instead of silently queueing, and a duplicate event already in
        flight is skipped.
        """
        event_id = event.get("eventID") if isinstance(event, dict) else None
        if not isinstance(event_id, str) or not event_id:
            return await asyncio.to_thread(
                results.issue_forecast, event, observed_at=observed_at,
            )
        if (
            event_id in self._forecast_inflight
            or len(self._forecast_inflight) >= 8
        ):
            _LOG.info(
                "RivaL sports forecast shed event=%s inflight=%d",
                event_id, len(self._forecast_inflight),
            )
            return "shed"
        self._forecast_inflight.add(event_id)
        try:
            async with self._forecast_semaphore:
                return await asyncio.to_thread(
                    results.issue_forecast, event, observed_at=observed_at,
                )
        finally:
            self._forecast_inflight.discard(event_id)

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

    def _last_notice(self) -> str | None:
        """The last persisted refresh failure note, if any.

        A quota-reserve pause is the case that matters: it survives restarts
        until a later refresh succeeds, so the sports page can name the
        reason instead of only showing a stale "last checked" age.
        """
        raw = self.store.get_runtime_state(_NOTICE_KEY)
        if raw is None or not raw.strip():
            return None
        return raw

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

            # Rotate league requests so one busy sport cannot monopolize intake.
            league = self.league_ids[self._league_cursor % len(self.league_ids)]
            self._league_cursor += 1
            for _ in range(self.max_pages):
                stage_start = time.monotonic()
                try:
                    events, next_cursor, page_notice = await self.client.get_events(
                        league_ids=(league,),
                        bookmaker_ids=self.bookmaker_ids,
                        limit=self.event_limit,
                        cursor=cursor,
                    )
                except QuotaReserveReached as exc:
                    _LOG.warning("RivaL sports collection paused by provider quota reserve: %s", exc)
                    await asyncio.to_thread(
                        self.store.set_runtime_state, _NOTICE_KEY, str(exc),
                    )
                    return CurrentRefreshResult(
                        refreshed=False,
                        events_seen=events_seen,
                        observations_seen=observations_seen,
                        new_states=new_states,
                        repeated_states=repeated_states,
                        stale_states_ignored=stale_states_ignored,
                        pages_fetched=pages_fetched,
                        truncated=True,
                        notice=str(exc),
                    )
                _LOG.info('RivaL sports events fetched count=%d seconds=%.2f', len(events), time.monotonic() - stage_start)
                pages_fetched += 1
                events_seen += len(events)
                if page_notice:
                    notice = page_notice

                for event in events:
                    stage_start = time.monotonic()
                    board = await asyncio.to_thread(normalize_event, event)
                    _LOG.info('RivaL sports normalized seconds=%.2f', time.monotonic() - stage_start)
                    stage_start = time.monotonic()
                    result = await asyncio.to_thread(self.store.ingest_board, board)
                    _LOG.info('RivaL sports ingested observations=%d seconds=%.2f', result.observations_seen, time.monotonic() - stage_start)
                    if self.results is not None:
                        # Forecast research is optional and must not delay odds intake.
                        task = asyncio.create_task(
                            self._run_forecast(
                                self.results, event,
                                observed_at=datetime.now(UTC),
                            )
                        )
                        task.add_done_callback(self._log_forecast_outcome)
                    observations_seen += result.observations_seen
                    new_states += result.new_states
                    repeated_states += result.repeated_states
                    stale_states_ignored += result.stale_states_ignored

                cursor = next_cursor
                if cursor is None:
                    break

            now = datetime.now(UTC)
            await asyncio.to_thread(self.store.set_runtime_state, _STATE_KEY, now.isoformat())
            # A completed refresh clears any earlier pause note.
            await asyncio.to_thread(self.store.set_runtime_state, _NOTICE_KEY, "")

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
