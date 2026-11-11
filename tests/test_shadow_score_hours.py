"""Stored hourly tallies give exactly the old 7-day aggregate, cheaply.

The watchdog's challenger check scanned every graded test-model row of the
last 7 days in one pass (60 s and a statement timeout on production). These
tests pin that the tally path returns the same rows and numbers as that
scan, builds itself over a few passes, reads still-grading hours live, and
re-tallies hours a no-trade withdrawal touched.
"""
from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals import shadow_score_hours as tally
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.perpetuals.runtime_health import RuntimeHealthMonitor
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 11, 13, 37, 12, tzinfo=UTC)
VENUES = ("okx", "bybit", "dydx")
FAMILIES = ("regime_v1", "cross_v1", "liq_v1")


@pytest.fixture
def store(tmp_path):
    hot = SQLiteHotStore(tmp_path / "tally.sqlite")
    ShadowModelCompetition(hot).initialize()
    with hot.connect() as conn:
        conn.execute("CREATE TABLE perp_feed_health (venue TEXT, asset TEXT)")
        for venue in VENUES:
            conn.execute("INSERT INTO perp_feed_health VALUES (?, 'BTC')", (venue,))
    yield hot
    hot.close()


def _rows(store, *, count=4000, seed=5, unsettled_recent=True, now=NOW,
          minutes=8 * 24 * 60):
    rng = random.Random(seed)
    with store.connect() as conn:
        for i in range(count):
            issued = now - timedelta(minutes=rng.randrange(0, minutes))
            horizon = rng.choice((5, 15, 60))
            settled = not (unsettled_recent and now - issued < timedelta(hours=3)
                           and rng.random() < .5)
            graded = settled and rng.random() > .05     # some rows withdrawn/NULL scores
            m, b = rng.random() * .5, rng.random() * .5
            conn.execute(
                """INSERT INTO rival_model_shadow_forecasts (venue,contract,quote,asset,
                     horizon_minutes,family,reference_opened_at,issued_at,target_opened_at,
                     probability_up,incumbent_up,baseline_up,training_count,training_sha256,
                     settled_at,outcome_up,model_brier,incumbent_brier,baseline_brier)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rng.choice(VENUES), "BTC-USD", "USD", "BTC", horizon, rng.choice(FAMILIES),
                 (issued - timedelta(minutes=5, microseconds=i)).isoformat(),
                 issued.isoformat(), (issued + timedelta(minutes=horizon)).isoformat(),
                 .5, .5, .5, 0, "s" * 64,
                 issued.isoformat() if settled else None, 1 if graded else None,
                 m if graded else None, b if graded else None, .25 if graded else None))


def _old(store, now):
    with store.connect() as conn:
        return RuntimeHealthMonitor._full_scan(conn, now)


def _same(a, b):
    key = ("venue", "contract", "quote", "horizon_minutes", "family")
    left = {tuple(r[k] for k in key): r for r in a}
    right = {tuple(r[k] for k in key): r for r in b}
    assert left.keys() == right.keys()
    for k, x in left.items():
        y = right[k]
        assert (int(x["n"]), int(x["days"]), str(x["last_issued"])) == (
            int(y["n"]), int(y["days"]), str(y["last_issued"])), k
        for col in ("model_score", "incumbent_score", "loss_square"):
            assert float(x[col]) == pytest.approx(float(y[col]), rel=1e-9, abs=1e-12), (k, col)


def _until_built(store, now, passes=40):
    for i in range(passes):
        scores = tally.window_scores(store, now)
        if scores is not None:
            return scores, i + 1
    raise AssertionError("window never completed")


def test_tallies_reproduce_the_old_seven_day_scan_exactly(store):
    _rows(store)
    scores, passes = _until_built(store, NOW)
    # 156 whole hours to tally, 12 per exchange per pass: built in 14 passes.
    assert passes == 14
    _same(scores, _old(store, NOW))
    # Time moves on: new forecasts are issued now (never into a past hour),
    # and the ones still grading get their results late.
    later = NOW + timedelta(minutes=10)
    _rows(store, count=300, seed=9, now=later, minutes=10)
    with store.connect() as conn:
        conn.execute("""UPDATE rival_model_shadow_forecasts SET settled_at=?, outcome_up=1,
                          model_brier=.2, incumbent_brier=.3, baseline_brier=.25
                        WHERE settled_at IS NULL""", (later.isoformat(),))
    _same(_until_built(store, later)[0], _old(store, later))


def test_a_still_grading_hour_is_read_live_not_frozen(store):
    _rows(store, count=0)
    issued = NOW - timedelta(hours=10)
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO rival_model_shadow_forecasts (venue,contract,quote,asset,
                 horizon_minutes,family,reference_opened_at,issued_at,target_opened_at,
                 probability_up,incumbent_up,baseline_up,training_count,training_sha256)
               VALUES ('okx','BTC-USD','USD','BTC',60,'regime_v1',?,?,?,.6,.5,.5,0,?)""",
            ((issued - timedelta(minutes=5)).isoformat(), issued.isoformat(),
             (issued + timedelta(minutes=60)).isoformat(), "s" * 64))
    assert _until_built(store, NOW)[0] == []              # not graded yet: nothing
    with store.connect() as conn:                          # graded late
        conn.execute("""UPDATE rival_model_shadow_forecasts SET settled_at=?, outcome_up=1,
                          model_brier=.16, incumbent_brier=.25, baseline_brier=.25""",
                     (NOW.isoformat(),))
    late = _until_built(store, NOW)[0]
    assert [(r["family"], r["n"]) for r in late] == [("regime_v1", 1)]
    _same(late, _old(store, NOW))


def test_a_withdrawal_re_tallies_the_hours_it_touched(store):
    _rows(store)
    _until_built(store, NOW)
    # Withdraw a stored hour's rows the way the no-trade correction does.
    with store.connect() as conn:
        target = (NOW - timedelta(days=2)).isoformat()[:13]
        conn.execute(
            """UPDATE rival_model_shadow_forecasts SET outcome_up=NULL, model_brier=NULL,
                 incumbent_brier=NULL, baseline_brier=NULL
               WHERE SUBSTR(issued_at,1,13)=?""", (target,))
        tally.invalidate(conn, since=(NOW - timedelta(days=2)).isoformat())
    _same(_until_built(store, NOW)[0], _old(store, NOW))


def test_without_an_exchange_list_the_caller_scans_the_small_ledger(tmp_path):
    hot = SQLiteHotStore(tmp_path / "new.sqlite")
    try:
        ShadowModelCompetition(hot).initialize()
        with pytest.raises(tally.NoVenues):
            tally.window_scores(hot, NOW)
    finally:
        hot.close()


def test_a_failed_refresh_waits_before_trying_again(store, monkeypatch):
    monitor = RuntimeHealthMonitor(store)
    monkeypatch.setattr(tally, "window_scores", lambda *_: (_ for _ in ()).throw(
        RuntimeError("statement timeout")))
    assert monitor.regressions_due(NOW)
    with pytest.raises(RuntimeError):
        monitor.refresh_regressions(now=NOW)
    assert not monitor.regressions_due(NOW + timedelta(minutes=2))   # no retry storm
    assert monitor.regressions_due(NOW + timedelta(minutes=11))


def test_while_building_the_background_refresh_keeps_its_last_verdict(store):
    _rows(store)
    monitor = RuntimeHealthMonitor(store)
    first = monitor.refresh_regressions(now=NOW)      # pass 1 of 14: still building
    assert first == {} and monitor.paired_scores()[0] == ()
