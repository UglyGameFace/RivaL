"""Offline model intake resumes through Drive without overwhelming the bot."""
from __future__ import annotations

from parlay_bot.discord_app.bot import candidate_import_delay


def test_candidate_import_scheduler_is_bounded_and_scans_backlog_fast():
    assert candidate_import_delay(backlog=True) == 900
    assert candidate_import_delay(backlog=False) == 3600
    assert candidate_import_delay(backlog=True, failed=True) == 300
    assert candidate_import_delay(backlog=False, failed=True) == 300
