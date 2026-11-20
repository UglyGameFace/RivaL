"""PR-2b: the live Polymarket model's after-fee money result.

Pins the fee math against hand-computed examples (including the fee at
50c under the pre-change 0.07 rate and both post-change rates), the
coverage rules (a call is priced only when the recorded book is proven
to be its issuance book), the summary() integration, and the rendered
Performance Overview line.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app import home as home_module
from parlay_bot.discord_app.performance_panels import after_fee_line, build
from parlay_bot.perpetuals.model_checkpoints import canonical
from parlay_bot.perpetuals.polymarket_learning import (
    MODEL,
    after_fee_call_net,
    polymarket_fee,
    polymarket_scheduled_fee_rate,
)
from parlay_bot.perpetuals.polymarket_research import PolymarketResearch
from parlay_bot.storage.hot import SQLiteHotStore
from tests.test_performance_panels import _xmarket
from tests.test_performance_three_embeds import NOW, _reports

BEFORE = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
AFTER = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def test_fee_at_fifty_cents_under_each_published_rate():
    assert polymarket_fee(0.5, 0.07) == pytest.approx(0.0175)   # 1.75c
    assert polymarket_fee(0.5, 0.05) == pytest.approx(0.0125)   # 1.25c
    assert polymarket_fee(0.5, 0.01) == pytest.approx(0.0025)   # 0.25c


def test_scheduled_fee_rate_boundaries():
    for asset, horizon in (("BTC", 5), ("BTC", 15), ("BTC", 60), ("ETH", 5)):
        assert polymarket_scheduled_fee_rate(
            asset=asset, horizon=horizon, issued_at=BEFORE) == 0.07
    assert polymarket_scheduled_fee_rate(asset="BTC", horizon=5, issued_at=AFTER) == 0.05
    assert polymarket_scheduled_fee_rate(asset="BTC", horizon=15, issued_at=AFTER) == 0.05
    assert polymarket_scheduled_fee_rate(asset="BTC", horizon=60, issued_at=AFTER) == 0.01
    assert polymarket_scheduled_fee_rate(asset="ETH", horizon=5, issued_at=AFTER) == 0.01
    assert polymarket_scheduled_fee_rate(asset="SOL", horizon=15, issued_at=AFTER) == 0.01


def _features(up, down, depth_up_off=0.0, depth_down_off=0.0):
    return [up, down, up / (up + down), up + down - 1,
            depth_up_off, depth_down_off, 0.5, 0.0, up - down,
            (up + depth_up_off) - (down + depth_down_off)]


def test_net_matches_hand_checked_production_rows():
    # XRP 15m, issued 2026-11-13: bought UP at 0.75, won.
    # net = 1 - 0.75 - 0.07*0.75*0.25 = 0.236875 (hand-verified in prod).
    net = after_fee_call_net(
        probability_up=0.650340996599739, direction="UP",
        features=_features(0.75, 0.26), market_baseline=0.75 / 1.01,
        asset="XRP", horizon=15, issued_at=datetime(2026, 11, 13, 21, 46, tzinfo=UTC))
    assert net == pytest.approx(0.236875)
    # XRP 5m at 50c, issued 2026-11-16: bought UP at 0.50, lost.
    # net = 0 - 0.50 - 0.0175 = -0.5175 (hand-verified in prod).
    net = after_fee_call_net(
        probability_up=0.5029700455425714, direction="DOWN",
        features=_features(0.5, 0.5), market_baseline=0.5,
        asset="XRP", horizon=5, issued_at=datetime(2026, 11, 16, 17, 25, tzinfo=UTC))
    assert net == pytest.approx(-0.5175)


def test_net_uses_depth_average_ask_and_post_change_rate():
    # Depth-average UP ask is 0.50 + 0.02 = 0.52; pre-change fee 0.07.
    net = after_fee_call_net(
        probability_up=0.8, direction="UP",
        features=_features(0.5, 0.5, depth_up_off=0.02), market_baseline=0.5,
        asset="ETH", horizon=5, issued_at=BEFORE)
    assert net == pytest.approx(1 - 0.52 - 0.07 * 0.52 * 0.48)
    # Post-change BTC 15m at 50c: fee is 0.05 * 0.25 = 0.0125.
    net = after_fee_call_net(
        probability_up=0.8, direction="UP",
        features=_features(0.5, 0.5), market_baseline=0.5,
        asset="BTC", horizon=15, issued_at=AFTER)
    assert net == pytest.approx(1 - 0.5 - 0.0125)


def test_net_refuses_unverifiable_or_unpriceable_calls():
    base = {"direction": "UP", "asset": "BTC", "horizon": 5, "issued_at": BEFORE}
    # Stored book is not the issuance book (baseline mismatch).
    assert after_fee_call_net(probability_up=0.8, features=_features(0.75, 0.26),
                              market_baseline=0.99, **base) is None
    # No favoured side.
    assert after_fee_call_net(probability_up=0.5, features=_features(0.5, 0.5),
                              market_baseline=0.5, **base) is None
    # A 1.00 ask cannot be bought.
    assert after_fee_call_net(probability_up=0.9, features=_features(1.0, 0.5),
                              market_baseline=1.0 / 1.5, **base) is None
    # Missing or short feature rows are excluded, never guessed.
    assert after_fee_call_net(probability_up=0.9, features=[],
                              market_baseline=0.5, **base) is None
    assert after_fee_call_net(probability_up=0.9, features=None,
                              market_baseline=0.5, **base) is None


def _seed_settled(store, slug, *, asset, horizon, probability, baseline,
                  features_json, direction):
    now = datetime.now(UTC)
    expires = now - timedelta(days=1)
    issued = expires - timedelta(minutes=4)
    resolved = expires + timedelta(minutes=2)
    condition = f"condition-{slug}"
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO rival_polymarket_observations
               (slug,condition_id,asset,horizon,resolution_source,
                expires_at,observed_at,features_json,evidence_sha256,learned_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (slug, condition, asset, horizon, "polymarket",
             expires.isoformat(), issued.isoformat(), features_json,
             "a" * 64, now.isoformat()))
        conn.execute(
            """INSERT INTO rival_polymarket_model_forward
               (slug,model_version,issued_at,weights_sha256,probability_up,market_baseline)
               VALUES (?,?,?,?,?,?)""",
            (slug, MODEL, issued.isoformat(), "b" * 64, probability, baseline))
        conn.execute(
            """INSERT INTO rival_polymarket_ai_outcomes
               (slug,condition_id,direction,resolved_at,observed_at,
                receipt_sha256,receipt_json)
               VALUES (?,?,?,?,?,?,?)""",
            (slug, condition, direction, resolved.isoformat(),
             (resolved + timedelta(minutes=1)).isoformat(), "c" * 64, "{}"))


def test_summary_computes_after_fee_over_exactly_the_priced_calls(tmp_path):
    store = SQLiteHotStore(tmp_path / "after-fee.sqlite")
    service = PolymarketResearch(store)
    service.initialize()
    try:
        _seed_settled(store, "win-up", asset="BTC", horizon=5, probability=0.65,
                      baseline=0.75 / 1.01,
                      features_json=canonical(_features(0.75, 0.26)),
                      direction="UP")
        _seed_settled(store, "win-down", asset="SOL", horizon=5, probability=0.30,
                      baseline=0.40 / 1.01,
                      features_json=canonical(_features(0.40, 0.61)),
                      direction="DOWN")
        # Settled and Brier-counted, but its stored book is not its
        # issuance book: never priced.
        _seed_settled(store, "wrong-book", asset="ETH", horizon=5, probability=0.65,
                      baseline=0.99,
                      features_json=canonical(_features(0.75, 0.26)),
                      direction="UP")
        # Settled with no recorded book at all: never priced.
        _seed_settled(store, "no-book", asset="XRP", horizon=5, probability=0.7,
                      baseline=0.5, features_json="[]", direction="UP")
        report = service.learner.summary()
        assert report["settled_forward_contracts"] == 4
        assert report["after_fee_calls"] == 2
        expected = (0.236875 + (1 - 0.61 - 0.07 * 0.61 * 0.39)) / 2 * 100
        assert report["after_fee_mean_net_cents"] == pytest.approx(expected)
        scoped = service.learner.summary(asset="BTC", horizon=5)
        assert scoped["after_fee_calls"] == 1
        assert scoped["after_fee_mean_net_cents"] == pytest.approx(23.6875)
    finally:
        store.close()


def _overview_field(polymarket_changes):
    reports = {**_reports(), "xmarket": _xmarket(),
               "polymarket": {**_reports()["polymarket"], **polymarket_changes}}
    embeds = build("BTC", 5, reports, section="overview", now=NOW,
                   venues=("hyperliquid",),
                   details_renderer=home_module.performance_embeds)
    return next(f.value for f in embeds[0].fields
                if f.name == "Polymarket · last 7 days")


def test_rendered_line_full_coverage():
    value = _overview_field({"after_fee_calls": 2001,
                             "after_fee_mean_net_cents": -2.23})
    assert ("After-fee money result: -2.2¢ per call over 2,001 graded calls "
            "(last 7 days)") in value


def test_rendered_line_partial_coverage_states_both_counts():
    value = _overview_field({"after_fee_calls": 1900,
                             "after_fee_mean_net_cents": 0.44})
    assert ("After-fee money result: +0.4¢ per call over 1,900 of 2,001 "
            "graded calls (last 7 days)") in value


def test_rendered_line_falls_back_to_the_plain_gap():
    assert after_fee_line({"polymarket": {"settled_forward_contracts": 12,
                                          "after_fee_calls": 0,
                                          "after_fee_mean_net_cents": None}}) \
        == "After-fee money result: not measured yet"
    assert after_fee_line({"polymarket": {}}) \
        == "After-fee money result: not measured yet"
    assert after_fee_line({}) == "After-fee money result: not measured yet"
