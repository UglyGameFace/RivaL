"""Fixed wall-clock phases for the two drifting 30-minute loops.

Sleep-after-work loops drift by their work time each iteration; the
benefit warm (:07/:37) and the Model Lab review (:17/:47) anchor their
sleep to the wall clock so they stop wandering into each other and into
the :00/:30 perp cycles (the 2026-11-20 saturation clusters).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.discord_app.bot import _phase_sleep_seconds


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 11, 20, hour, minute, second, tzinfo=UTC)


def test_phase_sleep_lands_on_the_offset_minutes() -> None:
    for offset in (7, 17):
        for start in (_at(0, 0), _at(0, 10), _at(0, 29), _at(0, 30), _at(0, 59)):
            wait = _phase_sleep_seconds(offset, now=start)
            assert 0 < wait <= 1800
            wake = start + timedelta(seconds=wait)
            assert wake.minute in (offset, offset + 30)


def test_phase_sleep_never_returns_zero_on_a_boundary() -> None:
    # Exactly on a tick, the loop sleeps a full period rather than
    # spinning straight into a second run.
    assert _phase_sleep_seconds(7, now=_at(0, 7)) == 1800
    assert _phase_sleep_seconds(17, now=_at(0, 47)) == 1800


def test_the_two_loops_stay_ten_minutes_apart() -> None:
    start = _at(0, 3)
    benefit = start + timedelta(seconds=_phase_sleep_seconds(7, now=start))
    lab = start + timedelta(seconds=_phase_sleep_seconds(17, now=start))
    assert benefit.minute == 7 and lab.minute == 17
