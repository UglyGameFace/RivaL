"""Next-X cohorts are prospective, source exact and persistent."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.research_dashboard import _SECTIONS, ResearchDashboard
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.forward_tracking import (
    ForwardTracking,
    TrackingUnavailable,
    tracking_text,
)
from parlay_bot.perpetuals.market_store import PerpetualMarketStore

START = datetime(2026, 11, 2, 18, tzinfo=UTC)


def _forecast(store, i, *, venue="hyperliquid", settled=True, p=0.7,
              issued_before=False, lineage=True):
    target = START + timedelta(minutes=5 * i)
    issued = target - timedelta(seconds=40)
    if issued_before:
        issued = START - timedelta(minutes=5)
        target = START - timedelta(minutes=5)
    settled_at = target + timedelta(minutes=5, seconds=20)
    ref = target - timedelta(minutes=5)
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO perp_forward_audit (
                 venue,contract,quote,asset,horizon_minutes,model_version,
                 reference_opened_at,reference_close,issued_at,target_opened_at,
                 probability_up,baseline_up,training_evaluated,settled_at,
                 outcome_up,realized_close,model_brier,baseline_brier)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (venue, "BTC", "USDC", "BTC", 5,
             "prospective-logistic-persistent-v3",
             ref.isoformat(), 100., issued.isoformat(), target.isoformat(),
             p, .5, 120, settled_at.isoformat() if settled else None,
             1 if settled else None, 101. if settled else None,
             (p-1)**2 if settled else None, .25 if settled else None),
        )
        if lineage:
            conn.execute(
                """INSERT INTO rival_forward_model_lineage (
                     venue,contract,quote,horizon_minutes,model_version,
                     reference_opened_at,issued_at,weights_sha256,feature_sha256,
                     training_labels,historical_training_labels,online_training_updates,
                     historical_seed_sha256)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (venue, "BTC", "USDC", 5, "prospective-logistic-persistent-v3",
                 ref.isoformat(), issued.isoformat(), "a"*64, "b"*64,
                 120, 0, i, None),
            )


def _monitor(store):
    ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
    return ForwardTracking(store)


def test_next_x_starts_now_not_before_and_cannot_reset_while_running(learning_store):
    tracker = _monitor(learning_store)
    with pytest.raises(TrackingUnavailable):
        tracker.start(owner_id=42, asset="BTC", venue="hyperliquid",
                      horizon=5, target=25, now=START)
    _forecast(learning_store, 0, issued_before=True)
    started = tracker.start(owner_id=42, asset="BTC", venue="hyperliquid",
                            horizon=5, target=25, now=START)
    assert started["status"] == "started"
    assert tracker.start(owner_id=42, asset="BTC", venue="hyperliquid",
                         horizon=5, target=150, now=START)["status"] == "already_tracking"
    for i in range(1, 26):
        _forecast(learning_store, i, p=.55 if i <= 12 else .8)
    _forecast(learning_store, 27, venue="okx")
    result = tracker.report(owner_id=42, asset="BTC", venue="hyperliquid",
                            now=START+timedelta(hours=6))
    assert len(result["cohorts"]) == 1
    cohort = result["cohorts"][0]
    assert cohort["issued"] == 25
    assert cohort["settled"] == 25
    assert cohort["correct"] == 25
    assert cohort["complete"]
    assert cohort["target"] == 25
    assert cohort["model_brier"] < cohort["baseline_brier"]
    assert cohort["trend"] == "later_period_higher_paired_gain"
    assert "No real trades" in tracking_text(result)
    assert tracker.report(owner_id=73, asset="BTC", venue="hyperliquid",
                          now=START+timedelta(hours=6))["status"] == "not_started"
    next_run = tracker.start(owner_id=42, asset="BTC", venue="hyperliquid",
                             horizon=5, target=50, now=START+timedelta(hours=6))
    assert next_run["session_id"] != started["session_id"]
    assert tracker.report(owner_id=42, asset="BTC", venue="hyperliquid",
                          now=START+timedelta(hours=6))["cohorts"][0]["issued"] == 0


