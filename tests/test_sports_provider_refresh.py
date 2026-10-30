from __future__ import annotations

import pytest

from parlay_bot.discord_app.bot import RivaLBot


class FakeCollector:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    async def refresh_if_due(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_all_providers_continue_after_quota_failure():
    bot = object.__new__(RivaLBot)
    bot.current_collector = FakeCollector(error=RuntimeError("quota protected"))
    bot.oddspapi_collector = FakeCollector(result="Updated sport 2: 5 observations")
    bot.the_odds_api_collector = FakeCollector(result="soccer_epl: 10 odds")
    status = await RivaLBot._refresh_all_sports(bot)
    assert "SportsGameOdds: unavailable (RuntimeError)" in status
    assert "OddsPapi: Updated sport 2" in status
    assert "The Odds API: soccer_epl" in status
    assert bot.oddspapi_collector.calls == 1
    assert bot.the_odds_api_collector.calls == 1
    assert bot.sports_refresh_checked_at is not None
