"""Bot Performance at 15 and 60 minutes shows the same sections as at 5.

Nov 10 video: the 15-minute page had no test-model rows while the
5-minute page loaded minutes earlier showed them. Those readers ran live on every load, against
the 5-minute cycle, under a 7 s limit, and a section whose read lost was
left off without a word. They are now cached and refreshed in the
background for every coin and timeframe (the trial table is one of them),
and a read that still fails says so on the page.
"""
from __future__ import annotations

import pytest

from parlay_bot.discord_app import home as home_module
from parlay_bot.discord_app.home import (
    _cached_page_read,
    warm_page_reads,
)
from tests.test_performance_overview import (
    _all_fields,
    _close,
    _make_home,
    _patch_live_reads,
)


class _Market:
    store = None


def test_warm_up_reads_every_reader_for_every_coin_and_timeframe(monkeypatch):
    seen = []

    def reader(name):
        def read(market, asset, horizon):
            seen.append((name, asset, horizon))
            return {"issued": 0}
        read.__name__ = name
        return read

    readers = tuple(reader(f"r{i}") for i in range(7))
    monkeypatch.setattr(home_module, "_page_readers", lambda: readers)
    assert warm_page_reads(_Market()) == 4 * 3 * 7
    assert {(asset, horizon) for _, asset, horizon in seen} == {
        (asset, horizon) for asset in ("BTC", "ETH", "SOL", "XRP") for horizon in (5, 15, 60)}


def test_a_cached_read_is_reused_until_refreshed_and_failures_are_never_kept():
    market, calls = _Market(), []

    def read(market, asset, horizon):
        calls.append(horizon)
        if len(calls) == 1:
            raise TimeoutError("slow")
        return {"issued": len(calls)}

    with pytest.raises(TimeoutError):
        _cached_page_read(read, market, "BTC", 15)
    # The failure was not cached: the next request reads again.
    assert _cached_page_read(read, market, "BTC", 15) == {"issued": 2}
    assert _cached_page_read(read, market, "BTC", 15) == {"issued": 2}
    # Each timeframe has its own entry.
    assert _cached_page_read(read, market, "BTC", 60) == {"issued": 3}
    # The background refresh replaces it.
    assert _cached_page_read(read, market, "BTC", 15, refresh=True) == {"issued": 4}
    assert calls == [15, 15, 60, 15]


def test_two_readers_never_share_an_entry():
    market = _Market()
    assert _cached_page_read(lambda *_: "a", market, "BTC", 15) == "a"
    assert _cached_page_read(lambda *_: "b", market, "BTC", 15) == "b"


@pytest.mark.asyncio
@pytest.mark.parametrize("horizon", [15, 60])
async def test_a_trial_read_that_fails_is_shown_not_dropped(tmp_path, monkeypatch, horizon):
    bot, home = _make_home(tmp_path, section="details")
    home.horizon = horizon

    def slow(*_):
        raise TimeoutError("lost the race with the 5-minute cycle")

    _patch_live_reads(monkeypatch)
    monkeypatch.setattr(home_module, "_trial_board_record", slow)
    try:
        embeds = await home.render_embeds()
        assert ("⚠️ Couldn't load the trial table right now. Press Refresh"
                in embeds[1].description)
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_nothing_is_flagged_when_every_read_worked(tmp_path, monkeypatch):
    bot, home = _make_home(tmp_path, section="details")
    home.horizon = 15
    _patch_live_reads(monkeypatch)
    try:
        embeds = await home.render_embeds()
        page = "\n".join(
            [*(e.description or "" for e in embeds),
             *_all_fields(embeds).values()])
        assert "Couldn't load" not in page and "couldn't check" not in page
    finally:
        await _close(bot, home)


