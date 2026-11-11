"""The odds-lag learner: starts as the market, learns when the odds lag.

It enters the existing Polymarket contest as a new identity, judged like the
others against Polymarket's own odds with fees, and must never change the
four declared contestants' tests.
"""
from __future__ import annotations

import random
from dataclasses import replace
from datetime import timedelta

import pytest

from parlay_bot.perpetuals import polymarket_lag
from parlay_bot.perpetuals.model_league import (
    BOOK,
    FORMULA,
    LAG,
    MODELS,
    PRESSURE,
    _test_alpha,
    label,
    league,
    settlement_schema,
)
from parlay_bot.perpetuals.polymarket_formula_test import FormulaTest
from parlay_bot.perpetuals.polymarket_lag import (
    MIN_LABELS,
    PRIOR,
    OddsLagLearner,
    features,
    market_up,
)
from tests.test_model_league import CONDITION, SHA, START, notice, setup


def _round(store, contest, i, *, formula, outcome_up, horizon=60, pressure=None,
           resolve_delay=timedelta(minutes=1)):
    n = notice(start=START + timedelta(minutes=i * horizon), horizon=horizon)
    forecasts = {FORMULA: (formula, SHA), BOOK: (.5, SHA)}
    if pressure is not None:
        forecasts[PRESSURE] = (pressure, SHA)
    assert contest.capture(n, forecasts, now=n.quote_at)
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO rival_polymarket_ai_outcomes VALUES (?,?,?,?,?,?,?)",
            (n.slug, CONDITION, "UP" if outcome_up else "DOWN", n.expires.isoformat(),
             (n.expires + resolve_delay).isoformat(), SHA, "{}"))
    return n


def _lagging_market(store, contest, rounds, *, seed=7):
    """The market sits at 50%; the live-price formula knows better."""
    rng = random.Random(seed)
    last = None
    for i in range(rounds):
        formula = .8 if rng.random() < .5 else .2
        last = _round(store, contest, i, formula=formula, outcome_up=rng.random() < formula)
    return last


# --- Inputs ------------------------------------------------------------------

def test_inputs_are_the_market_and_how_far_the_live_price_disagrees():
    m = market_up(.49, .51, .49, .51)
    assert m == pytest.approx(.5)
    x = features(market=m, formula=.8, pressure=None, up_ask=.51, down_ask=.51)
    assert x[0] == 1 and x[1] == pytest.approx(0)            # bias, logit(market)
    assert x[2] == pytest.approx(1.3863, abs=1e-3)           # logit(.8) - logit(.5)
    assert x[3:5] == (0.0, 0.0)                              # no pressure forecast
    assert x[5] == pytest.approx(.2)                         # 2-cent spread, scaled
    assert features(market=m, formula=.8, pressure=.7, up_ask=.51, down_ask=.51)[4] == 1
    # Unusable books and forecasts are no input, never a guess.
    assert market_up(.6, .5, .4, .5) is None
    assert features(market=m, formula=float("nan"), pressure=None,
                    up_ask=.51, down_ask=.51) is None
    assert features(market=m, formula=.8, pressure=1.5, up_ask=.51, down_ask=.51) is None


def test_before_learning_anything_its_forecast_is_the_market_price():
    x = features(market=.62, formula=.9, pressure=.4, up_ask=.63, down_ask=.39)
    z = sum(w * v for w, v in zip(PRIOR, x))
    assert polymarket_lag._sigmoid(z) == pytest.approx(.62, abs=1e-9)


# --- Learning ----------------------------------------------------------------

def test_it_learns_to_lean_on_the_live_price_when_the_market_lags(learning_store):
    contest = setup(learning_store)
    last = _lagging_market(learning_store, contest, 400)
    learner = OddsLagLearner(learning_store)
    assert learner.learn(now=last.expires + timedelta(hours=1), force=True) == 400
    status = learner.status()
    assert len(status) == 1 and status[0]["updates"] == 400 and status[0]["issuing"]
    assert status[0]["weight_on_live_price"] > .3
    # Its call for a new contract moves from 50% toward the formula's 80%.
    nxt = notice(start=last.expires, horizon=60)
    p, sha = learner.forecast(nxt, settlement_schema(nxt), formula=.8, pressure=None,
                              now=nxt.quote_at)
    assert .55 < p < .8 and len(sha) == 64


def test_when_the_market_is_right_it_learns_nothing_to_add(learning_store):
    contest = setup(learning_store)
    rng = random.Random(3)
    last = None
    for i in range(400):  # the formula is noise; outcomes follow the 50% market
        last = _round(learning_store, contest, i, formula=rng.choice((.8, .2)),
                      outcome_up=rng.random() < .5)
    learner = OddsLagLearner(learning_store)
    learner.learn(now=last.expires + timedelta(hours=1), force=True)
    assert abs(learner.status()[0]["weight_on_live_price"]) < .15
    nxt = notice(start=last.expires, horizon=60)
    p, _ = learner.forecast(nxt, settlement_schema(nxt), formula=.8, pressure=None,
                            now=nxt.quote_at)
    assert abs(p - .5) < .05   # stays close to the market: no reason to trade


def test_nothing_is_issued_before_enough_resolutions(learning_store):
    contest = setup(learning_store)
    last = _lagging_market(learning_store, contest, MIN_LABELS - 1)
    learner = OddsLagLearner(learning_store)
    learner.learn(now=last.expires + timedelta(hours=1), force=True)
    nxt = notice(start=last.expires, horizon=60)
    assert learner.forecast(nxt, settlement_schema(nxt), formula=.8, pressure=None,
                            now=nxt.quote_at) is None


