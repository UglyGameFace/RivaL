"""Cross-exchange test models (cross_v1, cross_all_v1).

cross_v1: one learner per coin and horizon that reads every exchange at
once. cross_all_v1: one learner per horizon shared by all coins. Both are
shadow only, learn one lesson per target from the majority outcome, issue
nothing during their first day of lessons, and go through the same per-stream
grading as every other test model.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.answer_cards import rename_text
from parlay_bot.perpetuals import cross_model, forward_audit
from parlay_bot.perpetuals.cross_model import (
    ALL_FAMILY,
    FAMILY,
    MIN_UPDATES,
    CrossModel,
    artifact_identity,
    cross_features,
    live_continuity,
    majority_label,
    model_inputs,
)
from parlay_bot.perpetuals.forward_audit import MODEL, ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import CHALLENGER_PREFIXES
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
VENUES = (("hyperliquid", "BTC", "USDC"), ("okx", "BTC-USDT-SWAP", "USDT"),
          ("bitget", "BTCUSDT", "USDT"))


def _vector(last=0.1, five=0.3):
    return (last, five, 0.2, 0.3, 0.0, 0.5, 0.1, 0.4, 0.3, 0.1)


# --- Inputs ------------------------------------------------------------------

def test_consensus_inputs_are_medians_plus_how_much_exchanges_disagree():
    features = cross_features([_vector(0.1, 0.3), _vector(0.3, 0.5), _vector(-0.2, 0.1)])
    assert len(features) == 14
    assert features[0] == pytest.approx(0.1) and features[1] == pytest.approx(0.3)
    assert features[10] == pytest.approx(0.5)            # last-return spread
    assert features[11] == pytest.approx(2 / 3 * 2 - 1)  # two of three went up
    assert features[12] == pytest.approx(0.4)            # five-bar spread
    assert features[13] == pytest.approx(3 / 9)
    assert all(abs(x) <= 5 for x in features)


def test_fewer_than_three_usable_exchanges_is_no_input():
    assert cross_features([_vector(), _vector()]) is None
    # A vector outside the main model's input bound is not usable.
    bad = (float("nan"), *_vector()[1:])
    assert cross_features([_vector(), _vector(), bad]) is None
    assert cross_features([_vector(), _vector(), (9.0, *_vector()[1:])]) is None
    assert cross_features(None) is None


def test_one_odd_exchange_cannot_move_the_consensus():
    calm = cross_features([_vector(0.1), _vector(0.1), _vector(0.1)])
    odd = cross_features([_vector(0.1), _vector(0.1), _vector(4.9)])
    assert odd[0] == calm[0]          # the median ignores it
    assert odd[10] > calm[10]         # the disagreement input reports it


def test_the_shared_learner_also_sees_which_coin_it_is():
    features = cross_features([_vector()] * 3)
    assert len(model_inputs(FAMILY, "BTC", features)) == 15
    shared = model_inputs(ALL_FAMILY, "SOL", features)
    assert len(shared) == 19 and shared[-4:] == (0.0, 0.0, 1.0, 0.0)


@pytest.mark.parametrize(("graded", "ups", "label"), [
    (3, 3, 1), (3, 2, 1), (3, 1, 0), (9, 0, 0),
    (4, 2, None),   # a tie teaches nothing
    (2, 2, None),   # too few exchanges graded
])
def test_the_lesson_is_the_majority_outcome(graded, ups, label):
    assert majority_label(graded, ups) == label


def test_family_names_are_registered_everywhere_they_must_be():
    assert "cross_" in CHALLENGER_PREFIXES
    assert forward_audit.ISSUE_CROSS_V1_CHALLENGERS is True
    assert rename_text("cross_v1 beat cross_all_v1") == (
        "all-exchanges test beat all-coins test")


# --- Issuing and learning on a real ledger -----------------------------------

def _store(tmp_path, name="cross.sqlite") -> SQLiteHotStore:
    store = SQLiteHotStore(tmp_path / name)
    store.initialize()
    ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
    return store


def _pending(reference, *, asset="BTC", horizon=5, last=0.1, venues=VENUES):
    contract = {"BTC": {}, "ETH": {"BTC": "ETH", "BTC-USDT-SWAP": "ETH-USDT-SWAP",
                                    "BTCUSDT": "ETHUSDT"}}[asset]
    return [{"venue": venue, "contract": contract.get(name, name), "quote": quote,
             "asset": asset, "horizon": horizon, "reference_opened_at": reference,
             "target_opened_at": reference + timedelta(minutes=horizon),
             "incumbent_up": 0.55, "baseline_up": 0.5, "features": _vector(last)}
            for venue, name, quote in venues]


def _grade(store, pending, outcomes):
    """Store the main model's graded rows for these streams."""
    with store.connect() as conn:
        for entry, outcome in zip(pending, outcomes):
            if outcome is None:
                continue
            reference = entry["reference_opened_at"]
            conn.execute(
                """INSERT INTO perp_forward_audit (
                     venue,contract,quote,asset,horizon_minutes,model_version,
                     reference_opened_at,reference_close,issued_at,target_opened_at,
                     probability_up,baseline_up,training_evaluated,settled_at,outcome_up)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (entry["venue"], entry["contract"], entry["quote"], entry["asset"],
                 entry["horizon"], MODEL, reference.isoformat(), 100.0,
                 reference.isoformat(), entry["target_opened_at"].isoformat(),
                 0.55, 0.5, 100,
                 (entry["target_opened_at"] + timedelta(minutes=6)).isoformat(), outcome))


def _teach(store, lessons, *, start, asset="BTC", rule=lambda i: i % 2):
    """``lessons`` graded targets where a positive last return means UP."""
    model = CrossModel(store)
    for i in range(lessons):
        reference = start + timedelta(minutes=5 * i)
        label = rule(i)
        pending = _pending(reference, asset=asset, last=0.4 if label else -0.4)
        model.issue(pending, issued_at=reference + timedelta(seconds=30))
        _grade(store, pending, [label] * len(pending))
    return model.learn(start + timedelta(minutes=5 * lessons + 30), model_version=MODEL)


def _shadow_rows(store, where="1=1", args=()):
    with store.connect() as conn:
        return [dict(row) for row in conn.execute(
            f"""SELECT venue,family,probability_up,training_count,training_sha256
                FROM rival_model_shadow_forecasts WHERE {where}
                ORDER BY family,venue""", args)]


def test_nothing_is_issued_until_a_learner_has_a_day_of_lessons(tmp_path):
    store = _store(tmp_path)
    try:
        start = NOW - timedelta(days=2)
        assert _teach(store, MIN_UPDATES - 1, start=start) == MIN_UPDATES - 1
        # Lessons were recorded and learned, but no shadow row exists yet.
        assert _shadow_rows(store) == []
        status = {(s["family"], s["scope"]): s for s in CrossModel(store).status()}
        assert status[(FAMILY, "BTC")]["updates"] == MIN_UPDATES - 1
        assert status[(FAMILY, "BTC")]["issuing"] is False
        assert status[(FAMILY, "BTC")]["lessons_needed"] == 1
    finally:
        store.close()


def test_a_taught_learner_issues_one_call_on_every_contributing_exchange(tmp_path):
    store = _store(tmp_path, "issue.sqlite")
    try:
        start = NOW - timedelta(days=2)
        _teach(store, MIN_UPDATES + 10, start=start)
        reference = NOW - timedelta(minutes=5)
        saved = CrossModel(store).issue(
            _pending(reference, last=0.4), issued_at=reference + timedelta(seconds=40))
        assert saved == 6  # two families on three exchanges
        rows = _shadow_rows(store, "reference_opened_at=?", (reference.isoformat(),))
        assert [(r["family"], r["venue"]) for r in rows] == [
            (ALL_FAMILY, "bitget"), (ALL_FAMILY, "hyperliquid"), (ALL_FAMILY, "okx"),
            (FAMILY, "bitget"), (FAMILY, "hyperliquid"), (FAMILY, "okx")]
        per_family = {r["family"]: r for r in rows}
        for family in (FAMILY, ALL_FAMILY):
            mine = [r for r in rows if r["family"] == family]
            assert len({r["probability_up"] for r in mine}) == 1   # one call
            assert len(per_family[family]["training_sha256"]) == 64
            assert per_family[family]["training_count"] == MIN_UPDATES + 10
            # It learned the lesson it was taught: a rise means UP.
            assert per_family[family]["probability_up"] > 0.5
        down = NOW
        CrossModel(store).issue(_pending(down, last=-0.4),
                                issued_at=down + timedelta(seconds=40))
        assert all(r["probability_up"] < 0.5 for r in _shadow_rows(
            store, "reference_opened_at=?", (down.isoformat(),)))
    finally:
        store.close()


def test_two_exchanges_are_not_enough_to_issue_or_to_learn(tmp_path):
    store = _store(tmp_path, "thin.sqlite")
    try:
        reference = NOW - timedelta(hours=1)
        assert CrossModel(store).issue(
            _pending(reference, venues=VENUES[:2]),
            issued_at=reference + timedelta(seconds=30)) == 0
        with store.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM rival_cross_model_examples").fetchone()["n"] == 0
    finally:
        store.close()


def test_nine_exchanges_agreeing_is_one_lesson_and_a_tie_is_none(tmp_path):
    store = _store(tmp_path, "lessons.sqlite")
    try:
        nine = tuple((f"venue{i}", "BTC", "USDT") for i in range(9))
        first, tied = NOW - timedelta(hours=2), NOW - timedelta(hours=1)
        model = CrossModel(store)
        agree = _pending(first, venues=nine)
        split = _pending(tied, venues=nine[:4])
        model.issue(agree, issued_at=first + timedelta(seconds=30))
        model.issue(split, issued_at=tied + timedelta(seconds=30))
        _grade(store, agree, [1] * 9)
        _grade(store, split, [1, 1, 0, 0])
        assert model.learn(NOW, model_version=MODEL) == 1
        assert model.learn(NOW, model_version=MODEL) == 0   # each target learns once
        with store.connect() as conn:
            labels = {row["reference_opened_at"]: row["label_up"] for row in conn.execute(
                "SELECT reference_opened_at,label_up FROM rival_cross_model_examples")}
        assert labels == {first.isoformat(): 1, tied.isoformat(): None}
        assert {s["updates"] for s in model.status()} == {1}
    finally:
        store.close()


def test_a_target_waits_for_its_exchanges_then_learns_from_those_that_graded(tmp_path):
    store = _store(tmp_path, "wait.sqlite")
    try:
        reference = NOW - timedelta(hours=7)
        nine = tuple((f"venue{i}", "BTC", "USDT") for i in range(4))
        model = CrossModel(store)
        pending = _pending(reference, venues=nine)
        model.issue(pending, issued_at=reference + timedelta(seconds=30))
        _grade(store, pending, [0, 0, 0, None])   # one exchange never grades
        # Inside the patience window the target keeps waiting.
        assert model.learn(reference + timedelta(hours=1), model_version=MODEL) == 0
        # Past it, the three graded exchanges decide.
        assert model.learn(NOW, model_version=MODEL) == 1
    finally:
        store.close()


def test_the_shared_learner_takes_lessons_from_every_coin(tmp_path):
    store = _store(tmp_path, "shared.sqlite")
    try:
        start = NOW - timedelta(hours=6)
        _teach(store, 20, start=start, asset="BTC")
        _teach(store, 30, start=start, asset="ETH")
        updates = {(s["family"], s["scope"]): s["updates"] for s in CrossModel(store).status()}
        assert updates == {(FAMILY, "BTC"): 20, (FAMILY, "ETH"): 30,
                           (ALL_FAMILY, "ALL"): 50}
    finally:
        store.close()


def test_a_corrupt_state_row_is_an_error_not_a_silent_restart(tmp_path):
    store = _store(tmp_path, "state.sqlite")
    try:
        _teach(store, 5, start=NOW - timedelta(hours=3))
        with store.connect() as conn:
            conn.execute("UPDATE rival_cross_model_state SET weights_json=?",
                         (json.dumps([0.0, 1.0]),))
        with pytest.raises(ValueError):
            CrossModel(store).issue(_pending(NOW - timedelta(minutes=5)),
                                    issued_at=NOW - timedelta(minutes=4))
    finally:
        store.close()


# --- Promotion continuity ----------------------------------------------------

def test_promotion_identity_only_moves_forward():
    earned = {"feature_digest": cross_model.FEATURE_DIGEST, "weights_sha256": "a" * 64,
              "trained_through": NOW.isoformat(), "graded_count": 200}
    later = {**earned, "weights_sha256": "b" * 64, "graded_count": 260,
             "trained_through": (NOW + timedelta(hours=5)).isoformat()}
    assert live_continuity(earned, earned)
    assert live_continuity(earned, later)
    assert not live_continuity(earned, {**later, "graded_count": 150})
    assert not live_continuity(earned, {**later, "feature_digest": "0" * 64})
    assert not live_continuity(earned, {**earned, "weights_sha256": "b" * 64})
    assert not live_continuity(None, later) and not live_continuity(earned, None)


def test_identity_is_none_until_a_graded_row_exists(tmp_path):
    store = _store(tmp_path, "identity.sqlite")
    try:
        assert artifact_identity(store, FAMILY, "BTC", 5) is None
        assert artifact_identity(store, "liq_v1", "BTC", 5) is None
    finally:
        store.close()


# --- The 5-minute cycle ------------------------------------------------------

def _seed(market, *, now, venues=VENUES, bars=500):
    boundary = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
    market.initialize()
    with market.store.connect() as conn:
        for venue, contract, quote in venues:
            for i in range(bars):
                opened = boundary - timedelta(minutes=5 * (bars - i))
                closed = opened + timedelta(minutes=5, milliseconds=-1)
                price = 100 + (i % 7) * 0.3 + i * 0.01
                conn.execute(
                    """INSERT INTO perp_market_candles
                       (venue,contract,quote,interval_minutes,opened_at,closed_at,
                        first_observed_at,open_price,high_price,low_price,close_price,
                        volume_base,trades)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (venue, contract, quote, 5, opened.isoformat(), closed.isoformat(),
                     (closed + timedelta(milliseconds=1)).isoformat(),
                     price, price + 1, price - 1, price + 0.2, 10, 12))


