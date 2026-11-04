"""Status card error history saved across restarts (rival_problem_log)."""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.error_log import RecentProblems
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 3, 16, 0, tzinfo=UTC)


def test_problem_history_survives_a_restart_without_double_counting(tmp_path):

    store = SQLiteHotStore(tmp_path / "problems.sqlite")
    clock = [NOW]
    try:
        before = RecentProblems(clock=lambda: clock[0])
        log = logging.getLogger("parlay_bot.test_history")
        log.addHandler(before)
        try:
            for _ in range(3):
                log.error("feed %s failed", "okx")
            assert before.flush(store) == 1
            assert before.flush(store) == 0  # already saved; nothing new
        finally:
            log.removeHandler(before)
        clock[0] = NOW + timedelta(hours=2)
        restarted = RecentProblems(clock=lambda: clock[0])  # empty memory, like a deploy
        log.addHandler(restarted)
        try:
            log.error("feed %s failed", "okx")  # one new, not yet saved
            groups, totals = restarted.stored(store)
        finally:
            log.removeHandler(restarted)
        assert totals == {"ERROR": 4, "WARNING": 0}
        assert groups[0].count == 4 and "okx" in groups[0].text
    finally:
        store.close()


def test_failed_save_keeps_counts_for_the_next_try(tmp_path):
    class Down:
        def connect(self):
            raise TimeoutError("pool busy")

    problems = RecentProblems(clock=lambda: NOW)
    log = logging.getLogger("parlay_bot.test_history_down")
    log.addHandler(problems)
    try:
        log.warning("slow %s", "query")
        log.warning("slow %s", "query")
    finally:
        log.removeHandler(problems)
    with pytest.raises(TimeoutError):
        problems.flush(Down())
    store = SQLiteHotStore(tmp_path / "later.sqlite")
    try:
        assert problems.flush(store) == 1
        assert problems.stored(store)[1] == {"ERROR": 0, "WARNING": 2}
    finally:
        store.close()