def test_pending_missing_lineage_and_invalid_outcomes_never_complete(learning_store):
    tracker = _monitor(learning_store)
    _forecast(learning_store, 0, issued_before=True)
    tracker.start(owner_id=7, asset="BTC", venue="hyperliquid",
                  horizon=5, target=25, now=START)
    for i in range(1, 26):
        _forecast(learning_store, i, settled=i != 25,
                  lineage=i != 24)
    progress = tracker.report(owner_id=7, asset="BTC", venue="hyperliquid",
                              now=START+timedelta(hours=5))["cohorts"][0]
    assert progress["issued"] == 25
    assert progress["settled"] == 24
    assert progress["pending"] == 1
    assert progress["unverified_lineage"] == 1
    assert not progress["complete"]
    assert tracker.start(owner_id=7, asset="BTC", venue="hyperliquid",
                         horizon=5, target=50,
                         now=START+timedelta(hours=5))["status"] == "already_tracking"
    with learning_store.connect() as conn:
        conn.execute(
            """UPDATE perp_forward_audit SET settled_at=?,outcome_up=1,
                      model_brier=.09,baseline_brier=.25
               WHERE venue='hyperliquid' AND reference_opened_at=?""",
            ((START+timedelta(minutes=130, seconds=20)).isoformat(),
             (START+timedelta(minutes=120)).isoformat()),
        )
    completed = tracker.report(owner_id=7, asset="BTC", venue="hyperliquid",
                               now=START+timedelta(hours=5))["cohorts"][0]
    assert completed["settled"] == 25
    assert completed["unverified_lineage"] == 1
    assert not completed["complete"]


@pytest.mark.asyncio
async def test_tracking_controls_are_available_without_triggering_sessions(learning_store):
    tracker = _monitor(learning_store)
    panel = ResearchDashboard(owner_id=42, market=PerpetualMarketStore(learning_store),
                              ai=None, venues=("hyperliquid",))
    panel.screen, panel.section = "accuracy", "tracker"
    panel._rebuild()
    try:
        assert "tracker" in dict(_SECTIONS["accuracy"])
        ids = {child.custom_id: child for child in panel.children}
        assert "rival:research:track-start" in ids
        assert len(ids["rival:research:track-start"].options) == 18
        # Polymarket is the default platform; the exchange picker appears
        # only after switching to exchange futures.
        assert ids["rival:research:track-platform"].label == "Platform: Polymarket"
        assert "rival:research:track-venue" not in ids
        polymarket = await panel.render()
        assert "Platform: Polymarket" in polymarket.fields[-1].value
        panel.track_platform = "exchange"
        panel._rebuild()
        ids = {child.custom_id: child for child in panel.children}
        assert "rival:research:track-venue" in ids
        assert tracker.report(owner_id=42, asset="BTC", venue="hyperliquid")[
            "status"] == "not_started"
        # Merely opening a page must never create a new experiment.
        result = await panel.render()
        fields = {field.name: field.value for field in result.fields}
        assert "Prediction tracking" in fields
        assert "No tracking session yet" in fields["Prediction tracking"]
        assert tracker.report(owner_id=42, asset="BTC", venue="hyperliquid")[
            "status"] == "not_started"
    finally:
        panel.stop()


@pytest.mark.asyncio
async def test_bot_performance_has_direct_tracking_shortcut(tmp_path):
    from tests.test_guided_home import _client, _home

    bot = _client(tmp_path)
    home = _home(bot)
    home.research_factory = lambda _owner: object()
    home.navigate("performance")
    home._buttons()
    try:
        labels = {child.label for child in home.children if hasattr(child, "label")}
        assert "Track Next Predictions" in labels
        assert "Detailed Research" in labels
    finally:
        home.stop()
        await bot.zip_resolver.aclose()
        await bot.public_quotes.aclose()
        bot.store.close()