def _counts(store):
    with store.connect() as conn:
        examples = conn.execute(
            "SELECT COUNT(*) AS n FROM rival_cross_model_examples").fetchone()["n"]
        incumbent = conn.execute(
            "SELECT COUNT(*) AS n FROM perp_forward_audit WHERE model_version=?",
            (MODEL,)).fetchone()["n"]
    return examples, incumbent


def test_the_cycle_records_one_example_per_target_and_the_switch_stops_it(
        tmp_path, monkeypatch):
    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "cycle.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        _seed(market, now=now)
        monkeypatch.setattr(forward_audit, "ISSUE_CROSS_V1_CHALLENGERS", False)
        monitor.run_once(now=now)
        CrossModel(store).initialize()
        examples_off, incumbent = _counts(store)
        assert examples_off == 0 and incumbent >= 3   # the main model ran
    finally:
        store.close()

    store = SQLiteHotStore(tmp_path / "cycle-on.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        _seed(market, now=now)
        monkeypatch.setattr(forward_audit, "ISSUE_CROSS_V1_CHALLENGERS", True)
        monitor.run_once(now=now)
        examples, incumbent = _counts(store)
        # Three exchanges issued each horizon: one example per horizon, not three.
        assert incumbent >= 3 and 1 <= examples == incumbent // 3
        with store.connect() as conn:
            row = conn.execute(
                "SELECT features_json,streams_json FROM rival_cross_model_examples "
                "LIMIT 1").fetchone()
        assert len(json.loads(row["features_json"])) == 14
        assert len(json.loads(row["streams_json"])) == 3
    finally:
        store.close()


def test_a_broken_cross_model_never_costs_the_main_model_its_cycle(tmp_path, monkeypatch):
    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "broken.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)

    def boom(*args, **kwargs):
        raise RuntimeError("cross model exploded")

    try:
        _seed(market, now=now)
        monkeypatch.setattr(CrossModel, "issue", boom)
        monkeypatch.setattr(CrossModel, "learn", boom)
        monitor.run_once(now=now)
        with store.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM perp_forward_audit WHERE model_version=?",
                (MODEL,)).fetchone()["n"] >= 3
    finally:
        store.close()
