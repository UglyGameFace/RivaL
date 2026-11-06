"""Track Next on Polymarket: next frozen calls only, graded by Polymarket, priced at the ask."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals.forward_tracking import TrackingUnavailable
from parlay_bot.perpetuals.polymarket_learning import (
    MODEL,
    PolymarketOutcomeLearner,
    polymarket_fee,
)
from parlay_bot.perpetuals.polymarket_research import PolymarketResearch
from parlay_bot.perpetuals.polymarket_tracking import PolymarketTracking, polymarket_tracking_text

START = datetime(2026, 11, 6, 5, tzinfo=UTC)


def _contract(store, i, *, p, up_ask, down_ask, outcome, asset="BTC", late_price=False):
    opens = START + timedelta(minutes=5 * i)
    issued = opens + timedelta(seconds=4)
    observed = issued - (timedelta(minutes=2) if late_price else timedelta(seconds=1))
    expires = opens + timedelta(minutes=5)
    slug = f"{asset.lower()}-updown-5m-{i}"
    features = [up_ask, down_ask, up_ask / (up_ask + down_ask), up_ask + down_ask - 1,
                0, 0, 1, 0, up_ask - down_ask, 0]
    import json
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO rival_polymarket_observations (slug,condition_id,asset,horizon,"
            "resolution_source,expires_at,observed_at,features_json,evidence_sha256) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (slug, "0x" + "a" * 64, asset, 5, "chainlink", expires.isoformat(),
             observed.isoformat(), json.dumps(features), "e" * 64))
        conn.execute(
            "INSERT INTO rival_polymarket_model_forward (slug,model_version,issued_at,"
            "weights_sha256,probability_up,market_baseline) VALUES (?,?,?,?,?,?)",
            (slug, MODEL, issued.isoformat(), "w" * 64, p, up_ask / (up_ask + down_ask)))
        if outcome:
            conn.execute(
                "INSERT INTO rival_polymarket_ai_outcomes (slug,condition_id,direction,"
                "resolved_at,observed_at,receipt_sha256,receipt_json) VALUES (?,?,?,?,?,?,?)",
                (slug, "0x" + "a" * 64, outcome, expires.isoformat(),
                 (expires + timedelta(minutes=1)).isoformat(), "r" * 64, "{}"))


def _store(learning_store):
    PolymarketOutcomeLearner(learning_store).initialize()
    PolymarketResearch(learning_store).initialize()
    return PolymarketTracking(learning_store)


def test_only_calls_after_start_count_and_results_are_priced_honestly(learning_store):
    tracker = _store(learning_store)
    with pytest.raises(TrackingUnavailable):
        tracker.start(owner_id=1, asset="BTC", horizon=5, target=25, now=START)
    _contract(learning_store, -1, p=0.9, up_ask=0.5, down_ask=0.5, outcome="UP")  # before start
    assert tracker.start(owner_id=1, asset="BTC", horizon=5, target=25,
                         now=START)["status"] == "started"
    assert tracker.start(owner_id=1, asset="BTC", horizon=5, target=25,
                         now=START)["status"] == "already_tracking"
    _contract(learning_store, 1, p=0.40, up_ask=0.48, down_ask=0.53, outcome="DOWN")  # win
    _contract(learning_store, 2, p=0.70, up_ask=0.60, down_ask=0.41, outcome="DOWN")  # loss
    _contract(learning_store, 3, p=0.48, up_ask=0.50, down_ask=0.51, outcome="UP")    # WAIT
    _contract(learning_store, 4, p=0.30, up_ask=0.45, down_ask=0.56, outcome="DOWN",
              late_price=True)  # win, but price not recorded at call time
    _contract(learning_store, 5, p=0.80, up_ask=0.50, down_ask=0.51, outcome=None)    # pending
    report = tracker.report(owner_id=1, asset="BTC", now=START + timedelta(hours=1))
    item = report["cohorts"][0]
    assert (item["issued"], item["settled"], item["pending"]) == (5, 4, 1)
    assert (item["picked"], item["wins"], item["waits"], item["priced"]) == (3, 2, 1, 2)
    stake = 0.53 + polymarket_fee(0.53) + 0.60 + polymarket_fee(0.60)
    assert item["return_per_dollar"] == pytest.approx((1.0 - stake) / stake)
    text = polymarket_tracking_text(report)
    assert "Platform: Polymarket" in text and "right **2**" in text
    assert "after Polymarket fees" in text and "no real trades" in text
    # Another user's view is separate.
    assert tracker.report(owner_id=2, asset="BTC")["status"] == "not_started"


def test_opportunities_lead_with_open_polymarket_calls(learning_store):
    from parlay_bot.discord_app.opportunities import polymarket_call_fields
    _store(learning_store)
    _contract(learning_store, 0, p=0.40, up_ask=0.48, down_ask=0.53, outcome=None)  # open
    _contract(learning_store, -2, p=0.80, up_ask=0.5, down_ask=0.5, outcome="UP")   # closed
    now = START + timedelta(minutes=2)
    fields = polymarket_call_fields(learning_store, asset="BTC", now=now)
    assert len(fields) == 1
    name, value = fields[0]
    assert name == "🎰 Polymarket • 5 min • call: DOWN"
    assert "40.0% UP" in value and "btc-updown-5m-0" in value and "Not a proven edge" in value
    # Before the call is issued nothing is shown, never a guessed call.
    assert polymarket_call_fields(learning_store, asset="BTC", now=START + timedelta(seconds=2)) == []
