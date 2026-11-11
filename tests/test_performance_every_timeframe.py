"""Bot Performance at 15 and 60 minutes shows the same sections as at 5.

Nov 10 video: the 15-minute page had no test-model rows and "RivaL
Intelligence: couldn't check right now", while the 5-minute page loaded
minutes earlier showed both. Those readers ran live on every load, against
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
    _intelligence_status,
    warm_page_reads,
)
from tests.test_performance_overview import (
    HEALTH,
    _close,
    _fields,
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


def test_the_ai_review_call_is_live_and_only_its_grades_are_cached(monkeypatch):
    from parlay_bot.perpetuals import intelligence

    latest_calls, report_calls = [], []

    class Ledger:
        def __init__(self, store):
            pass

        def latest(self, *, asset):
            latest_calls.append(asset)
            return {"stances": {15: ("follow", "up")}}

        def report(self, *, asset, horizon):
            report_calls.append(horizon)
            return {"followed": 4}

    monkeypatch.setattr(intelligence, "IntelligenceLedger", Ledger)
    market = _Market()
    for _ in range(3):
        assert _intelligence_status(market, "BTC", 15)["report"] == {"followed": 4}
    assert latest_calls == ["BTC"] * 3   # the current call is never stale
    assert report_calls == [15]          # the slow 7-day grading ran once


@pytest.mark.asyncio
@pytest.mark.parametrize("horizon", [15, 60])
async def test_a_trial_read_that_fails_is_shown_not_dropped(tmp_path, monkeypatch, horizon):
    bot, home = _make_home(tmp_path)
    home.horizon = horizon

    def slow(*_):
        raise TimeoutError("lost the race with the 5-minute cycle")

    _patch_live_reads(monkeypatch)
    monkeypatch.setattr(home_module, "_trial_board_record", slow)
    try:
        fields = _fields(await home.render())
        assert ("⚠️ Couldn't load the trial table right now. Press Refresh"
                in fields["🧪 Models on trial"])
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
async def test_nothing_is_flagged_when_every_read_worked(tmp_path, monkeypatch):
    bot, home = _make_home(tmp_path)
    home.horizon = 15
    _patch_live_reads(monkeypatch)
    try:
        page = "\n".join(_fields(await home.render()).values())
        assert "Couldn't load" not in page and "couldn't check" not in page
    finally:
        await _close(bot, home)


@pytest.mark.asyncio
@pytest.mark.parametrize(("horizon", "unit"), [
    (5, "results"), (15, "separate 15-min windows"), (60, "separate 60-min windows")])
async def test_the_learning_count_says_when_it_counts_whole_windows(
        tmp_path, monkeypatch, horizon, unit):
    """1,015 at 15 minutes vs 3,020 at 5 is by design (one per window)."""
    bot, home = _make_home(tmp_path)
    home.horizon = horizon
    _patch_live_reads(monkeypatch)
    monkeypatch.setattr(home_module, "_performance_benefit", lambda *_: {
        "status": "not_improving", "targets": 1015, "days": 12})
    try:
        assert f"(1,015 {unit} over 12 days)" in _fields(await home.render())[HEALTH]
    finally:
        await _close(bot, home)


def _intel(age):
    from datetime import UTC, datetime
    reviewed = datetime.now(UTC) - age
    return {"latest": {"issued_at": reviewed.isoformat(),
                       "stances": {5: ("follow", "down"), 15: ("follow", "up")}},
            "report": {"followed": 139, "followed_hit_rate": .52,
                       "abstained": 2, "model_hit_when_abstained": 0.0}}


@pytest.mark.asyncio
@pytest.mark.parametrize(("horizon", "age_minutes", "expected"), [
    (5, 3, "says DOWN (price falling) for the next candle"),
    (15, 20, "says UP (price rising) for the next candle"),
    (5, 40, "no current call (last review 40 min ago)"),
    (5, 60 * 30, "automatic reviews are paused (last one 30 hours ago); it still answers when asked"),
    (60, 60 * 24 * 3, "automatic reviews are paused (last one 3 days ago); it still answers when asked"),
])
async def test_an_old_ai_review_is_not_shown_as_a_call_for_the_next_candle(
        tmp_path, monkeypatch, horizon, age_minutes, expected):
    from datetime import timedelta
    bot, home = _make_home(tmp_path)
    home.horizon = horizon
    _patch_live_reads(monkeypatch)
    monkeypatch.setattr(home_module, "_intelligence_status",
                        lambda *_: _intel(timedelta(minutes=age_minutes)))
    try:
        health = _fields(await home.render())[HEALTH]
        assert f"**AI reviewer:** {expected}" in health
        assert "agreed 139 times (right 52%), sat out 2" in health
    finally:
        await _close(bot, home)
