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



def test_logging_shutdown_flush_is_safe_and_does_not_lose_pending_counts(tmp_path):
    """Python calls logging.Handler.flush() without a database argument."""
    store = SQLiteHotStore(tmp_path / "shutdown.sqlite")
    problems = RecentProblems(clock=lambda: NOW)
    logger = logging.getLogger("parlay_bot.test_shutdown_flush")
    logger.addHandler(problems)
    try:
        logger.warning("warning before shutdown")
        assert problems.flush() == 0
        assert problems.flush(store) == 1
        assert problems.stored(store)[1]["WARNING"] == 1
    finally:
        logger.removeHandler(problems)
        store.close()


def test_slow_database_owners_are_counted_separately(tmp_path):
    """One slow-query format string must not merge every caller into one count."""
    from parlay_bot.storage.database import _owner_group

    store = SQLiteHotStore(tmp_path / "owners.sqlite")
    problems = RecentProblems(clock=lambda: NOW)
    log = logging.getLogger("parlay_bot.storage.database_test_owners")
    log.addHandler(problems)
    msg = "RivaL PostgreSQL slow operation owner=%s operation=%s duration=%.2fs"
    try:
        for owner in ("perpetual_candles._next_batch:117", "perpetual_candles._next_batch:92"):
            log.warning(msg, owner, "SELECT", 2.9,
                        extra={"problem_group": _owner_group(owner)})
        for _ in range(3):
            owner = "forward_audit.active_learning_evidence:434"
            log.warning(msg, owner, "WITH", 7.1,
                        extra={"problem_group": _owner_group(owner)})
        log.warning("plain %s", "warning")  # no group: unchanged behaviour
        assert problems.flush(store) == 3
        groups, totals = problems.stored(store)
    finally:
        log.removeHandler(problems)
        store.close()
    assert totals == {"ERROR": 0, "WARNING": 6}
    counts = {g.text.split("owner=")[1].split(":")[0] if "owner=" in g.text else g.text: g.count
              for g in groups}
    assert counts == {"perpetual_candles._next_batch": 2,
                      "forward_audit.active_learning_evidence": 3,
                      "plain warning": 1}


def test_event_feed_outages_are_grouped_per_source_not_merged(tmp_path):
    """One event-feed format string must not merge every source into one count.

    GDELT can be attempted at most 48x/day, so a 58x count on the card
    necessarily included other sources; each source needs its own group so
    the card attributes the count to the source that actually failed.
    """
    import httpx

    from parlay_bot.perpetuals.event_intelligence import SOURCES, EventIntelligence

    store = SQLiteHotStore(tmp_path / "event_sources.sqlite")
    problems = RecentProblems(clock=lambda: NOW)
    log = logging.getLogger("parlay_bot.perpetuals.event_intelligence")
    log.addHandler(problems)
    try:

        def handler(request):
            if request.url.host == "api.gdeltproject.org":
                raise httpx.ConnectTimeout("timed out", request=request)
            if str(request.url) == SOURCES["sec_releases"][0]:
                return httpx.Response(503)
            return httpx.Response(
                200,
                content=b"<rss version='2.0'><channel></channel></rss>"
                if "xml" in str(request.url) or "rss" in str(request.url)
                else b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
            )

        intel = EventIntelligence(store)
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            report = intel.refresh(now=NOW, client=http)
    finally:
        log.removeHandler(problems)
        store.close()
    assert {f["source"] for f in report["source_failures"]} == {"global_news_index", "sec_releases"}
    groups = problems.recent()
    assert len(groups) == 2
    assert sorted(g.count for g in groups) == [1, 1]
    assert {g.text.split("source=")[1].split()[0] for g in groups} == {
        "global_news_index",
        "sec_releases",
    }
    # The card reads the durable history: the split must survive a restart.
    store = SQLiteHotStore(tmp_path / "event_sources.sqlite")
    try:
        assert problems.flush(store) == 2
        stored_groups, totals = problems.stored(store)
    finally:
        store.close()
    assert totals == {"ERROR": 0, "WARNING": 2}
    assert len(stored_groups) == 2
    assert sorted(g.count for g in stored_groups) == [1, 1]
