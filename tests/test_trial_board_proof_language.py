"""Trial leaders must never imply time alone establishes model quality."""
from parlay_bot.discord_app.trial_board import TrialRow, leader_line


def test_trial_leader_reports_remaining_checks_after_day_floor():
    row = TrialRow(
        name="6-year", days=21, results=2600, gain=0.0027,
        ahead=1, exchanges=1, stopped=0, passed=False,
        after_fees_bps=0.8,
    )
    line = leader_line([row])
    assert "complete promotion checks" in line
    assert "trusted" not in line
    assert "aggregate after-fee comparison" in line


def test_trial_leader_does_not_call_missing_fee_proof_positive():
    row = TrialRow(
        name="Calibrated", days=10, results=2400, gain=0.0022,
        ahead=1, exchanges=1, stopped=0, passed=False,
        after_fees_bps=None,
    )
    line = leader_line([row])
    assert "11 more days" in line
    assert "after-fee comparison unavailable" in line
    assert "all promotion checks must pass" in line
