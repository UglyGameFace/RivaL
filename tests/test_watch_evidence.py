"""Conservative WATCH evidence needs genuine daily breadth and meaningful gain."""
import pytest

from parlay_bot.perpetuals.watch_evidence import day_block_watch_evidence


def rows(gains, *, count=10):
    return [
        {
            "n": count,
            "model_sum": (.25 - gain) * count,
            "baseline_sum": .25 * count,
            "invalid_rows": 0,
        }
        for gain in gains
    ]


def test_consistent_multi_day_forward_gain_passes_research_watch():
    proof = day_block_watch_evidence(rows([.05] * 6))
    assert proof["watch_evidence_passes"] is True
    assert proof["watch_days"] == 6
    assert proof["watch_brier_gain_lower_bound"] == pytest.approx(.05)


def test_single_day_and_marginal_win_fail_closed():
    assert day_block_watch_evidence(rows([.05], count=60))[
        "watch_reason"
    ] == "insufficient_days"
    assert day_block_watch_evidence(rows([.001] * 6))[
        "watch_reason"
    ] == "uncertain_gain"


def test_lucky_outlier_does_not_override_uncertain_days():
    proof = day_block_watch_evidence(rows([.3, -.02, -.02, -.02, -.02, -.02]))
    assert proof["watch_evidence_passes"] is False
    assert proof["watch_reason"] == "uncertain_gain"


def test_sparse_days_and_invalid_scores_fail_closed():
    sparse = rows([.06] * 5, count=4)
    sparse.extend(rows([.06] * 3, count=12))
    assert day_block_watch_evidence(sparse)["watch_reason"] == "insufficient_days"
    invalid = rows([.05] * 6)
    invalid[1]["invalid_rows"] = 1
    assert day_block_watch_evidence(invalid)["watch_reason"] == "invalid_scores"
    invalid = rows([.05] * 6)
    invalid[1]["model_sum"] = float("nan")
    assert day_block_watch_evidence(invalid)["watch_reason"] == "invalid_scores"
