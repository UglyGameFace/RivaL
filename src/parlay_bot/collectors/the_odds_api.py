from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx

from parlay_bot.ingestion.models import MarketObservation, NormalizedOddsBoard
from parlay_bot.storage.hot import RelationalHotStore

_LOG = logging.getLogger(__name__)
_BASE = "https://api.the-odds-api.com/v4"


class TheOddsApiCollector:
    """Credit-bounded event odds intake using The Odds API v4."""

    def __init__(self, *, api_key: str, store: RelationalHotStore,
                 refresh_interval: timedelta = timedelta(minutes=30)) -> None:
        self.api_key = api_key
        self.store = store
        self.refresh_interval = refresh_interval
        self._lock = asyncio.Lock()
        self._sport_cursor = 0
        self._remaining_credits: int | None = None
        self._client = httpx.AsyncClient(timeout=25)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def refresh_if_due(self) -> str:
        async with self._lock:
            now = datetime.now(UTC)
            key = "the_odds_api:last_successful_refresh"
            raw = await asyncio.to_thread(self.store.get_runtime_state, key)
            if raw:
                try:
                    last = datetime.fromisoformat(raw)
                    if last.tzinfo and now - last < self.refresh_interval:
                        return "Skipped: not due"
                except ValueError:
                    pass

            if self._remaining_credits is not None and self._remaining_credits <= 10:
                return "Quota reserve protected (10 credits)"
            params = {"apiKey": self.api_key}
            sports = await self._client.get(f"{_BASE}/sports", params=params)
            sports.raise_for_status()
            eligible = [item["key"] for item in sports.json()
                        if item.get("active") and not item.get("has_outrights")]
            if not eligible:
                return "No active sports"
            sport = eligible[self._sport_cursor % len(eligible)]
            self._sport_cursor += 1

            # One sport, one region, three main markets. Never enumerate every
            # event/bookmaker at startup: this endpoint bills by market/region.
            odds = await self._client.get(
                f"{_BASE}/sports/{sport}/odds",
                params={**params, "regions": "us", "markets": "h2h,spreads,totals",
                        "oddsFormat": "decimal", "dateFormat": "iso"},
            )
            remaining = odds.headers.get("x-requests-remaining")
            if remaining is not None:
                try:
                    self._remaining_credits = int(remaining)
                except ValueError:
                    pass
            if odds.status_code in (401, 403, 429):
                return f"Provider unavailable ({odds.status_code}); remaining={remaining or 'unknown'}"
            odds.raise_for_status()
            events = odds.json()
            written = 0
            for event in events[:5]:
                try:
                    start = datetime.fromisoformat(event["commence_time"])
                    observations = []
                    for bookmaker in event.get("bookmakers", []):
                        for market in bookmaker.get("markets", []):
                            for outcome in market.get("outcomes", []):
                                price = outcome.get("price")
                                if not isinstance(price, (int, float)) or price <= 1:
                                    continue
                                changed = datetime.fromisoformat(
                                    market["last_update"]
                                )
                                observations.append(MarketObservation(
                                    provider="the_odds_api", fixture_id=event["id"],
                                    sport_id=sport, tournament_id=sport,
                                    bookmaker=bookmaker["key"], market_id=market["key"],
                                    outcome_id=outcome["name"],
                                    player_id="",
                                    market_name=market["key"], outcome_name=outcome["name"],
                                    line_value=outcome.get("point"),
                                    active=True, main_line=True,
                                    price_decimal=float(price), changed_at=changed,
                                    observed_at=now,
                                ))
                    if not observations:
                        continue
                    board = NormalizedOddsBoard(
                        provider="the_odds_api", fixture_id=event["id"],
                        sport_id=sport, tournament_id=sport,
                        start_time=start, updated_at=now,
                        participant1_name=event.get("home_team"),
                        participant2_name=event.get("away_team"),
                        observations=observations,
                    )
                    await asyncio.to_thread(self.store.ingest_board, board)
                    written += len(observations)
                except (KeyError, ValueError, TypeError) as exc:
                    _LOG.warning("The Odds API malformed event skipped: %s", exc)
            await asyncio.to_thread(self.store.set_runtime_state, key, now.isoformat())
            return f"{sport}: {written} odds, remaining credits={remaining or 'unknown'}"
