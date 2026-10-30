"""Conservative shadow governance: correlated venues, false skill and drift."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.autonomous import AutonomousResearch
from parlay_bot.perpetuals.governance import score_paired_forward
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore

START = datetime(2026, 10, 21, 18, tzinfo=UTC)


def paired_days(deltas, *, venues=3, per_day=16):
    """Positive delta means model Brier beats its paired baseline."""
    rows = []
    for day, improvement in enumerate(deltas):
        for idx in range(per_day):
            target = (START + timedelta(days=day, minutes=5 * idx)).isoformat()
            for _ in range(venues):
                rows.append((target, .25 - improvement, .25))
    return rows


def test_stable_skill_qualifies_only_for_shadow_and_not_by_venue_multiplicity():
    audit = score_paired_forward(paired_days([.08] * 7, venues=9))
    assert audit.targets == 112  # 1008 rows are just 112 correlated UTC events.
    assert audit.days == 7 and audit.positive_days == 7
    assert audit.status == "qualified_shadow"
    assert audit.lower_day_bound == pytest.approx(.08)
    assert audit.paired_gain == pytest.approx(.08)
    incomplete = score_paired_forward(paired_days([.08] * 6, venues=9))
    assert incomplete.status == "insufficient"  # Only six distinct days.


def test_a_few_lucky_wins_cannot_qualify_a_drifting_model():
    # Model wins dramatically on two days but *loses* in 5 others.
    samples = paired_days([.24, .24, -.03, -.03, -.03, -.03, -.03])
    audit = score_paired_forward(samples)
    assert audit.targets == 112 and audit.days == 7
    assert audit.paired_gain > .005
    assert audit.status == "rejected_shadow"
    assert audit.positive_days == 2
    assert audit.lower_day_bound < 0


def test_small_or_unreliable_skill_cannot_be_called_qualified():
    weak = score_paired_forward(paired_days([.002] * 7))
    assert weak.status == "rejected_shadow"
    assert "below minimum" in weak.reason
    noisy = score_paired_forward(
        paired_days([.08, .08, .08, .08, .08, .08, -.12])
    )
    assert noisy.paired_gain > .005
    assert noisy.positive_days == 6
    assert noisy.lower_day_bound < 0
    assert noisy.status == "rejected_shadow"
    assert "stress" in noisy.reason


def test_invalid_scores_or_timestamps_never_vote_for_model_promotion():
    with pytest.raises(ValueError, match="Brier"):
        score_paired_forward([(START.isoformat(), float("nan"), .25)])
    with pytest.raises(ValueError, match="Brier"):
        score_paired_forward([(START.isoformat(), -.1, .25)])
    with pytest.raises(ValueError, match="UTC"):
        score_paired_forward([("2026-10-20T12:00:00", .2, .25)])
    empty = score_paired_forward([])
    assert empty.targets == 0 and empty.status == "insufficient"


def test_governance_filters_wrong_model_versions_and_uses_paired_loss(tmp_path):
    store = SQLiteHotStore(tmp_path / "governance.sqlite")
    market = PerpetualMarketStore(store)
    supervisor = AutonomousResearch(market, ai=None, venues=(), assets=("BTC",))
    now = datetime(2026, 10, 28, 16, tzinfo=UTC)
    supervisor.initialize()
    try:
        with store.connect() as conn:
            for i, (target, loss, baseline) in enumerate(
                paired_days([.04] * 7, venues=1)
            ):
                for version, a, b in (
                    ("prospective-logistic-shadow-v2", loss, baseline),
                    ("experimental-rogue", .8, .1),
                ):
                    conn.execute(
                        """INSERT INTO perp_forward_audit
                           (venue,contract,quote,asset,horizon_minutes,
                            model_version,reference_opened_at,reference_close,
                            issued_at,target_opened_at,probability_up,
                            baseline_up,training_evaluated,settled_at,
                            outcome_up,realized_close,model_brier,
                            baseline_brier)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        ("hyperliquid", "BTC", "USDC", "BTC", 5,
                         version, (START + timedelta(minutes=i)).isoformat(),
                         100., (datetime.fromisoformat(target) -
                                timedelta(minutes=2)).isoformat(),
                         target, .6, .5, 100,
                         (datetime.fromisoformat(target) +
                          timedelta(minutes=6)).isoformat(),
                         1, 102., a, b),
                    )
        assert supervisor.review_model(now=now) >= 1
        with store.connect() as conn:
            row = conn.execute(
                """SELECT * FROM perp_model_governance
                   WHERE asset='BTC' AND horizon_minutes=5
                   ORDER BY evaluated_at DESC LIMIT 1"""
            ).fetchone()
        assert row["status"] == "qualified_shadow"
        assert row["model_version"] == "prospective-logistic-shadow-v2"
        assert row["unique_target_windows"] == 112
        assert "positive_days=7/7" in row["reason"]
        assert "day_stress_lower=" in row["reason"]
        # Redoing unchanged review cannot add a duplicate governance decision.
        assert supervisor.review_model(now=now) == 0
    finally:
        store.close()
