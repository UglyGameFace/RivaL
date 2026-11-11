"""The Model Lab: designs, live learning without look-ahead, review and refill."""
from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from parlay_bot.perpetuals import model_lab
from parlay_bot.perpetuals.features import LabeledExample
from parlay_bot.perpetuals.model_lab import (
    SEEDS,
    SLOTS_PER_HORIZON,
    SPACE,
    LabLearners,
    ModelLab,
    canonical,
    describe,
    family_of,
    propose,
    search_bar,
)
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    s = SQLiteHotStore(tmp_path / "lab.sqlite")
    yield s
    s.close()


def _candles(n=60, trend=True):
    start = NOW - timedelta(minutes=5 * n)
    price, out = 100.0, []
    for i in range(n):
        price += 0.5 if trend else (0.5 if i % 2 else -0.5)
        opened = start + timedelta(minutes=5 * i)
        out.append(SimpleNamespace(opened_at=opened, observed_at=opened + timedelta(minutes=5),
                                   close_price=price))
    return out


def _samples(n=200, *, horizon=5, end=NOW):
    rng = random.Random(7)
    out = []
    for i in range(n):
        issued = end - timedelta(minutes=5 * (n - i) + horizon)
        f0 = rng.uniform(-2, 2)
        features = (f0, *[rng.uniform(-1, 1) for _ in range(9)])
        out.append(LabeledExample("okx", "BTC-USDT-SWAP", "USDT", horizon, issued,
                                  issued + timedelta(minutes=horizon), features,
                                  int(f0 > 0)))
    return out


def test_a_design_has_one_stable_name_and_a_plain_description():
    genome = dict(SEEDS[0])
    assert family_of(genome, 5) == family_of(dict(genome), 5)
    assert family_of(genome, 5) != family_of(genome, 15)
    assert family_of(genome, 5).startswith("lab_5m_") and len(family_of(genome, 60)) <= 64
    assert "every market" in describe(genome)
    with pytest.raises(ValueError):
        canonical({**genome, "lr": 0.3}, 5)          # outside the search space


def test_the_search_starts_with_seeds_then_never_repeats_a_design():
    tried, rng = [], random.Random(1)
    for _ in range(40):
        genome, _parent = propose(15, tried, rng)
        family = family_of(genome, 15)
        assert family not in {t["family"] for t in tried}
        assert all(genome[d] in SPACE[d] or tuple(genome[d]) in SPACE[d] for d in SPACE)
        tried.append({"family": family, "genome": canonical(genome, 15),
                      "fitness": rng.uniform(-0.01, 0.01)})
    assert [t["family"] for t in tried[:3]] == [family_of(s, 15) for s in SEEDS]


def test_the_search_mostly_builds_on_the_best_design():
    best = canonical(dict(SEEDS[2]), 5)
    tried = [{"family": family_of(s, 5), "genome": canonical(s, 5), "fitness": -0.01}
             for s in SEEDS[:2]]
    tried.append({"family": family_of(SEEDS[2], 5), "genome": best, "fitness": 0.02})
    parents = [propose(5, tried, random.Random(seed))[1] for seed in range(60)]
    from_best = sum(p == family_of(SEEDS[2], 5) for p in parents)
    assert from_best > 30                    # mostly small changes to the winner
    assert any(p is None for p in parents)   # and some fresh ideas


def test_trying_more_designs_needs_stronger_evidence():
    pairs = []
    for day in range(21):
        for k in range(10):
            stamp = (NOW - timedelta(days=day, minutes=5 * k)).isoformat()
            gain = 0.009 if day % 3 else -0.003
            pairs.append((stamp, 0.25 - gain, 0.25))
    one = search_bar(pairs, tried=1)
    many = search_bar(pairs, tried=500)
    assert one["z"] == pytest.approx(2.576, abs=0.01)
    assert many["z"] > one["z"] + 1
    assert one["passed"] and not many["passed"]


