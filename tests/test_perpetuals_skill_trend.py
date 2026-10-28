"""Improving accuracy must mean better baseline-adjusted skill, not easier markets."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.analytics import analytics_snapshot
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor, MODEL
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


def test_raw_brier_improves_but_relative_skill_worsens(tmp_path):
    store = SQLiteHotStore(tmp_path / "trend.sqlite")
    market = PerpetualMarketStore(store)
    ForwardAccuracyMonitor(market).initialize()
    now = datetime(2026, 10, 28, 12, tzinfo=UTC)
    try:
        with store.connect() as conn:
            for section, days in (("prior", 10), ("recent", 2)):
                start = now - timedelta(days=days)
                for i in range(120):
                    target = start + timedelta(minutes=5 * i)
                    issued = target - timedelta(minutes=1)
                    opened = target - timedelta(minutes=5)
                    # Both weeks predict up correctly, but the baseline is
                    # much stronger in the recent regime.
                    p, baseline = (.60, .50) if section == "prior" else (.65, .70)
                    conn.execute(
                        """INSERT INTO perp_forward_audit
                           (venue,contract,quote,asset,horizon_minutes,
                            model_version,reference_opened_at,reference_close,
                            issued_at,target_opened_at,probability_up,
                            baseline_up,training_evaluated,settled_at,
                            outcome_up,realized_close,model_brier,
                            baseline_brier)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        ("hyperliquid", "BTC", "USDC", "BTC", 5, MODEL,
                         opened.isoformat(), 100., issued.isoformat(),
                         target.isoformat(), p, baseline, 100,
                         (target + timedelta(minutes=5)).isoformat(),
                         1, 101., (1 - p) ** 2, (1 - baseline) ** 2),
                    )
                    # A different research model must not pollute the
                    # official 5m math model's trend or event counts.
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
                         "rogue-older-experiment-v0",
                         opened.isoformat(), 100., issued.isoformat(),
                         target.isoformat(), .01, .99, 100,
                         (target + timedelta(minutes=5)).isoformat(),
                         1, 101., .9801, .0001),
                    )
        report = analytics_snapshot(market, asset="BTC", now=now)
        period = report["math"][5]
        assert period["last_7d"]["events"] == 120
        assert period["prior_7d"]["events"] == 120
        assert (
            period["last_7d"]["brier"] < period["prior_7d"]["brier"]
        )  # Raw score looks better.
        assert period["trend"] == "worsening"  # But relative skill fell.
        assert period["last_7d_skill"] < period["prior_7d_skill"]
        assert period["trend_method"] == "relative_to_same_window_baseline"
    finally:
        store.close()
