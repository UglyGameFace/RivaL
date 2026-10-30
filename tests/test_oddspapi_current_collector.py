from __future__ import annotations

from types import SimpleNamespace

import pytest

from parlay_bot.collectors.oddspapi_current import OddsPapiCurrentCollector
from parlay_bot.storage.hot import SQLiteHotStore


class FakeOddsPapi:
    def __init__(self):
        self.calls = 0

    async def get_account(self):
        return SimpleNamespace(
            sport_ids=[1, 2],
            bookmakers={"book": SimpleNamespace(has_live_odds=True),
                        "unlicensed": SimpleNamespace(has_live_odds=False)},
        )

    async def get_fixtures(self, **kwargs):
        self.calls += 1
        assert kwargs["bookmakers"] == ("book",)
        return []

    async def get_odds(self, *args, **kwargs):
        raise AssertionError("No fixtures means no paid odds fetch")


@pytest.mark.asyncio
async def test_oddspapi_respects_entitlements_and_skips_empty_fixture_board(tmp_path):
    client = FakeOddsPapi()
    collector = OddsPapiCurrentCollector(
        client=client, store=SQLiteHotStore(tmp_path / "odds.db"),
    )
    result = await collector.refresh_if_due()
    assert "No upcoming odds fixtures" in result
    assert client.calls == 1


@pytest.mark.asyncio
async def test_oddspapi_rotates_sports_without_spending_on_empty_odds(tmp_path):
    client = FakeOddsPapi()
    collector = OddsPapiCurrentCollector(
        client=client, store=SQLiteHotStore(tmp_path / "odds.db"),
    )
    assert "sport 1" in await collector.refresh_if_due()
    assert "sport 2" in await collector.refresh_if_due()
    assert client.calls == 2
