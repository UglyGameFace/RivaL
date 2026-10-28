"""Reliability must come from independent settled target outcomes."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.research_dashboard import ResearchDashboard
from parlay_bot.perpetuals.calibration_audit import forward_reliability
from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


def examples(*, bad: bool = False, venues: int = 1) -> list[dict]:
    start = datetime(2026, 10, 20, 12, tzinfo=UTC)
    result = []
    for day in range(7):
        for i in range(20):
            low = i < 10
            p = (0.2 if low else 0.8)
            label = (int(i < 2) if low else int(i < 18))
            if bad:
                label = 1 - label
            for _ in range(venues):
                result.append({
                    "target_opened_at": (
                        start + timedelta(days=day, minutes=5 * i)
                    ).isoformat(),
                    "probability": p, "up_rate": float(label),
                })
    return result


def test_seven_day_calibration_bins_and_independent_targets():
    one = forward_reliability(examples())
    repeated = forward_reliability(examples(venues=9))
    assert repeated == one
    assert one["unique_targets"] == 140
    assert one["days"] == 7
    assert one["disputed_targets"] == 0
    assert one["occupied_bins"] == 2
    assert one["dense_bins"] == 2
    assert one["ece"] == pytest.approx(0.0)
    assert one["status"] == "preliminary_reliability_screen_only"
    assert one["recalibration_trained"] is False
    assert one["approved_for_trading"] is False


def test_calibration_drift_is_flagged_and_short_forward_sample_not_trusted():
    bad = forward_reliability(examples(bad=True))
    assert bad["ece"] > 0.5
    assert bad["status"] == "calibration_gap_too_high"
    small = forward_reliability(examples()[:40])
    assert small["status"] == "insufficient_independent_forward_history"
    assert small["days"] < 7
    all_one_bin = forward_reliability([
        {**row, "probability": 0.6} for row in examples()
    ])
    assert all_one_bin["status"] == "narrow_probability_coverage"


def test_conflicting_venue_directions_not_a_fake_fractional_win():
    rows = examples()
    rows[0]["up_rate"] = .5
    result = forward_reliability(rows)
    assert result["disputed_targets"] == 1
    assert result["unique_targets"] == 139


def test_invalid_future_targets_and_probabilities_fail_closed():
    with pytest.raises(ValueError):
        forward_reliability([{
            "target_opened_at": "2026-10-28T10:00:00",
            "probability": .8, "up_rate": 1,
        }])
    with pytest.raises(ValueError):
        forward_reliability([{
            "target_opened_at": "2026-10-28T10:00:00+00:00",
            "probability": float("nan"), "up_rate": 1,
        }])


@pytest.mark.asyncio
async def test_private_accuracy_calibration_page_reads_real_forward_rows(tmp_path):
    store = SQLiteHotStore(tmp_path / "reliability.sqlite")
    market = PerpetualMarketStore(store)
    ForwardAccuracyMonitor(market).initialize()
    try:
        with store.connect() as conn:
            current = datetime.now(UTC)
            start = current - timedelta(days=6)
            for i, row in enumerate(examples()):
                base_target = datetime.fromisoformat(row["target_opened_at"])
                target = start + (
                    base_target - datetime(2026, 10, 20, 12, tzinfo=UTC)
                )
                for venue, contract in (("hyperliquid", "BTC"),):
                    p = row["probability"]
                    y = int(row["up_rate"])
                    conn.execute(
                        """INSERT INTO perp_forward_audit
                           (venue,contract,quote,asset,horizon_minutes,
                            model_version,reference_opened_at,reference_close,
                            issued_at,target_opened_at,probability_up,baseline_up,
                            training_evaluated,settled_at,outcome_up,realized_close,
                            model_brier,baseline_brier)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            venue, contract, "USDC", "BTC", 5, MODEL,
                            (target - timedelta(minutes=5)).isoformat(), 100.,
                            (target - timedelta(minutes=2)).isoformat(),
                            target.isoformat(), p, .5, 100,
                            (target + timedelta(minutes=5)).isoformat(),
                            y, 101. if y else 99.,
                            (p - y) ** 2, (.5 - y) ** 2,
                        ),
                    )
        view = ResearchDashboard(
            owner_id=42, market=market, ai=None,
            venues=("hyperliquid",), shadow_enabled=True,
        )
        try:
            view.screen = "accuracy"
            view.section = "calibration"
            view._rebuild()
            embed = await view.render()
            assert len(embed.fields) <= 3
            shown = {field.name: field.value for field in embed.fields}
            assert "Forward 5m calibration" in shown
            assert "Forward 15m calibration" in shown
            assert "140" in shown["Forward 5m calibration"]
            assert "Expected calibration error" in shown["Forward 5m calibration"]
            assert "preliminary reliability screen only" in shown[
                "Forward 5m calibration"
            ]
            assert "NOT" in shown["Forward 5m calibration"]
        finally:
            view.stop()
    finally:
        store.close()
