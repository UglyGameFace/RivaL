"""Regime live-learning challengers (spec rival-regime-live-learning-spec-20261108).

Covers: the two live families (regime_live_v1 at lr 0.025, regime_live_fast_v1
at lr 0.05 -- the Q1 amendment), the parameterized learning rate in
LiveModelCheckpoints, the one-time warm start from the incumbent (never
re-applied after restart), trending-only filtering (ER > 0.30 frozen at
issuance, recomputed deterministically from persisted candles), family
separation (separate weights, separate contracts, independent kill
switches, the fast rate moving faster), the "regime_" prefix accepted by
CHALLENGER_PREFIXES and issue() so rows are not silently dropped, the
selected() continuity branch (no D14 pin: no offline artifact), evaluate()
recording the artifact and qualifying on the issued subset with no pin
filter, the promotion receipt preserving the weights SHA, the D7 trial
report (paired gain, vs-v1 head-to-head, selection, coverage as two
ratios, updates, weight drift, 7-day vs 8-30-day gain split), the kill
switches in the 5-minute cycle (flag on issues, flag off issues nothing
new, choppy cycles abstain while the incumbent still runs, a learner-fit
or call failure never blocks the incumbent), D10 (offline regime_v2
retired: kill switch off, no longer in the weekly training loop), and
D11 (no OTG live lean for either family).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.out_the_gate import live_lean
from parlay_bot.perpetuals import forward_audit, regime_live_model
from parlay_bot.perpetuals.family_governance import FamilyPromotion
from parlay_bot.perpetuals.features import LabeledExample
from parlay_bot.perpetuals.forward_audit import MODEL as FORWARD_MODEL
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.hyperliquid import MarketCandle
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_checkpoints import LiveModelCheckpoints
from parlay_bot.perpetuals.model_competition import (
    CHALLENGER_PREFIXES,
    ShadowModelCompetition,
)
from parlay_bot.perpetuals.regime_gate import (
    efficiency_ratio,
    trending_gate,
)
from parlay_bot.perpetuals.regime_live_model import (
    BASE_FAMILY,
    BASE_FEATURE_CONTRACT,
    BASE_LEARNING_RATE,
    FAMILIES,
    FAST_FAMILY,
    FAST_FEATURE_CONTRACT,
    FAST_LEARNING_RATE,
    artifact_identity,
    feature_contract,
    feature_digest,
    learning_rate,
    metric_prefix,
    regime_live_calls,
    trending_samples,
    trial_report,
)
from parlay_bot.storage.hot import SQLiteHotStore
from tests.test_archive_v1_shadow import (
    _insert_shadow_row,
    _promotion_row,
    _store,
)
from tests.test_regime_v1 import (
    _insert_incumbent_row,
    _insert_regime_row,
    _seed_promotion,
    _seed_shape_candles,
)

NOW = datetime(2026, 11, 8, 12, 0, 30, tzinfo=UTC)


def _candles(closes, *, start=None, venue="hyperliquid"):
    """Consecutive closed 5-minute MarketCandles with the given closes."""
    start = start or (NOW - timedelta(minutes=5 * len(closes)))
    out = []
    for i, close in enumerate(closes):
        opened = start + timedelta(minutes=5 * i)
        out.append(MarketCandle(
            venue=venue, contract="BTC", quote="USDC", interval_minutes=5,
            opened_at=opened, closed_at=opened + timedelta(minutes=5),
            observed_at=opened + timedelta(minutes=5),
            open_price=float(close), high_price=float(close),
            low_price=float(close), close_price=float(close),
            volume_base=10, trades=12,
        ))
    return out


def _samples(now, *, count=80, start=None):
    first = start or (now - timedelta(minutes=500))
    return tuple(LabeledExample(
        venue="hyperliquid", contract="BTC", quote="USDC", horizon_minutes=5,
        issued_at=first + timedelta(minutes=5 * i),
        label_known_at=first + timedelta(minutes=5 * i + 6),
        features=(.1 if i % 2 else -.1, .2, .01, .03, .05, .2, .1, .1, .1, .1),
        label_up=i % 2,
    ) for i in range(count))


def _warm_start(weights, *, now):
    return {
        "weights": tuple(weights),
        "source": "live_model_checkpoints",
        "source_weights_sha256": hashlib.sha256(
            json.dumps(list(weights), separators=(",", ":"),
                       allow_nan=False).encode()).hexdigest(),
        "copied_at": now.isoformat(),
    }


def _insert_live_row(conn, *, target, issued, p, incumbent_p=0.6, y=1, ref=None,
                     settled=True, sha=None, family=BASE_FAMILY):
    """One settled live-family shadow row with explicit per-side probabilities.

    Mirrors _insert_regime_row from test_regime_v1, but the family is a
    parameter. The settled timestamp is 10 minutes after the target: a
    row settled before its target closes is invalid evidence (evaluate()
    and evaluate_regimes() both drop it).
    """
    ref = ref or (target - timedelta(minutes=5))
    settled_at = (target + timedelta(minutes=10)).isoformat() if settled else None
    conn.execute(
        """INSERT INTO rival_model_shadow_forecasts
           (venue,contract,quote,asset,horizon_minutes,family,
            reference_opened_at,issued_at,target_opened_at,
            probability_up,incumbent_up,baseline_up,
            training_count,training_sha256,settled_at,outcome_up,
            model_brier,incumbent_brier,baseline_brier)
           VALUES ('hyperliquid','BTC','USDC','BTC',5,?,?,?,?,?,?,?,100,?,
                   ?,?,?,?,?)""",
        (family, ref.isoformat(), issued.isoformat(), target.isoformat(),
         p, incumbent_p, 0.45, sha or "a" * 64, settled_at,
         y if settled else None,
         (p - y) ** 2 if settled else None,
         (incumbent_p - y) ** 2 if settled else None,
         (0.45 - y) ** 2 if settled else None),
    )


def _cycle_counts(store, family):
    with store.connect() as conn:
        shadow = conn.execute(
            "SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts "
            "WHERE family=?", (family,),
        ).fetchone()["n"]
        incumbent = conn.execute(
            "SELECT COUNT(*) AS n FROM perp_forward_audit "
            "WHERE model_version=?", (FORWARD_MODEL,),
        ).fetchone()["n"]
    return shadow, incumbent


# --- Config: families, contracts, rates --------------------------------------

def test_family_config():
    """Two families, separate contracts pinning the rate, 2x rate ratio."""
    assert FAMILIES == (BASE_FAMILY, FAST_FAMILY)
    assert BASE_FAMILY == "regime_live_v1"
    assert FAST_FAMILY == "regime_live_fast_v1"
    assert BASE_LEARNING_RATE == 0.025
    assert FAST_LEARNING_RATE == 0.05
    assert FAST_LEARNING_RATE == 2 * BASE_LEARNING_RATE
    assert BASE_FEATURE_CONTRACT != FAST_FEATURE_CONTRACT
    assert "trending-er20-gt0.30" in BASE_FEATURE_CONTRACT
    assert "trending-er20-gt0.30" in FAST_FEATURE_CONTRACT
    assert "lr0.025" in BASE_FEATURE_CONTRACT
    assert "lr0.05" in FAST_FEATURE_CONTRACT
    assert feature_contract(BASE_FAMILY) == BASE_FEATURE_CONTRACT
    assert feature_contract(FAST_FAMILY) == FAST_FEATURE_CONTRACT
    assert learning_rate(BASE_FAMILY) == BASE_LEARNING_RATE
    assert learning_rate(FAST_FAMILY) == FAST_LEARNING_RATE
    assert metric_prefix(BASE_FAMILY) == "regime_live"
    assert metric_prefix(FAST_FAMILY) == "regime_live_fast"
    assert feature_digest(BASE_FAMILY) != feature_digest(FAST_FAMILY)
    assert feature_digest(BASE_FAMILY) == hashlib.sha256(
        f"regime_live_v1|efficiency-ratio-20|er>0.30|"
        f"{BASE_FEATURE_CONTRACT}|10-features".encode()).hexdigest()


def test_learning_rate_validation(tmp_path):
    """The 0 < lr <= 0.1 bound from walk_forward.py is enforced here too."""
    hot = SQLiteHotStore(tmp_path / "lr.sqlite")
    try:
        with pytest.raises(ValueError):
            LiveModelCheckpoints(hot, learning_rate=0)
        with pytest.raises(ValueError):
            LiveModelCheckpoints(hot, learning_rate=0.2)
        LiveModelCheckpoints(hot, learning_rate=0.1)
        LiveModelCheckpoints(hot, learning_rate=0.025)
    finally:
        hot.close()


# --- D3: trending-only filtering ---------------------------------------------

def test_trending_filter_admits_only_trending_samples():
    """A sample trains iff its issuance bar's ER > 0.30, frozen at issuance.

    A 21-bar straight climb has ER 1.0 at the last bar (admitted); a
    21-bar 100/101 alternation has ER 0.0 (rejected); a 10-bar window
    has no 21-bar history at all (rejected). The filter agrees with
    trending_gate() on the same closes.
    """
    trend = _candles([100 + i for i in range(21)])
    assert trending_gate(trend) is True
    assert efficiency_ratio([c.close_price for c in trend]) == pytest.approx(1.0)
    sample = (LabeledExample(
        venue="hyperliquid", contract="BTC", quote="USDC", horizon_minutes=5,
        issued_at=trend[-1].observed_at,
        label_known_at=trend[-1].observed_at + timedelta(minutes=6),
        features=(.1,) * 10, label_up=1,
    ),)
    assert trending_samples(trend, sample) == sample

    choppy = _candles([100 + (i % 2) for i in range(21)])
    assert trending_gate(choppy) is False
    assert efficiency_ratio([c.close_price for c in choppy]) == pytest.approx(0.0)
    choppy_sample = (LabeledExample(
        venue="hyperliquid", contract="BTC", quote="USDC", horizon_minutes=5,
        issued_at=choppy[-1].observed_at,
        label_known_at=choppy[-1].observed_at + timedelta(minutes=6),
        features=(.1,) * 10, label_up=1,
    ),)
    assert trending_samples(choppy, choppy_sample) == ()

    short = _candles([100 + i for i in range(10)])
    short_sample = (LabeledExample(
        venue="hyperliquid", contract="BTC", quote="USDC", horizon_minutes=5,
        issued_at=short[-1].observed_at,
        label_known_at=short[-1].observed_at + timedelta(minutes=6),
        features=(.1,) * 10, label_up=1,
    ),)
    assert trending_samples(short, short_sample) == ()


def test_trending_filter_is_deterministic_across_replays():
    """Same candles, same samples, same admission -- restarts agree."""
    closes = [100 + i * 0.5 for i in range(30)] + [115 + (i % 2) for i in range(10)]
    candles = _candles(closes)
    samples = tuple(LabeledExample(
        venue="hyperliquid", contract="BTC", quote="USDC", horizon_minutes=5,
        issued_at=c.observed_at,
        label_known_at=c.observed_at + timedelta(minutes=6),
        features=(.1,) * 10, label_up=1,
    ) for c in candles)
    first = trending_samples(candles, samples)
    second = trending_samples(list(reversed(candles)), samples)
    assert first == second
    assert first  # the climb section admits samples
    assert len(first) < len(samples)  # the choppy tail is excluded


# --- D4: warm start, copied once ---------------------------------------------

def test_warm_start_copied_once_and_never_reapplied(tmp_path):
    """First fit copies the incumbent's weights; a restart never re-copies.

    The warm-start record (source SHA + timestamp + copied weights) is in
    the persisted state, so the drift metric can measure movement away
    from it. A second fit with a DIFFERENT warm start after a restart
    must not jump the weights to the new copy: the learner continues
    from its persisted weights.
    """
    hot = SQLiteHotStore(tmp_path / "warm.sqlite")
    try:
        now = datetime.now(UTC)
        rows = _samples(now)
        warm_weights = tuple(0.1 * (i + 1) for i in range(11))
        warm = _warm_start(warm_weights, now=now)
        learner = LiveModelCheckpoints(
            hot, feature_contract=BASE_FEATURE_CONTRACT,
            learning_rate=BASE_LEARNING_RATE)
        first = learner.fit(rows, as_of=now, warm_start=warm)
        assert first.updates == 80
        assert first.final_weights != warm_weights  # it learned from the copy
        with hot.connect() as conn:
            state = json.loads(conn.execute(
                "SELECT state_json FROM rival_live_model_checkpoints "
                "WHERE feature_contract=?", (BASE_FEATURE_CONTRACT,),
            ).fetchone()["state_json"])
        assert state["warm_start"]["source_weights_sha256"] == warm["source_weights_sha256"]
        assert state["warm_start"]["copied_at"] == warm["copied_at"]
        assert state["warm_start_weights"] == list(warm_weights)

        # Restart with a DIFFERENT warm start and NEW labels: the weights
        # continue from the persisted state, not from the new copy.
        restarted = LiveModelCheckpoints(
            hot, feature_contract=BASE_FEATURE_CONTRACT,
            learning_rate=BASE_LEARNING_RATE)
        other_weights = tuple(5.0 for _ in range(11))
        other = _warm_start(other_weights, now=now + timedelta(seconds=1))
        more = _samples(now + timedelta(minutes=500), count=10,
                        start=now + timedelta(minutes=5))
        second = restarted.fit((*rows, *more), as_of=now + timedelta(minutes=600),
                               warm_start=other)
        assert second.updates == 90
        assert second.final_weights != other_weights
        # The drift from the ORIGINAL copy is small (10 more labels on top
        # of 80); the drift from the never-applied copy would be huge.
        drift_original = math.sqrt(sum(
            (w - w0) ** 2 for w, w0 in zip(second.final_weights, warm_weights)))
        drift_other = math.sqrt(sum(
            (w - w0) ** 2 for w, w0 in zip(second.final_weights, other_weights)))
        assert drift_original < drift_other
        with hot.connect() as conn:
            state = json.loads(conn.execute(
                "SELECT state_json FROM rival_live_model_checkpoints "
                "WHERE feature_contract=?", (BASE_FEATURE_CONTRACT,),
            ).fetchone()["state_json"])
        assert state["warm_start"]["source_weights_sha256"] == warm["source_weights_sha256"]
        assert state["warm_start_weights"] == list(warm_weights)
    finally:
        hot.close()


def test_warm_start_rejected_when_malformed(tmp_path):
    """A bad warm start fails loudly instead of silently starting from zeros."""
    hot = SQLiteHotStore(tmp_path / "warm-bad.sqlite")
    try:
        now = datetime.now(UTC)
        rows = _samples(now)
        learner = LiveModelCheckpoints(
            hot, feature_contract=BASE_FEATURE_CONTRACT,
            learning_rate=BASE_LEARNING_RATE)
        with pytest.raises(ValueError):
            learner.fit(rows, as_of=now, warm_start={"weights": (0.1,) * 10,
                                                     "source_weights_sha256": "a" * 64,
                                                     "copied_at": now.isoformat()})
        with pytest.raises(ValueError):
            learner.fit(rows, as_of=now, warm_start={"weights": (0.1,) * 11,
                                                     "source_weights_sha256": "xyz",
                                                     "copied_at": now.isoformat()})
    finally:
        hot.close()


# --- Separation: two families, independent weights and rates -----------------

def test_families_keep_separate_weights_and_rates(tmp_path):
    """Same warm start, same samples: the fast family moves further, and
    a later update to one family never touches the other."""
    hot = SQLiteHotStore(tmp_path / "separate.sqlite")
    try:
        now = datetime.now(UTC)
        rows = _samples(now)
        warm_weights = tuple(0.05 * (i + 1) for i in range(11))
        warm = _warm_start(warm_weights, now=now)
        base = LiveModelCheckpoints(
            hot, feature_contract=BASE_FEATURE_CONTRACT,
            learning_rate=BASE_LEARNING_RATE)
        fast = LiveModelCheckpoints(
            hot, feature_contract=FAST_FEATURE_CONTRACT,
            learning_rate=FAST_LEARNING_RATE)
        base_result = base.fit(rows, as_of=now, warm_start=warm)
        fast_result = fast.fit(rows, as_of=now, warm_start=warm)
        assert base_result.updates == fast_result.updates == 80
        assert base_result.final_weights != fast_result.final_weights
        base_drift = math.sqrt(sum(
            (w - w0) ** 2 for w, w0 in zip(base_result.final_weights, warm_weights)))
        fast_drift = math.sqrt(sum(
            (w - w0) ** 2 for w, w0 in zip(fast_result.final_weights, warm_weights)))
        assert fast_drift > base_drift  # 2x step moves further on the same labels
        assert fast_drift < 3 * base_drift  # same order, not a runaway
        with hot.connect() as conn:
            contracts = [r["feature_contract"] for r in conn.execute(
                "SELECT feature_contract FROM rival_live_model_checkpoints").fetchall()]
        assert sorted(contracts) == sorted([BASE_FEATURE_CONTRACT, FAST_FEATURE_CONTRACT])
        # One more fit for the base family only: the fast weights are untouched.
        more = _samples(now + timedelta(minutes=500), count=5,
                        start=now + timedelta(minutes=5))
        base.fit((*rows, *more), as_of=now + timedelta(minutes=600),
                 warm_start=warm)
        assert fast.fit(rows, as_of=now + timedelta(minutes=600),
                        warm_start=warm).final_weights == fast_result.final_weights
    finally:
        hot.close()


# --- regime_live_calls() validation -------------------------------------------

def test_regime_live_calls_validation():
    """Gate pass + interior probability + 64-hex SHA issues; anything else []."""
    candles = _candles([100 + i for i in range(21)])
    good = regime_live_calls(
        family=BASE_FAMILY, candles=candles, probability=0.6,
        training_rows=100, model_sha256="a" * 64)
    assert good == [{"family": BASE_FAMILY, "probability": 0.6,
                     "training_rows": 100, "model_sha256": "a" * 64}]
    choppy = _candles([100 + (i % 2) for i in range(21)])
    assert regime_live_calls(
        family=BASE_FAMILY, candles=choppy, probability=0.6,
        training_rows=100, model_sha256="a" * 64) == []
    assert regime_live_calls(
        family=BASE_FAMILY, candles=candles, probability=0.0,
        training_rows=100, model_sha256="a" * 64) == []
    assert regime_live_calls(
        family=BASE_FAMILY, candles=candles, probability=1.0,
        training_rows=100, model_sha256="a" * 64) == []
    assert regime_live_calls(
        family=BASE_FAMILY, candles=candles, probability=0.6,
        training_rows=100, model_sha256="xyz") == []
    assert regime_live_calls(
        family="regime_v1", candles=candles, probability=0.6,
        training_rows=100, model_sha256="a" * 64) == []


# --- Prefix + issue() ----------------------------------------------------------

def test_live_prefix_accepted_everywhere_it_must_be():
    """Both families ride the existing regime_ prefix (CHALLENGER_PREFIXES,
    issue() allowlist, OTG exclusion) -- no new prefix to register."""
    for family in FAMILIES:
        assert any(family.startswith(prefix) for prefix in CHALLENGER_PREFIXES)


def test_issue_accepts_live_challenger_rows(tmp_path):
    """issue() stores a live-family row with its own probability (not the
    incumbent's -- unlike v1, the live learner decides its own call)."""
    store = _store(tmp_path, name="live-issue.sqlite")
    try:
        ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
        with store.connect() as conn:
            comp = ShadowModelCompetition(store)
            for family, p in ((BASE_FAMILY, 0.7), (FAST_FAMILY, 0.4)):
                saved = comp.issue(
                    conn, samples=[], features=(0.25,) * 10, now=NOW,
                    venue="hyperliquid", contract="BTC", quote="USDC",
                    asset="BTC", horizon=5,
                    reference_opened_at=NOW - timedelta(minutes=30),
                    target_opened_at=NOW - timedelta(minutes=25),
                    issued_at=NOW - timedelta(minutes=25),
                    incumbent_up=0.6, baseline_up=0.45,
                    challengers=[{"family": family, "probability": p,
                                  "training_rows": 100, "model_sha256": "a" * 64}],
                )
                assert saved == 1
                row = conn.execute(
                    """SELECT family,probability_up,incumbent_up,training_count,
                              training_sha256
                       FROM rival_model_shadow_forecasts
                       WHERE family=?""", (family,),
                ).fetchone()
                assert row["probability_up"] == p
                assert row["incumbent_up"] == 0.6
                assert row["training_count"] == 100
                assert row["training_sha256"] == "a" * 64
    finally:
        store.close()


# --- Cycle: kill switches, abstention, learner safety --------------------------

def test_kill_switches_independent_in_cycle(tmp_path, monkeypatch):
    """Flag on: the cycle issues live rows for both families. Each flag off
    stops only its own family, while the other family and the incumbent
    keep running."""
    assert forward_audit.ISSUE_REGIME_LIVE_V1_CHALLENGERS is True
    assert forward_audit.ISSUE_REGIME_LIVE_FAST_V1_CHALLENGERS is True
    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "live-cycle.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        _seed_shape_candles(market, now=now, shape="trend")
        monkeypatch.setattr(forward_audit, "ISSUE_REGIME_LIVE_V1_CHALLENGERS", True)
        monkeypatch.setattr(forward_audit, "ISSUE_REGIME_LIVE_FAST_V1_CHALLENGERS", True)
        monitor.run_once(now=now)
        base_on, incumbent_on = _cycle_counts(store, BASE_FAMILY)
        fast_on, _ = _cycle_counts(store, FAST_FAMILY)
        assert base_on >= 1  # the control proves this cycle can issue live rows
        assert fast_on >= 1
        assert incumbent_on >= 1
        monkeypatch.setattr(forward_audit, "ISSUE_REGIME_LIVE_V1_CHALLENGERS", False)
        monitor.run_once(now=now)
        assert _cycle_counts(store, BASE_FAMILY) == (base_on, incumbent_on)
        fast_after, _ = _cycle_counts(store, FAST_FAMILY)
        assert fast_after >= fast_on  # the fast family is unaffected
        monkeypatch.setattr(forward_audit, "ISSUE_REGIME_LIVE_FAST_V1_CHALLENGERS", False)
        monitor.run_once(now=now)
        assert _cycle_counts(store, FAST_FAMILY) == (fast_after, incumbent_on)
    finally:
        store.close()


def test_choppy_cycle_abstains_but_incumbent_runs(tmp_path, monkeypatch, caplog):
    """Abstention writes no live row; the incumbent still forecasts."""
    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "live-choppy.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        _seed_shape_candles(market, now=now, shape="choppy")
        monkeypatch.setattr(forward_audit, "ISSUE_REGIME_LIVE_V1_CHALLENGERS", True)
        monkeypatch.setattr(forward_audit, "ISSUE_REGIME_LIVE_FAST_V1_CHALLENGERS", True)
        with caplog.at_level(logging.INFO):
            monitor.run_once(now=now)
        base, incumbent = _cycle_counts(store, BASE_FAMILY)
        fast, _ = _cycle_counts(store, FAST_FAMILY)
        assert base == 0
        assert fast == 0
        assert incumbent >= 1
        for prefix in ("regime_live", "regime_live_fast"):
            match = re.search(rf"{prefix}_issued=(\d+) {prefix}_abstained=(\d+)",
                              caplog.text)
            assert match is not None, prefix
            assert int(match.group(1)) == 0
            assert int(match.group(2)) >= 1
    finally:
        store.close()


def test_learner_fit_exception_never_blocks_incumbent(tmp_path, monkeypatch):
    """A filter that explodes fits nothing; the incumbent still forecasts."""
    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "live-boom.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        _seed_shape_candles(market, now=now, shape="trend")

        def _boom(*args, **kwargs):
            raise RuntimeError("filter exploded")

        monkeypatch.setattr(regime_live_model, "trending_samples", _boom)
        monkeypatch.setattr(forward_audit, "ISSUE_REGIME_LIVE_V1_CHALLENGERS", True)
        monkeypatch.setattr(forward_audit, "ISSUE_REGIME_LIVE_FAST_V1_CHALLENGERS", True)
        monitor.run_once(now=now)
        base, incumbent = _cycle_counts(store, BASE_FAMILY)
        fast, _ = _cycle_counts(store, FAST_FAMILY)
        assert base == 0
        assert fast == 0
        assert incumbent >= 1
    finally:
        store.close()


def test_live_call_exception_never_blocks_incumbent(tmp_path, monkeypatch):
    """A call that explodes is an abstention, never a failed incumbent cycle."""
    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "live-call-boom.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        _seed_shape_candles(market, now=now, shape="trend")

        def _boom(*args, **kwargs):
            raise RuntimeError("call exploded")

        monkeypatch.setattr(regime_live_model, "regime_live_calls", _boom)
        monkeypatch.setattr(forward_audit, "ISSUE_REGIME_LIVE_V1_CHALLENGERS", True)
        monkeypatch.setattr(forward_audit, "ISSUE_REGIME_LIVE_FAST_V1_CHALLENGERS", True)
        monitor.run_once(now=now)
        base, incumbent = _cycle_counts(store, BASE_FAMILY)
        fast, _ = _cycle_counts(store, FAST_FAMILY)
        assert base == 0
        assert fast == 0
        assert incumbent >= 1
    finally:
        store.close()


# --- D10: offline regime_v2 retired --------------------------------------------

def test_offline_regime_v2_retired():
    """Kill switch off and no longer in the weekly training loop."""
    assert forward_audit.ISSUE_REGIME_V2_CHALLENGERS is False
    assert forward_audit.ISSUE_REGIME_LIVE_V1_CHALLENGERS is True
    assert forward_audit.ISSUE_REGIME_LIVE_FAST_V1_CHALLENGERS is True
    from parlay_bot.research import weekly_archive

    assert not hasattr(weekly_archive, "regime2_training")


# --- selected(): continuity, not a pin -----------------------------------------

def test_selected_continuity_branch(tmp_path):
    """No offline artifact, so no D14 pin: continuity of the live weights.

    The served weights must be the continuation of the promoted ones --
    same feature digest, trained-through and graded count monotone. A
    weight change is accepted only with more graded learning behind it.
    """
    store = _store(tmp_path, name="live-selected.sqlite")
    try:
        promoter = FamilyPromotion(store)
        kwargs = {"asset": "BTC", "venue": "hyperliquid", "contract": "BTC",
                  "quote": "USDC", "horizon": 5, "now": NOW}
        identity = {
            "feature_digest": feature_digest(BASE_FAMILY),
            "weights_sha256": "a" * 64,
            "trained_through": (NOW - timedelta(hours=1)).isoformat(),
            "graded_count": 0,
        }
        # No rows at all: no identity, no serve.
        _promotion_row(store, family=BASE_FAMILY, evaluated_at=NOW,
                       artifact=identity)
        assert promoter.selected(**kwargs) is None
        # A fitted learner gives the checkpoint row artifact_identity reads.
        learner = LiveModelCheckpoints(
            store, feature_contract=BASE_FEATURE_CONTRACT,
            learning_rate=BASE_LEARNING_RATE)
        result = learner.fit(
            _samples(NOW), as_of=NOW,
            warm_start=_warm_start(tuple(0.05 * (i + 1) for i in range(11)), now=NOW))
        weights_sha = hashlib.sha256(json.dumps(
            list(result.final_weights), separators=(",", ":"),
            allow_nan=False).encode()).hexdigest()
        identity = {**identity, "weights_sha256": weights_sha,
                    "trained_through": NOW.isoformat()}
        # One graded row under the promoted weights: serves.
        with store.connect() as conn:
            _insert_shadow_row(conn, p=0.7, y=1, sha=weights_sha, family=BASE_FAMILY,
                               issued=NOW - timedelta(hours=1),
                               target=NOW - timedelta(hours=1))
        assert artifact_identity(store, "BTC", 5, family=BASE_FAMILY)["graded_count"] == 1
        _promotion_row(store, family=BASE_FAMILY, evaluated_at=NOW,
                       artifact={**identity, "graded_count": 1})
        assert promoter.selected(**kwargs) == BASE_FAMILY
        # A mismatched weights SHA with no further graded learning: not
        # the weights that earned promotion.
        tampered = {**identity, "graded_count": 1, "weights_sha256": "0" * 64}
        _promotion_row(store, family=BASE_FAMILY, evaluated_at=NOW,
                       artifact=tampered)
        assert promoter.selected(**kwargs) is None
        # A mismatched feature digest: a different model, not a continuation.
        tampered = {**identity, "graded_count": 1,
                    "feature_digest": "0" * 64}
        _promotion_row(store, family=BASE_FAMILY, evaluated_at=NOW,
                       artifact=tampered)
        assert promoter.selected(**kwargs) is None
        # Stale promotion (older than 48h) fails safe to the incumbent.
        _promotion_row(store, family=BASE_FAMILY,
                       evaluated_at=NOW - timedelta(hours=49),
                       artifact={**identity, "graded_count": 1})
        assert promoter.selected(**kwargs) is None
        # A weight change WITH more graded learning behind it is a
        # continuation (the live learner moved), not a refit: still serves.
        # The learner refits on new labels (weights move to a new SHA) and
        # earns TWO graded rows under the new weights -- more than the one
        # row the promotion was recorded with.
        _promotion_row(store, family=BASE_FAMILY, evaluated_at=NOW,
                       artifact={**identity, "graded_count": 1})
        more = _samples(NOW + timedelta(hours=2), count=10, start=NOW)
        refitted = learner.fit(more, as_of=NOW + timedelta(hours=2),
                               warm_start=_warm_start(
                                   tuple(0.05 * (i + 1) for i in range(11)),
                                   now=NOW + timedelta(hours=2)))
        new_sha = hashlib.sha256(json.dumps(
            list(refitted.final_weights), separators=(",", ":"),
            allow_nan=False).encode()).hexdigest()
        assert new_sha != weights_sha  # the weights really moved
        with store.connect() as conn:
            for i in range(2):
                _insert_live_row(conn, p=0.7, y=1, sha=new_sha, family=BASE_FAMILY,
                                 target=NOW - timedelta(minutes=30 * (i + 1)),
                                 issued=NOW - timedelta(minutes=30 * (i + 1) + 5),
                                 ref=NOW - timedelta(minutes=30 * (i + 1) + 10))
        current = artifact_identity(store, "BTC", 5, family=BASE_FAMILY)
        assert current["weights_sha256"] == new_sha
        assert current["graded_count"] == 2
        assert promoter.selected(**kwargs) == BASE_FAMILY
    finally:
        store.close()


# --- evaluate(): artifact + Route B qualification on the subset ----------------

def test_evaluate_records_live_artifact_no_pin_filter(tmp_path):
    """Two graded rows under DIFFERENT weights SHAs both count toward the
    family's evidence: there is no D14 pin filter for a live learner,
    whose weights move by design. The artifact's graded count is the
    continuity count -- rows under the CURRENT weights only."""
    store = _store(tmp_path, name="live-eval.sqlite")
    try:
        learner = LiveModelCheckpoints(
            store, feature_contract=BASE_FEATURE_CONTRACT,
            learning_rate=BASE_LEARNING_RATE)
        fitted = learner.fit(
            _samples(NOW), as_of=NOW,
            warm_start=_warm_start(tuple(0.05 * (i + 1) for i in range(11)), now=NOW))
        weights_sha = hashlib.sha256(json.dumps(
            list(fitted.final_weights), separators=(",", ":"),
            allow_nan=False).encode()).hexdigest()
        with store.connect() as conn:
            # Grid-aligned targets (evaluate() drops off-grid targets):
            # 11:30 and 11:00 on the same day, both settled 10 min later.
            _insert_live_row(conn, p=0.7, y=1, sha=weights_sha, family=BASE_FAMILY,
                             target=datetime(2026, 11, 7, 11, 30, tzinfo=UTC),
                             issued=datetime(2026, 11, 7, 11, 25, tzinfo=UTC),
                             ref=datetime(2026, 11, 7, 11, 25, tzinfo=UTC))
            _insert_live_row(conn, p=0.4, y=0, sha="b" * 64, family=BASE_FAMILY,
                             target=datetime(2026, 11, 7, 11, 0, tzinfo=UTC),
                             issued=datetime(2026, 11, 7, 10, 55, tzinfo=UTC),
                             ref=datetime(2026, 11, 7, 10, 55, tzinfo=UTC))
        result = FamilyPromotion(store).evaluate(
            asset="BTC", venue="hyperliquid", contract="BTC", quote="USDC",
            horizon=5, now=NOW)
        evidence = result["families"][BASE_FAMILY]
        assert evidence["targets"] == 2  # both SHAs count, no pin filter
        artifact = evidence["artifact"]
        assert artifact is not None
        assert artifact["graded_count"] == 1  # only the current-weights row
        assert artifact["feature_digest"] == feature_digest(BASE_FAMILY)
        assert artifact["weights_sha256"] == weights_sha
        # Two graded rows is far below the Route B minimums: held, never
        # promoted on a backtest-sized sample.
        assert evidence["qualified"] is False  # 2 < 150
        assert result["status"] == "held"
        assert result["selected_family"] is None
    finally:
        store.close()


def test_promotion_qualifies_on_live_subset(learning_store):
    """Route B on the issued subset: 168 winning targets, full quote
    coverage, after-cost proofs pass -> promoted_paper, selected()
    serves it, and the receipt preserves the weights SHA."""
    now = datetime(2026, 10, 31, 16, tzinfo=UTC)
    # artifact_identity() reads the live checkpoint row, so the learner
    # must have fitted first; the seeded ledger rows then carry the
    # fitted weights SHA (shadow and quote rows must agree, as in prod).
    learner = LiveModelCheckpoints(
        learning_store, feature_contract=BASE_FEATURE_CONTRACT,
        learning_rate=BASE_LEARNING_RATE)
    fitted = learner.fit(
        _samples(now), as_of=now,
        warm_start=_warm_start(tuple(0.05 * (i + 1) for i in range(11)), now=now))
    weights_sha = hashlib.sha256(json.dumps(
        list(fitted.final_weights), separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()
    family = _seed_promotion(learning_store, now, family=BASE_FAMILY)
    with learning_store.connect() as conn:
        conn.execute(
            "UPDATE rival_model_shadow_forecasts SET training_sha256=? "
            "WHERE family=?", (weights_sha, family))
        conn.execute(
            "UPDATE rival_family_quote_shadow SET training_sha256=? "
            "WHERE family=?", (weights_sha, family))
    owner = FamilyPromotion(learning_store)
    kwargs = {"asset": "BTC", "venue": "hyperliquid", "contract": "BTC",
              "quote": "USDC", "horizon": 5}
    result = owner.evaluate(now=now, **kwargs)
    assert result["selected_family"] == family
    assert result["status"] == "promoted_paper"
    evidence = result["families"][family]
    assert evidence["targets"] == 168
    assert evidence["days"] == 21
    assert evidence["closed_quotes"] == 168
    assert evidence["quote_coverage"] == 1.0
    assert evidence["calibration_status"] == "research_screen_passed"
    assert evidence["cost_proof"]["severe"]["qualified"]
    assert evidence["artifact"]["graded_count"] == 168
    assert owner.selected(now=now, **kwargs) == family
    assert owner.selected(now=now + timedelta(hours=49), **kwargs) is None
    with learning_store.connect() as conn:
        receipt = conn.execute(
            """SELECT evidence_json FROM rival_family_promotion_receipts
               WHERE family=? AND kind='promoted'""", (family,),
        ).fetchone()
    assert receipt is not None
    assert json.loads(receipt["evidence_json"])["evidence"]["artifact"]["weights_sha256"] == weights_sha


# --- D7: trial report ------------------------------------------------------------

def _live_report_fixture(tmp_path, name):
    """5 live rows + 5 v1 rows on the same targets + 15 incumbent rows."""
    store = _store(tmp_path, name=name)
    monitor = ForwardAccuracyMonitor(PerpetualMarketStore(store))
    monitor.initialize()
    with store.connect() as conn:
        for i in range(5):
            target = NOW - timedelta(days=1, minutes=5 * i)
            _insert_regime_row(conn, target=target,
                               issued=target - timedelta(minutes=5),
                               p=0.7, incumbent_p=0.6, y=1)
            _insert_shadow_row(conn, p=0.95, y=1, sha="a" * 64, family=BASE_FAMILY,
                               ref=target - timedelta(minutes=5),
                               issued=target - timedelta(minutes=5),
                               target=target)
            _insert_incumbent_row(conn, target=target,
                                  issued=target - timedelta(minutes=5),
                                  p=0.6, y=1)
        for i in range(10):
            target = NOW - timedelta(days=1, minutes=5 * (i + 5))
            _insert_incumbent_row(conn, target=target,
                                  issued=target - timedelta(minutes=5),
                                  p=0.5, y=1)
    return store, trial_report(store, asset="BTC", horizon=5,
                               family=BASE_FAMILY, now=NOW)


def test_trial_report_live_fields(tmp_path):
    """Paired gain is the headline (the learner can beat the incumbent),
    with the vs-v1 head-to-head, selection, both coverage ratios, and
    the gain split: all rows are in the last 7 days, so days 8-30 is None."""
    store, report = _live_report_fixture(tmp_path, "live-report.sqlite")
    try:
        assert report["issued"] == 5
        assert report["graded"] == 5
        assert report["decided"] == 5
        # Live 0.0025 vs incumbent 0.16 on the subset: gain +0.1575.
        assert report["paired_gain"] == pytest.approx(0.1575)
        # v1 0.09 vs live 0.0025 on the same targets: live beats v1 too.
        assert report["paired_gain_vs_regime_v1"] == pytest.approx(0.0875)
        # Incumbent 0.16 on the subset vs 0.22 overall: easier subset.
        assert report["selection_effect"] == pytest.approx(-0.06)
        assert report["coverage"] == pytest.approx(5 / 15)
        assert report["availability_ratio"] == pytest.approx(5 / 5)
        assert report["updates"] == 0  # no checkpoint yet
        assert report["weight_drift_mean"] == 0.0
        assert report["weight_drift_max"] == 0.0
        assert report["paired_gain_last_7d"] == pytest.approx(0.1575)
        assert report["paired_gain_days_8_30"] is None
    finally:
        store.close()


def test_trial_report_drift_after_learning(tmp_path):
    """Once the learner has fitted, the report carries its update count
    and its L2 drift from the warm-start copy."""
    store, _ = _live_report_fixture(tmp_path, "live-report-drift.sqlite")
    try:
        learner = LiveModelCheckpoints(
            store, feature_contract=BASE_FEATURE_CONTRACT,
            learning_rate=BASE_LEARNING_RATE)
        warm_weights = tuple(0.05 * (i + 1) for i in range(11))
        result = learner.fit(
            _samples(NOW), as_of=NOW,
            warm_start=_warm_start(warm_weights, now=NOW))
        report = trial_report(store, asset="BTC", horizon=5,
                              family=BASE_FAMILY, now=NOW)
        assert report["updates"] == 80
        expected_drift = math.sqrt(sum(
            (w - w0) ** 2 for w, w0 in zip(result.final_weights, warm_weights)))
        assert report["weight_drift_mean"] == pytest.approx(expected_drift)
        assert report["weight_drift_max"] == pytest.approx(expected_drift)
    finally:
        store.close()


def test_trial_report_empty(tmp_path):
    """No rows: the report is {"issued": 0}, never a crash."""
    store = _store(tmp_path, name="live-report-empty.sqlite")
    try:
        ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
        assert trial_report(store, asset="BTC", horizon=5,
                            family=BASE_FAMILY, now=NOW) == {"issued": 0}
        assert trial_report(store, asset="BTC", horizon=5,
                            family=FAST_FAMILY, now=NOW) == {"issued": 0}
    finally:
        store.close()


# --- regime_summary() family slice -------------------------------------------

def test_regime_summary_live_family_slice(tmp_path):
    """The dashboard slice over a live family's issued subset only."""
    store = _store(tmp_path, name="live-summary.sqlite")
    try:
        monitor = ForwardAccuracyMonitor(PerpetualMarketStore(store))
        monitor.initialize()
        with store.connect() as conn:
            for i in range(3):
                target = NOW - timedelta(days=1, minutes=5 * i)
                ref = target - timedelta(minutes=5)
                _insert_live_row(conn, target=target,
                                 issued=target, p=0.7, y=1,
                                 ref=ref, family=BASE_FAMILY)
                conn.execute(
                    """INSERT INTO perp_forward_regimes
                       (venue,contract,quote,horizon_minutes,model_version,
                        reference_opened_at,issued_at,method_version,regime,
                        five_min_volatility_pct,feature_sha256,weights_sha256)
                       VALUES ('hyperliquid','BTC','USDC',5,?,?,?,?,
                               'normal',0.2,?,?)""",
                    (FORWARD_MODEL, ref.isoformat(), target.isoformat(),
                     "regime-v1", "a" * 64, "a" * 64),
                )
        summary = monitor.regime_summary(
            asset="BTC", horizon_minutes=5, family=BASE_FAMILY, now=NOW)
        part = summary["regimes"]["normal"]
        assert part["unique_targets"] == 3
        # Live 0.09 vs incumbent 0.16 on the same targets: gain +0.07.
        assert part["paired_brier_gain"] == pytest.approx(0.07)
    finally:
        store.close()


# --- D11: never an OTG live lean ----------------------------------------------

def test_live_lean_none_for_live_families():
    """Neither live family has a read-only recompute path: its live lean
    is None, so it can never become a WATCH/alert/DM/Auto-Hunt candidate
    or appear in the OTG display as a live call."""
    for family in FAMILIES:
        assert live_lean(None, asset="BTC", horizon=5, venue="hyperliquid",
                         model_version=family) is None
        assert live_lean(None, asset="BTC", horizon=5, venue="hyperliquid",
                         model_version=family, contract="BTC",
                         quote="USDC") is None
