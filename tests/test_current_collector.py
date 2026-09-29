from __future__ import annotations

from datetime import timedelta

import pytest

from parlay_bot.collectors.current import CurrentBoardCollector
from parlay_bot.storage.hot import SQLiteHotStore
from tests.sgo_samples import sample_sgo_event


class FakeSportsGameOddsClient:
    def __init__(self) -> None:
        self.calls = 0

    async def get_events(
        self,
        *,
        league_ids,
        bookmaker_ids=None,
        limit=25,
        cursor=None,
    ):
        self.calls += 1
        assert tuple(league_ids) == ("NBA", "NFL")
        assert tuple(bookmaker_ids or ()) == ("draftkings", "fanduel")
        assert limit == 25
        assert cursor is None
        return [sample_sgo_event()], None, None


@pytest.mark.asyncio
async def test_shared_collector_reuses_persisted_refresh_gate(tmp_path) -> None:
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    client = FakeSportsGameOddsClient()
    collector = CurrentBoardCollector(
        client=client,
        store=store,
        refresh_interval=timedelta(minutes=10),
    )

    first = await collector.refresh_if_due()
    second = await collector.refresh_if_due()

    assert first.refreshed is True
    assert first.events_seen == 1
    assert first.observations_seen == 6
    assert first.pages_fetched == 1
    assert second.refreshed is False
    assert client.calls == 1
    assert len(store.current_for_fixture("sgo-event-1")) == 6


@pytest.mark.asyncio
async def test_refresh_gate_survives_new_collector_instance(tmp_path) -> None:
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    first_client = FakeSportsGameOddsClient()
    first = CurrentBoardCollector(client=first_client, store=store)
    await first.refresh_if_due()

    second_client = FakeSportsGameOddsClient()
    second = CurrentBoardCollector(client=second_client, store=store)
    result = await second.refresh_if_due()

    assert result.refreshed is False
    assert second_client.calls == 0


@pytest.mark.asyncio
async def test_force_refresh_bypasses_refresh_gate(tmp_path) -> None:
    store = SQLiteHotStore(tmp_path / "rival.sqlite")
    client = FakeSportsGameOddsClient()
    collector = CurrentBoardCollector(client=client, store=store)

    await collector.refresh_if_due()
    forced = await collector.refresh_if_due(force=True)

    assert forced.refreshed is True
    assert client.calls == 2
    assert forced.repeated_states == 6