def test_a_learner_learns_only_from_graded_labels_and_abstains_off_regime(store):
    learners = LabLearners(store)
    genome = {"inputs": ("momentum",), "shape": "linear", "lr": 0.1, "l2": 0.0001,
              "regime": "any", "shrink": 1.0, "warm": "zero"}
    family = family_of(genome, 5)
    future = LabeledExample("okx", "BTC-USDT-SWAP", "USDT", 5, NOW - timedelta(minutes=1),
                            NOW + timedelta(minutes=4), (-5.0,) + (0.0,) * 9, 1)
    calls = learners.calls(genomes={family: genome}, candles=_candles(),
                           samples=[*_samples(), future], features=(2.0,) + (0.0,) * 9,
                           incumbent_weights=None, venue="okx", contract="BTC-USDT-SWAP",
                           quote="USDT", horizon=5, now=NOW)
    assert len(calls) == 1 and calls[0]["family"] == family
    assert calls[0]["probability"] > 0.8                # learned "momentum up = up"
    state = learners._states[("okx", "BTC-USDT-SWAP", "USDT", 5, family)]
    assert state["updates"] == 200                      # the ungraded label was skipped
    assert learners.flush() == 1 and learners.flush() == 0   # one save, then nothing new
    # Saved and restored after a restart: same weights.
    again = LabLearners(store)
    again._load(("okx", "BTC-USDT-SWAP", "USDT", 5), [family])
    restored = again._states[("okx", "BTC-USDT-SWAP", "USDT", 5, family)]
    assert restored["weights"] == pytest.approx(state["weights"])
    # A trend-only design stays quiet in a choppy market.
    quiet = {**genome, "regime": "trend"}
    assert learners.calls(genomes={family_of(quiet, 5): quiet}, candles=_candles(trend=False),
                          samples=_samples(), features=(2.0,) + (0.0,) * 9,
                          incumbent_weights=None, venue="okx", contract="BTC-USDT-SWAP",
                          quote="USDT", horizon=5, now=NOW) == []


def test_a_warm_start_copies_only_the_inputs_it_reads(store):
    learners = LabLearners(store)
    genome = {"inputs": ("volume",), "shape": "linear", "lr": 0.01, "l2": 0.0001,
              "regime": "any", "shrink": 1.0, "warm": "main"}
    family = family_of(genome, 5)
    learners.calls(genomes={family: genome}, candles=_candles(), samples=[],
                   features=(0.0,) * 10, incumbent_weights=tuple(range(11)),
                   venue="okx", contract="BTC-USDT-SWAP", quote="USDT", horizon=5, now=NOW)
    weights = learners._states[("okx", "BTC-USDT-SWAP", "USDT", 5, family)]["weights"]
    assert weights[0] == 0 and weights[5] == 5 and weights[10] == 10  # bias + volume inputs
    assert weights[1] == 0 and weights[6] == 0                          # others start at 0


