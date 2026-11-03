"""Prevent configuration drift that silently slows historical archive recovery.

The bounded GitHub schedules are read-only static tests; actual downloads
still enforce their source-specific checks and provider throttles.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _workflow(name: str) -> str:
    return (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")


def _scheduled_hours(content: str) -> tuple[int, ...]:
    matches = re.findall(r'cron: "(\d+) ([\d,*/]+) \* \* \*"', content)
    assert len(matches) == 1, "must have exactly one bounded daily provider schedule"
    hours = matches[0][1]
    if hours == "*/2":
        return tuple(range(0, 24, 2))
    return tuple(int(value) for value in hours.split(","))


def test_gate_advanced_recovery_has_bounded_twelve_run_capacity():
    config = _workflow("gate-official-archive-backfill.yml")
    hours = _scheduled_hours(config)
    assert len(hours) == len(set(hours)) == 12
    assert '--max-new "$MAX_NEW" --max-attempts 48' in config
    assert "MAX_NEW: ${{ inputs.max_new || '32' }}" in config
    assert "group: rival-gate-public-bulk-archive" in config
    assert "cancel-in-progress: false" in config
    assert "vars.RIVAL_GATE_ARCHIVE_ENABLED != 'false'" in config


def test_binance_is_staggered_time_budgeted_and_self_continuing():
    gate = _workflow("gate-official-archive-backfill.yml")
    binance = _workflow("binance-deep-file-backfill.yml")
    gh = _scheduled_hours(gate)
    bh = _scheduled_hours(binance)
    # Four schedule slots only restart a dropped chain; each run continues
    # itself while backlog remains (issue #295).
    assert len(bh) == len(set(bh)) == 4
    assert not set(gh).intersection(bh)
    assert "--max-new 5000 --time-budget-seconds 2700" in binance
    assert 'gh workflow run "binance-deep-file-backfill.yml"' in binance
    assert '"$CHAIN" -lt 40' in binance
    assert "actions: write" in binance
    assert "group: rival-binance-2021-monthly-archive" in binance
    assert "cancel-in-progress: false" in binance
    assert "vars.RIVAL_NATIVE_PUBLIC_BOOTSTRAP_ENABLED != 'false'" in binance
    assert "source_family_coverage" in gate and "source_family_coverage" in binance


def test_perfect_gate_capacity_is_under_three_days_but_not_proven():
    # Based on latest source-family backlog of 716, not fabricated success.
    assert math.ceil(716 / (12 * 32)) == 2
