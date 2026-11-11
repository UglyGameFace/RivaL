"""A failed Bot Performance read shows the last good copy, with its age."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app import home, screen_snapshots, trial_board
from parlay_bot.storage.hot import SQLiteHotStore


@pytest.fixture(autouse=True)
def _clean():
    screen_snapshots.reset()
    yield
    screen_snapshots.reset()


def test_copy_survives_a_restart(tmp_path, monkeypatch):
    store = SQLiteHotStore(tmp_path / "s.sqlite")
    try:
        screen_snapshots.load(store)
        screen_snapshots.remember("exchange", "BTC", 0, {"calls": 5, "when": datetime.now(UTC)})
        screen_snapshots.reset()                      # a restart empties memory
        assert screen_snapshots.last("exchange", "BTC", 0) is None
        assert screen_snapshots.load(store) == 1
        value, age = screen_snapshots.last("exchange", "BTC", 0)
        assert value["calls"] == 5 and age < timedelta(minutes=1)
        # Too old is never shown.
        later = datetime.now(UTC) + screen_snapshots.MAX_AGE + timedelta(minutes=1)
        assert screen_snapshots.last("exchange", "BTC", 0, now=later) is None
    finally:
        store.close()


def test_last_good_maps_screen_names():
    screen_snapshots.remember("_trial_board_record", "ETH", 15, {"rows": []})
    screen_snapshots.remember("exchange", "ETH", 0, {"x": 1})
    assert home._last_good("trials", "ETH", 15)[0] == {"rows": []}
    assert home._last_good("exchange", "ETH", 60)[0] == {"x": 1}
    assert home._last_good("trials", "ETH", 5) is None
    assert home._last_good("cycles", "ETH", 15) is None


def test_partial_trial_board_is_not_cached(monkeypatch):
    calls = []

    def reader(market, asset, horizon):
        calls.append(1)
        return {"rows": [], "partial": True}

    market = object()
    home._cached_page_read(reader, market, "BTC", 5)
    home._cached_page_read(reader, market, "BTC", 5)
    assert len(calls) == 2                       # read again, not a cached blank
    assert screen_snapshots.last("reader", "BTC", 5) is None


def test_failed_hit_rates_are_not_zero(tmp_path):
    store = SQLiteHotStore(tmp_path / "e.sqlite")    # no ledger table: the read fails
    try:
        assert trial_board.hit_rates(store, asset="BTC", horizon=5) is None
    finally:
        store.close()


def test_page_says_when_a_section_is_an_older_copy():
    now = datetime.now(UTC)
    embed = home.performance_embed(
        "BTC", 5, {"_stale": {"exchange": timedelta(minutes=12)}}, now=now)
    assert embed.description.startswith("**⚠️ NOT ALL LIVE")
    assert "12 min old" in embed.description
    assert embed.color == home.discord.Color.orange()
    scoreboard = embed.fields[0]
    assert scoreboard.name.endswith("⏳ OLD COPY")
    assert scoreboard.value.startswith("**⏳ Old copy from 12 min ago, not live.**")
    assert not any(f.name.endswith("OLD COPY") for f in embed.fields[1:])
    plain = home.performance_embed("BTC", 5, {}, now=now)
    assert "NOT ALL LIVE" not in plain.description
    assert not any("OLD COPY" in f.name for f in plain.fields)