def _shadow_rows(store, family, *, gain, days=3, per_day=150, venue="okx"):
    from parlay_bot.perpetuals.model_competition import ShadowModelCompetition

    ShadowModelCompetition(store).initialize()
    with store.connect() as conn:
        for day in range(days):
            for k in range(per_day):
                target = NOW - timedelta(days=day, minutes=5 * k + 10)
                conn.execute(
                    """INSERT INTO rival_model_shadow_forecasts (venue,contract,quote,asset,
                         horizon_minutes,family,reference_opened_at,issued_at,target_opened_at,
                         probability_up,incumbent_up,baseline_up,training_count,
                         training_sha256,settled_at,outcome_up,model_brier,incumbent_brier)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (venue, "BTC-USDT-SWAP", "USDT", "BTC", 5, family,
                     (target - timedelta(minutes=5)).isoformat(),
                     (target - timedelta(minutes=1)).isoformat(), target.isoformat(),
                     .6, .5, .5, 100, "a" * 64, (target + timedelta(minutes=5)).isoformat(),
                     1, 0.25 - gain + (0.002 if k % 2 else -0.002), 0.25))


def test_review_fills_slots_closes_a_loser_and_replaces_it(store):
    lab = ModelLab(store)
    opened = lab.review(NOW - timedelta(days=4))["opened"]
    assert len(opened) == SLOTS_PER_HORIZON * 3            # 3 timeframes
    five = sorted(lab.active(5))
    loser = five[0]
    _shadow_rows(store, loser, gain=-0.02)
    changed = lab.review(NOW)
    assert (loser, "clearly losing to the main model") in changed["closed"]
    assert loser not in lab.active(5) and len(lab.active(5)) == SLOTS_PER_HORIZON
    replacement = [f for f in changed["opened"] if f.startswith("lab_5m_")]
    assert len(replacement) == 1 and replacement[0] != loser
    assert loser in model_lab.closed_families()
    from parlay_bot.discord_app.trial_board import is_retired, short_name

    assert is_retired(loser) and not is_retired(replacement[0])
    assert short_name(loser) == f"Lab {loser.split('_')[2][:6]}"
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM rival_lab_genomes WHERE family=?", (loser,)).fetchone()
    assert row["status"] == "closed" and row["results"] == 450 and row["fitness"] < 0
    # The lab learned something about 5-minute designs.
    summary = lab.summary()[5]
    assert summary["tried"] == SLOTS_PER_HORIZON + 1 and summary["closed"] == 1
    assert summary["best"] is None        # a closed design is never named "best"


def test_a_design_that_passes_route_b_keeps_running_without_a_slot(store):
    lab = ModelLab(store)
    lab.review(NOW - timedelta(days=1))
    winner = min(lab.active(15))
    from parlay_bot.perpetuals.family_governance import FAMILY_PROMOTION_SCHEMA

    with store.connect() as conn:
        conn.execute(FAMILY_PROMOTION_SCHEMA)
        conn.execute(
            """INSERT INTO rival_family_promotion (asset,venue,contract,quote,horizon_minutes,
                 selected_family,status,reason,evaluated_at,evidence_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("BTC", "okx", "BTC-USDT-SWAP", "USDT", 15, winner, "promoted_paper", "test",
             NOW.isoformat(), json.dumps({winner: {"qualified": True}})))
    changed = lab.review(NOW)
    assert changed["passed"] == [winner]
    assert winner in lab.active(15)                        # still issuing
    assert len(lab.active(15)) == SLOTS_PER_HORIZON + 1    # and its slot was refilled


def test_lab_designs_never_raise_regression_incidents():
    from parlay_bot.perpetuals.runtime_health import RuntimeHealthMonitor

    monitor = RuntimeHealthMonitor.__new__(RuntimeHealthMonitor)
    scores = [{"venue": "okx", "contract": "BTC", "quote": "USDT", "family": fam, "n": 500,
               "model_score": .30, "incumbent_score": .25,
               "last_issued": NOW.isoformat()} for fam in ("lab_5m_abcdef0123", "flow_v3")]
    faults = monitor._verdict(scores, NOW)
    assert list(faults) == ["regression:okx:BTC:flow_v3"]


def test_the_forecast_cycle_issues_lab_calls_beside_the_incumbent(tmp_path):
    """End to end: the lab opens designs, the real cycle trains them on the
    stream's graded labels and stores their calls in the shared ledger."""
    from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore
    from tests.test_regime_live import _cycle_counts, _seed_shape_candles

    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "lab-cycle.sqlite")
    market = PerpetualMarketStore(store)
    try:
        _seed_shape_candles(market, now=now, shape="trend")
        monitor = ForwardAccuracyMonitor(market)
        monitor._model_lab().review(now)
        monitor.run_once(now=now)
        with store.connect() as conn:
            rows = conn.execute(
                "SELECT family, horizon_minutes FROM rival_model_shadow_forecasts "
                "WHERE family LIKE 'lab_%'").fetchall()
            learners = conn.execute("SELECT COUNT(*) AS n FROM rival_lab_learners").fetchone()
        assert rows, "the lab issued no calls"
        assert all(r["family"].startswith(f"lab_{r['horizon_minutes']}m_") for r in rows)
        assert learners["n"] >= 1
        assert _cycle_counts(store, "regime_live_v1")[1] >= 1    # incumbent still ran
    finally:
        store.close()


def test_bot_performance_names_the_best_model_per_timeframe_and_the_lab():
    from parlay_bot.discord_app.home import _best_text
    from parlay_bot.discord_app.trial_board import TrialRow, best

    ahead = TrialRow(name="Trend learn", days=3, results=140, gain=.0071, ahead=1,
                     exchanges=1, stopped=0, passed=False, after_fees_bps=None)
    small = TrialRow(name="Calibrated", days=8, results=1855, gain=.0019, ahead=1,
                     exchanges=1, stopped=0, passed=False, after_fees_bps=None)
    behind = TrialRow(name="Naive Bayes", days=12, results=2873, gain=-.0053, ahead=0,
                      exchanges=1, stopped=0, passed=False, after_fees_bps=None)
    assert best([small, ahead, behind]) is ahead
    assert best([behind]) is None
    lab = {h: {"tried": 4, "active": [{}, {}, {}], "closed": 1, "passed": [],
               "best": ({"family": "lab_5m_3f9a2c11aa", "fitness": .002} if h == 5 else None),
               "learned": {}} for h in (5, 15, 60)}
    text = _best_text({"best": {"best": {5: ahead, 15: None, 60: "error"}, "lab": lab}})
    lines = text.splitlines()
    assert lines[0] == "**5m** · **Trend learn** +7.1 edge · 140 results over 3 days, not proven yet"
    assert lines[1] == "**15m** · no test model is ahead of the main model yet"
    assert lines[2] == "**60m** · couldn't load right now"
    assert lines[3].startswith("🧬 Model Lab: 9 designs testing, 3 closed so far")
    assert "Leading so far: **Lab 3f9a2c** on 5-min (early" in lines[3]
    assert _best_text({"best": RuntimeError("slow")}) is None
    assert _best_text({"best": {"best": {5: None, 15: None, 60: None}}}) is None
