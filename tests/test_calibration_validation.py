"""Independent outcome-window and calibration proof against future leakage."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.calibration_validation import unique_window_calibration


def records(*, days=21, per_day=10, probability=.7, daily_wins=7):
    now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    rows = []
    for day in range(days):
        for slot in range(per_day):
            target = now - timedelta(days=days-day) + timedelta(minutes=5*slot)
            rows.append({
                "target_opened_at": target.isoformat(),
                # Issued just after the target 5m candle opens but before
                # it closes, as the live five-minute forecast does.
                "issued_at": (target+timedelta(seconds=15)).isoformat(),
                "settled_at": (target+timedelta(minutes=6)).isoformat(),
                "probability_up": probability,
                "reference_probability_up": .5,
                "outcome_up": int(slot < daily_wins),
            })
    return rows


def test_only_unique_forward_events_support_21_day_calibration():
    evidence = unique_window_calibration(records())
    assert evidence["unique_windows"] == 210
    assert evidence["utc_days"] == 21
    assert evidence["status"] == "research_screen_passed"
    assert evidence["ece"] == pytest.approx(0)
    assert evidence["brier"] < evidence["incumbent_brier"]
    assert evidence["day_block_lower"] > 0


def test_hundred_correlated_windows_do_not_qualify():
    proof = unique_window_calibration(records(days=10))
    assert proof["status"] == "insufficient"
    assert proof["unique_windows"] == 100


def test_fake_certainty_and_duplicate_windows_are_rejected():
    assert unique_window_calibration(records(probability=.99))[
        "status"
    ] == "not_calibrated"
    duplicated = records()
    with pytest.raises(ValueError, match="duplicate"):
        unique_window_calibration(duplicated + [duplicated[0]])
    corrupted = records()
    corrupted[0]["issued_at"] = (
        datetime.fromisoformat(corrupted[0]["target_opened_at"])
        + timedelta(hours=2)
    ).isoformat()
    with pytest.raises(ValueError, match="non-causal"):
        unique_window_calibration(corrupted)
