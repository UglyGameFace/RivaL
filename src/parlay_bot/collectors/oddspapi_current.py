from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from parlay_bot.ingestion.oddspapi import normalize_current_odds
from parlay_bot.providers.errors import ProviderQuotaExhausted, QuotaReserveReached
from parlay_bot.providers.oddspapi import OddsPapiClient
from parlay_bot.storage.hot import RelationalHotStore

_LOG = logging.getLogger(__name__)


class OddsPapiCurrentCollector:
    """Bounded shared live intake, independent of SportsGameOdds quotas."""

    def __init__(self, *, client: OddsPapiClient, store: RelationalHotStore,
                 refresh_interval: timedelta = timedelta(minutes=10)) -> None:
        self.client = client
        self.store = store
        self.refresh_interval = refresh_interval
        self._lock = asyncio.Lock()
        self._sport_cursor = 0
        self._fixture_cursor = 0

    async def refresh_if_due(self) -> str:
        async with self._lock:
            now = datetime.now(UTC)
            key = "oddspapi:last_successful_refresh"
            raw = await asyncio.to_thread(self.store.get_runtime_state, key)
            if raw:
                try:
                    last = datetime.fromisoformat(raw)
                    if last.tzinfo and now - last < self.refresh_interval:
                        return "Skipped: not due"
                except ValueError:
                    pass
            try:
                account = await self.client.get_account()
                if not account.sport_ids:
                    return "No sports entitled by OddsPapi subscription"
                books = tuple(slug for slug, entitlement in account.bookmakers.items()
                              if entitlement.has_live_odds)
                if not books:
                    return "No entitled OddsPapi bookmakers"
                sport = account.sport_ids[self._sport_cursor % len(account.sport_ids)]
                fixtures = await self.client.get_fixtures(
                    sport_id=sport, from_time=now,
                    to_time=now + timedelta(days=2),
                    has_odds=True, bookmakers=books[:3],
                )
                self._sport_cursor += 1
                eligible = [f for f in fixtures if f.has_odds and f.start_time > now]
                if not eligible:
                    return f"No upcoming odds fixtures for sport {sport}"
                fixture = eligible[self._fixture_cursor % len(eligible)]
                self._fixture_cursor += 1
                payload = await self.client.get_odds(fixture.fixture_id, bookmakers=books[:3])
                board = await asyncio.to_thread(normalize_current_odds, payload)
                if not board.observations:
                    return "OddsPapi returned no usable selections"
                result = await asyncio.to_thread(self.store.ingest_board, board)
                await asyncio.to_thread(self.store.set_runtime_state, key, now.isoformat())
                return (f"Updated sport {sport}: {result.observations_seen} observations "
                        f"for fixture {fixture.fixture_id}")
            except (QuotaReserveReached, ProviderQuotaExhausted) as exc:
                _LOG.warning("OddsPapi intake paused to protect quota: %s", exc)
                return f"Quota protected: {exc}"