def test_it_never_learns_an_outcome_from_the_future(learning_store):
    contest = setup(learning_store)
    _round(learning_store, contest, 0, formula=.8, outcome_up=True,
           resolve_delay=timedelta(hours=5))
    learner = OddsLagLearner(learning_store)
    n0 = notice(start=START, horizon=60)
    # The result was published 5 hours after expiry: not learnable before then.
    assert learner.learn(now=n0.expires + timedelta(hours=1), force=True) == 0
    assert learner.learn(now=n0.expires + timedelta(hours=6), force=True) == 1
    # And each contract is learned once.
    assert learner.learn(now=n0.expires + timedelta(hours=7), force=True) == 0


def test_a_corrupt_state_is_an_error_not_a_silent_restart(learning_store):
    contest = setup(learning_store)
    last = _lagging_market(learning_store, contest, MIN_LABELS + 5)
    learner = OddsLagLearner(learning_store)
    learner.learn(now=last.expires + timedelta(hours=1), force=True)
    with learning_store.connect() as conn:
        conn.execute("UPDATE rival_poly_lag_state SET weights_json='[0,1,0,0,0,9]'")
    nxt = notice(start=last.expires, horizon=60)
    with pytest.raises(ValueError):
        learner.forecast(nxt, settlement_schema(nxt), formula=.8, pressure=None,
                         now=nxt.quote_at)


# --- The contest -------------------------------------------------------------

def test_it_enters_the_contest_without_touching_the_declared_models(learning_store):
    assert LAG not in MODELS and label(LAG) == "Odds-lag learner"
    slots = {LAG: 1}
    # The declared models' budget is exactly what it was.
    assert _test_alpha(BOOK, slots, 1) == _test_alpha(BOOK, {}, 1)
    # It draws from the reserved half, by enrollment order (pooled artifacts
    # already hold earlier slots in production, so its share is smaller).
    assert _test_alpha(LAG, slots, 1) > 0
    assert _test_alpha(LAG, {LAG: 5}, 1) < _test_alpha(BOOK, slots, 1)
    contest = setup(learning_store)
    n = notice(start=START, horizon=60)
    assert contest.capture(n, {FORMULA: (.6, SHA), LAG: (.55, SHA)}, now=n.quote_at)
    with learning_store.connect() as conn:
        models = {r["model"] for r in conn.execute(
            "SELECT model FROM rival_poly_contest_forecasts WHERE slug=?", (n.slug,))}
        slotted = {r["model"] for r in conn.execute(
            "SELECT model FROM rival_poly_contest_model_slots")}
    assert models == {FORMULA, LAG} and LAG in slotted


def test_it_is_scored_against_polymarkets_odds_like_every_contestant(learning_store):
    contest = setup(learning_store)
    rng = random.Random(11)
    last = None
    for i in range(30):
        n = notice(start=START + timedelta(minutes=i * 60), horizon=60)
        y = rng.random() < .5
        assert contest.capture(n, {FORMULA: (.5, SHA), LAG: (.9 if y else .1, SHA)},
                               now=n.quote_at)
        with learning_store.connect() as conn:
            conn.execute(
                "INSERT INTO rival_polymarket_ai_outcomes VALUES (?,?,?,?,?,?,?)",
                (n.slug, CONDITION, "UP" if y else "DOWN", n.expires.isoformat(),
                 (n.expires + timedelta(minutes=1)).isoformat(), SHA, "{}"))
        last = n
    scope = league(learning_store, now=last.expires + timedelta(minutes=5))[
        "markets"]["BTC", 60]["scopes"][settlement_schema(last)]
    row = next(m for m in scope["models"] if m["model"] == LAG)
    assert row["contracts"] == 30 and row["improvement"] > 0
    # 30 contracts are far short of the 300-contract bar: no false winner.
    assert scope["champion"] is None


class _Learner:
    def contest_forecasts(self, notice):
        return {}


def _reference(n):
    closes = [100 + .01 * i for i in range(130)]
    return {"quote_currency": "USD", "at": n.quote_at, "price": 101.0, "open": 100.0,
            "closes": closes, "sources": ["a", "b", "c"], "reference_label": "spot",
            "candles": [[i, c] for i, c in enumerate(closes)]}


def test_a_broken_lag_learner_never_costs_the_contest_its_round(learning_store, monkeypatch):
    setup(learning_store)
    test = FormulaTest(learning_store)

    def boom(*args, **kwargs):
        raise RuntimeError("lag learner exploded")

    monkeypatch.setattr(test.lag, "forecast", boom)
    n = replace(notice(start=START, horizon=60), price_to_beat=None)
    assert test.consider(n, reference=_reference(n), learner=_Learner(),
                         now=n.quote_at) is not None
    with learning_store.connect() as conn:
        models = {r["model"] for r in conn.execute(
            "SELECT model FROM rival_poly_contest_forecasts WHERE slug=?", (n.slug,))}
    assert FORMULA in models and LAG not in models


def test_once_taught_the_checkpoint_carries_its_forecast(learning_store):
    contest = setup(learning_store)
    last = _lagging_market(learning_store, contest, MIN_LABELS + 20)
    test = FormulaTest(learning_store)
    test.lag.learn(now=last.expires + timedelta(hours=1), force=True)
    n = replace(notice(start=last.expires + timedelta(hours=2), horizon=60),
                price_to_beat=None)
    assert test.consider(n, reference=_reference(n), learner=_Learner(),
                         now=n.quote_at) is not None
    with learning_store.connect() as conn:
        models = {r["model"] for r in conn.execute(
            "SELECT model FROM rival_poly_contest_forecasts WHERE slug=?", (n.slug,))}
    assert {FORMULA, LAG} <= models
