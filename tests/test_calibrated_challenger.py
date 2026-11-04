"""Calibrated incumbent: causal Brier-optimal shrink toward the up-rate."""
from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.calibrated_challenger import (
    FAMILY,
    MIN_GRADED,
    CalibratedIncumbent,
    shrink,
)
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 4, 19, 0, tzinfo=UTC)


def _graded(conn, n, *, incumbent, outcome, settled, asset="BTC", horizon=5, start=0):
    for i in range(start, start + n):
        ref = (NOW - timedelta(days=3) + timedelta(minutes=i)).isoformat()
        conn.execute(
            """INSERT INTO rival_model_shadow_forecasts (venue,contract,quote,asset,
               horizon_minutes,family,reference_opened_at,issued_at,target_opened_at,
               probability_up,incumbent_up,baseline_up,training_count,training_sha256,
               settled_at,outcome_up) VALUES ('okx','BTC-USDT-SWAP','USDT',?,?,
               'gaussian_nb_v1',?,?,?,0.5,?,0.5,1,'x',?,?)""",
            (asset, horizon, ref, ref, ref, incumbent(i), settled.isoformat(), outcome(i)))


def _store(tmp_path):
    store = SQLiteHotStore(tmp_path / "calib.sqlite")
    ShadowModelCompetition(store).initialize()
    return store


def test_shrink_is_the_least_squares_optimum():
    rng = random.Random(7)
    q = [rng.random() for _ in range(400)]
    y = [int(rng.random() < .5 + .2 * (p - .5)) for p in q]  # a weak, real signal
    dd = sum((p - .5) ** 2 for p in q)
    de = sum((p - .5) * (.5 - t) for p, t in zip(q, y, strict=True))
    a = shrink(dd, de)

    def brier(k):
        return sum((.5 + k * (p - .5) - t) ** 2 for p, t in zip(q, y, strict=True))
    assert 0 < a < 1
    assert brier(a) <= min(brier(a - .05), brier(a + .05), brier(1.0), brier(0.0))
    assert shrink(0.0, 1.0) == 0.0 and shrink(1.0, -5.0) == 1.0 and shrink(1.0, 5.0) == 0.0


def test_overconfident_incumbent_is_pulled_toward_the_up_rate(tmp_path):
    store = _store(tmp_path)
    try:
        with store.connect() as conn:  # says 90%/10%, right 80% of the time
            _graded(conn, 1000, incumbent=lambda i: .9 if i % 2 else .1,
                    outcome=lambda i: int((i % 2 == 1) == (i % 5 != 0)),
                    settled=NOW - timedelta(hours=1))
        calib = CalibratedIncumbent(store)
        calib.refit(NOW, {"BTC"})
        call = calib.call(asset="BTC", horizon=5, incumbent_up=.9, baseline_up=.5)
        assert call["family"] == FAMILY and call["training_rows"] == 1000
        assert abs(call["shrink"] - .75) < 1e-9  # exact least squares: 0.3 real edge / 0.4 claimed
        assert abs(call["probability"] - .8) < 1e-9
    finally:
        store.close()


def test_never_fits_on_outcomes_graded_after_the_clock_and_needs_enough_history(tmp_path):
    store = _store(tmp_path)
    try:
        with store.connect() as conn:  # perfectly right, but graded in the future
            _graded(conn, MIN_GRADED + 50, incumbent=lambda i: .9 if i % 2 else .1,
                    outcome=lambda i: i % 2, settled=NOW + timedelta(minutes=5))
            _graded(conn, MIN_GRADED - 1, incumbent=lambda i: .9 if i % 2 else .1,
                    outcome=lambda i: i % 2, settled=NOW - timedelta(hours=1),
                    asset="ETH", start=MIN_GRADED + 50)
        calib = CalibratedIncumbent(store)
        calib.refit(NOW, {"BTC", "ETH"})
        for asset in ("BTC", "ETH"):  # no usable history -> says the up-rate
            call = calib.call(asset=asset, horizon=5, incumbent_up=.9, baseline_up=.53)
            assert call["shrink"] == 0.0 and call["probability"] == .53
    finally:
        store.close()


def test_calibrated_call_is_issued_and_quote_shadowed_like_any_family(tmp_path):
    from parlay_bot.perpetuals.model_competition import CHALLENGER_PREFIXES

    assert FAMILY.startswith(CHALLENGER_PREFIXES)
    store = _store(tmp_path)
    try:
        with store.connect() as conn:
            _graded(conn, 1000, incumbent=lambda i: .9 if i % 2 else .1,
                    outcome=lambda i: int((i % 2 == 1) == (i % 5 != 0)),
                    settled=NOW - timedelta(hours=1))
        calib = CalibratedIncumbent(store)
        calib.refit(NOW, {"BTC"})
        call = calib.call(asset="BTC", horizon=5, incumbent_up=.9, baseline_up=.5)
        with store.connect() as conn:
            assert ShadowModelCompetition(store).issue(
                conn, samples=[], features=(0.,) * 10, now=NOW,
                venue="hyperliquid", contract="BTC", quote="USDC", asset="BTC",
                horizon=5, reference_opened_at=NOW,
                target_opened_at=NOW + timedelta(minutes=5), issued_at=NOW,
                incumbent_up=.9, baseline_up=.5, challengers=(call,)) >= 1
            row = conn.execute("SELECT * FROM rival_model_shadow_forecasts WHERE family=?",
                               (FAMILY,)).fetchone()
        assert abs(row["probability_up"] - .8) < 1e-9 and row["settled_at"] is None
    finally:
        store.close()
