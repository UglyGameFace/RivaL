"""Synthetic proofs for the late-window test's frozen state machine.

The verdict logic must PASS a genuinely winning call stream, KILL a
losing one, KILL a stream whose losses are too often near-total even
when its mean is positive, and refuse any verdict below the floors --
the same discipline the N2/N3 graders were proven against.
"""
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.late_window_evaluation import (
    ACCRUAL_DEADLINE,
    REGISTRATION_EPOCH,
    collect_observations,
    grade_late_window,
)

NOW = datetime(2026, 12, 20, tzinfo=UTC)


def _calls(days: int, per_day: int, net) -> list[dict]:
    """Call observations over consecutive days from the epoch.

    ``net`` is a constant per-call net or a callable(day_idx, i) -> net.
    """
    out = []
    start = REGISTRATION_EPOCH + timedelta(days=1)
    for d in range(days):
        day0 = start + timedelta(days=d)
        for i in range(per_day):
            value = net(d, i) if callable(net) else net
            out.append({
                "window_open": day0 + timedelta(minutes=15 * i),
                "day": day0.date().isoformat(),
                "call_side": "up",
                "call_net": value,
            })
    return out


def test_winning_stream_passes():
    # 168 calls over 21 days: mostly +9c, 5% near-total losses.
    obs = _calls(21, 8, lambda d, i: -0.95 if i == 0 and d % 4 == 0 else 0.09)
    report = grade_late_window(obs, now=NOW)
    assert report["status"] == "EVALUATED" and report["verdict"] == "PASS"
    assert all(report["legs"].values())
    assert report["standing"]["preliminary"] is False


def test_losing_stream_is_killed():
    obs = _calls(21, 8, -0.05)
    report = grade_late_window(obs, now=NOW)
    assert report["verdict"] == "KILL"
    assert report["legs"]["mean_net_ci_above_zero"] is False


def test_frequent_near_total_losses_kill_despite_positive_mean():
    # 80% at +45c, 20% near-total losses: mean +17c, adverse 20% > 15%.
    obs = _calls(25, 10, lambda d, i: -0.95 if i in (0, 5) else 0.45)
    report = grade_late_window(obs, now=NOW)
    assert report["standing"]["mean_net"] > 0
    assert report["standing"]["adverse_share"] > 0.15
    assert report["verdict"] == "KILL"
    assert report["legs"]["adverse_share_within_cap"] is False


def test_below_floors_is_accrual_never_a_verdict():
    obs = _calls(5, 8, 0.09)  # 40 calls, 5 days: far below the floors
    report = grade_late_window(obs, now=NOW)
    assert report["status"] == "ACCRUAL" and report["verdict"] is None
    assert report["standing"]["preliminary"] is True
    assert report["standing"]["mean_net"] == 0.09  # shown, labeled


def test_accrual_deadline_fails_closed():
    obs = _calls(5, 8, 0.09)
    report = grade_late_window(obs, now=ACCRUAL_DEADLINE + timedelta(days=1))
    assert report["status"] == "ACCRUAL_FAILED" and report["verdict"] is None


def test_collect_filters_venue_epoch_and_missing_strike():
    base = REGISTRATION_EPOCH
    windows = [
        # Pre-epoch Polymarket window: excluded before any pricing.
        {"venue": "polymarket", "window_open": base - timedelta(days=2),
         "settlement": 1, "strike": 100.0},
        # Another venue entirely: skipped, not counted.
        {"venue": "kalshi", "window_open": base + timedelta(days=1),
         "settlement": 1, "strike": 100.0},
        # Post-epoch but unsettled: counted exclusion.
        {"venue": "polymarket", "window_open": base + timedelta(days=1),
         "settlement": None, "strike": 100.0},
        # Post-epoch, settled, but no strike: counted exclusion.
        {"venue": "polymarket", "window_open": base + timedelta(days=1),
         "settlement": 1, "strike": None},
    ]
    observations, exclusions = collect_observations(windows)
    assert observations == []
    assert exclusions["pre_epoch"] == 1
    assert exclusions["settlement_unavailable"] == 1
    assert exclusions["strike_unavailable"] == 1
