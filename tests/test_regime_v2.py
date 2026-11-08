"""regime_v2 challenger (spec rival-regime-v2-spec-20261108).

Covers: the trending-subset trainer (ER mask from the candles DF,
mandatory census in every report + standalone entry point, D1 floors
that report-only instead of training), the frozen ER > 0.30 gate
shared with v1 (train/serve ER equivalence), the subset trainer
learning the trending signal where the all-rows trainer learns the
wrong sign, held-out evaluation on the trending subset only (paired
gain vs the incumbent is the headline), the "regime_" prefix already
covering regime_v2 (no prefix change), the kill switch in the 5-minute
cycle (flag on issues, flag off issues nothing new, gate-fail and
unavailable counted separately), the selected() D14 exact-pin branch
(a queued fit waits behind the pinned fit; rejection advances the
queue), evaluate() recording the artifact and qualifying on the issued
subset, the D5 decomposition (paired gain + v2-vs-v1 gain + selection
effect + coverage + availability) in trial_report(), the weekly shared
run training regime_v2 as a third family, and D8 (no OTG live lean).
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from parlay_bot.discord_app.out_the_gate import live_lean
from parlay_bot.perpetuals import forward_audit
from parlay_bot.perpetuals.family_governance import FamilyPromotion
from parlay_bot.perpetuals.forward_audit import MODEL as FORWARD_MODEL
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import (
    CHALLENGER_PREFIXES,
    ShadowModelCompetition,
)
from parlay_bot.perpetuals.regime2_model import (
    FAMILY,
    Regime2ModelSet,
    artifact_identity,
    calibration_status,
    is_regime2_document,
    model_sha256,
    regime2_calls,
    trial_report,
)
from parlay_bot.perpetuals.regime_gate import (
    ER_THRESHOLD,
    efficiency_ratio,
    trending_gate,
)
from parlay_bot.research import regime2_training
from parlay_bot.research.archive_features import FEATURE_CONTRACT, FEATURES, frame_features
from parlay_bot.research.archive_training import (
    FEATURE_DIGEST as ARCHIVE_FEATURE_DIGEST,
)
from parlay_bot.research.archive_training import TrainingDataUnavailable
from parlay_bot.research.archive_training import train_run as archive_train_run
from parlay_bot.research.regime2_training import (
    FEATURE_DIGEST,
    SUBSET_DESCRIPTOR,
    TRAINING_SUBSET,
)
from parlay_bot.storage.hot import SQLiteHotStore
from tests.test_archive_model import (
    PROV,
    _frames,
    _splits,
)
from tests.test_archive_v1_shadow import (
    _insert_shadow_row,
    _promotion_row,
    _store,
)
from tests.test_regime_v1 import (
    _candles,
    _insert_incumbent_row,
    _insert_regime_row,
    _seed_shape_candles,
)

NOW = datetime(2026, 11, 7, 12, 0, 30, tzinfo=UTC)


# --- Fixtures ----------------------------------------------------------------

def _regime_frames(*, n_bars=17280, trend_block=640, chop_block=320, mom=0.8):
    """Alternating-momentum archive frames: trending blocks carry a
    learnable +momentum signal, chop blocks carry the OPPOSITE signal.

    The point is discriminating: a trainer that sees only trending rows
    must learn the trending direction, while an all-rows trainer sees
    the two signals cancel (and, on this fixture, learns the wrong
    sign). Trending blocks are twice as long as chop blocks so the
    all-rows trainer still publishes (its other features carry signal),
    keeping the weight comparison apples-to-apples.
    """
    out = {}
    for venue, seed in (("binance", 5), ("bitget", 6)):
        rng = np.random.default_rng(seed)
        times = pd.date_range("2025-01-01", periods=n_bars, freq="5min", tz=UTC)
        close_times = times + pd.Timedelta(minutes=5)
        r = np.zeros(n_bars)
        cycle = trend_block + chop_block
        for t in range(1, n_bars):
            in_trend = (t % cycle) < trend_block
            m = mom if in_trend else -mom
            r[t] = m * r[t - 1] + rng.normal(0, 0.0015)
        imb = np.zeros(n_bars)
        for t in range(1, n_bars):
            imb[t - 1] = 0.5 + 0.3 * math.tanh(50 * r[t - 1])
        imb[-1] = 0.5 + 0.3 * math.tanh(50 * r[-1])
        price = 100 * np.exp(np.cumsum(r))
        volume = 1000 * (1 + np.abs(r) * 5000)
        k = pd.DataFrame({
            "open": np.concatenate([[price[0]], price[:-1]]),
            "high": price * 1.0002, "low": price * 0.9998, "close": price,
            "volume": volume, "trades": 10,
            "taker_buy_volume": volume * imb, "count": 100,
            "prem": 0.0002 * np.tanh(
                pd.Series(r).rolling(12).mean().fillna(0).to_numpy() * 100),
            "fund": 0.0001,
            "first_observed_at": close_times + pd.Timedelta(milliseconds=1),
        }, index=close_times)
        out[f"{venue}|BTC"] = (k, frame_features(k, None, "BTC"))
    return out


def _chop_frames(*, n_bars=2000):
    """Archive frames whose closes alternate 100/101: ER is 0.0 on every
    bar, so the trending subset is EMPTY while labelled rows are
    plentiful. The D1 floors must refuse these frames (report-only),
    not train on an empty subset."""
    out = {}
    for venue, seed in (("binance", 5), ("bitget", 6)):
        rng = np.random.default_rng(seed)
        times = pd.date_range("2025-01-01", periods=n_bars, freq="5min", tz=UTC)
        close_times = times + pd.Timedelta(minutes=5)
        close = np.array([100.0 + (i % 2) for i in range(n_bars)])
        volume = 1000 * (1 + rng.random(n_bars))
        k = pd.DataFrame({
            "open": np.concatenate([[close[0]], close[:-1]]),
            "high": close * 1.0002, "low": close * 0.9998, "close": close,
            "volume": volume, "trades": 10,
            "taker_buy_volume": volume * 0.5, "count": 100,
            "prem": np.zeros(n_bars),
            "fund": 0.0001,
            "first_observed_at": close_times + pd.Timedelta(milliseconds=1),
        }, index=close_times)
        out[f"{venue}|BTC"] = (k, frame_features(k, None, "BTC"))
    return out


def _seed_regime2_promotion(store, now):
    """21 days x 8 targets of winning regime_v2 rows + matched quote
    rows, carrying the imported artifact's SHA -- the D14 pin filter
    only counts rows under the pinned fit. Same shape as the v1
    promotion fixture, but the artifact is imported first."""
    from parlay_bot.perpetuals.family_quotes import FamilyQuoteShadow

    model_set = Regime2ModelSet(store)
    # Trained through the day BEFORE the evaluation: usable() requires
    # trained_through < now, so a doc trained after the evaluation
    # time would pin nothing and its rows would not count.
    doc = _valid_regime2_doc(trained_through=now - timedelta(days=1))
    assert model_set.import_document("file-1", json.dumps(doc).encode()) == 1
    sha = model_sha256(doc)
    market = PerpetualMarketStore(store)
    ForwardAccuracyMonitor(market).initialize()
    FamilyQuoteShadow(store).initialize()
    with store.connect() as conn:
        for d in range(21):
            for i in range(8):
                target = now - timedelta(days=23 - d) + timedelta(minutes=5 * i)
                issued = target - timedelta(seconds=40)
                reference = target - timedelta(minutes=5)
                settled = target + timedelta(minutes=6)
                score = (0.95 - 1.0) ** 2
                conn.execute(
                    """INSERT INTO rival_model_shadow_forecasts (
                      venue,contract,quote,asset,horizon_minutes,family,
                      reference_opened_at,issued_at,target_opened_at,
                      probability_up,incumbent_up,baseline_up,
                      training_count,training_sha256,settled_at,outcome_up,
                      model_brier,incumbent_brier,baseline_brier)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("hyperliquid", "BTC", "USDC", "BTC", 5, "regime_v2",
                     reference.isoformat(), issued.isoformat(), target.isoformat(),
                     0.95, 0.5, 0.5, 300, sha, settled.isoformat(), 1,
                     score, 0.25, 0.25),
                )
                entry = issued + timedelta(seconds=10)
                exit_at = target + timedelta(minutes=5, seconds=20)
                conn.execute(
                    """INSERT INTO rival_family_quote_shadow (
                      venue,contract,quote,asset,horizon_minutes,family,
                      reference_opened_at,issued_at,target_close_at,
                      training_sha256,challenger_up,incumbent_up,
                      challenger_side,incumbent_side,entry_at,
                      entry_bid,entry_ask,quantity,state,
                      exit_at,exit_bid,exit_ask,
                      challenger_gross_bps,incumbent_gross_bps)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    ("hyperliquid", "BTC", "USDC", "BTC", 5, "regime_v2",
                     reference.isoformat(), issued.isoformat(),
                     (target + timedelta(minutes=5)).isoformat(),
                     sha, 0.95, 0.5, "LONG", "CASH", entry.isoformat(),
                     100.0, 100.1, 0.25, "EXIT_QUOTED",
                     exit_at.isoformat(), 104.0, 104.1, 350.0, 0.0),
                )
    return "regime_v2"


def _train(frames, *, asset="BTC", horizon=5):
    train_end, test_start, trained_through = _splits()
    return regime2_training.train_run(
        frames, asset=asset, horizon=horizon, train_end=train_end,
        test_start=test_start, trained_through=trained_through,
        provenance=dict(PROV), code_version="test",
    )


def _valid_regime2_doc(*, asset="BTC", horizon=5, trained_through=None, weights=None):
    """A regime_v2 artifact doc: same shape as the archive doc, but the
    trending-subset feature digest and the regime_v2 schema/family."""
    trained_through = trained_through or (NOW - timedelta(days=1))
    weights = (
        weights if weights is not None
        else [0.01 * ((i % 7) - 3) for i in range(33)]
    )
    return {
        "schema": "rival-regime2-model-v1",
        "family": "regime_v2",
        "model_version": "regime-offline-v1",
        "asset": asset,
        "horizon_minutes": horizon,
        "feature_contract": "archive-candle-premium-calendar-v1",
        "features": list(FEATURES),
        "feature_digest": FEATURE_DIGEST,
        "weights": list(weights),
        "standardizer": {"mean": [0.0] * 32, "std": [1.0] * 32},
        "calibration": {"method": "platt", "status": "evaluated", "a": 1.0,
                        "b": 0.0, "ece": 0.02, "fit_rows": 200, "eval_rows": 100,
                        "top_bucket_hit_rate": 0.6, "top_bucket_n": 20,
                        "top_bucket_mean_confidence": 0.6},
        "trained_from": "2025-01-01T00:00:00+00:00",
        "trained_through": trained_through.isoformat(),
        "training_rows": 1000,
        "training_subset": dict(TRAINING_SUBSET),
        "gates_passed": True,
        "evaluation_status": "held-out evaluated",
        "provenance": {
            "provider_sha256_by_month": {"binance:2025-01": "a" * 64},
            "normalized_sha256_by_month": {"binance:2025-01": "b" * 64},
            "months_without_provider_hash": [],
        },
        "created_at": trained_through.isoformat(),
    }


def _insert_regime2_row(conn, *, target, issued, p, incumbent_p, baseline_p=0.45,
                        y=1, ref=None, settled=True, sha=None):
    """One settled regime_v2 shadow row with explicit per-side probabilities."""
    ref = ref or (target - timedelta(minutes=5))
    settled_at = (target + timedelta(minutes=10)).isoformat() if settled else None
    conn.execute(
        """INSERT INTO rival_model_shadow_forecasts
           (venue,contract,quote,asset,horizon_minutes,family,
            reference_opened_at,issued_at,target_opened_at,
            probability_up,incumbent_up,baseline_up,
            training_count,training_sha256,settled_at,outcome_up,
            model_brier,incumbent_brier,baseline_brier)
           VALUES ('hyperliquid','BTC','USDC','BTC',5,'regime_v2',?,?,?,?,?,?,100,?,
                   ?,?,?,?,?)""",
        (ref.isoformat(), issued.isoformat(), target.isoformat(),
         p, incumbent_p, baseline_p, sha or "a" * 64, settled_at,
         y if settled else None,
         (p - y) ** 2 if settled else None,
         (incumbent_p - y) ** 2 if settled else None,
         (baseline_p - y) ** 2 if settled else None),
    )


def _cycle_counts(store):
    with store.connect() as conn:
        shadow = conn.execute(
            "SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts "
            "WHERE family='regime_v2'"
        ).fetchone()["n"]
        incumbent = conn.execute(
            "SELECT COUNT(*) AS n FROM perp_forward_audit "
            "WHERE model_version=?", (FORWARD_MODEL,),
        ).fetchone()["n"]
    return shadow, incumbent


# --- D1: the ER mask + census ------------------------------------------------

def _closes_series(values):
    """A closes Series on a 5-minute DatetimeIndex, as the trainer sees
    it (the gap check compares index diffs against 5 minutes)."""
    return pd.Series(
        values, dtype=float,
        index=pd.date_range("2025-01-01", periods=len(values), freq="5min", tz=UTC),
    )


def test_trending_mask_unit_values():
    """The mask is the gate's efficiency ratio applied per bar: a straight
    climb is trending, chop is not, flat closes are not."""
    assert regime2_training.trending_mask(
        _closes_series([100 + i for i in range(30)])).iloc[20:].all()
    assert not regime2_training.trending_mask(
        _closes_series([100 + (i % 2) for i in range(30)])).any()
    assert not regime2_training.trending_mask(
        _closes_series([100.0] * 30)).any()
    # The first 20 bars can never be trending: no 21-close window yet.
    assert not regime2_training.trending_mask(
        _closes_series([100 + i for i in range(30)])).iloc[:20].any()


def test_trending_mask_boundary_exact_0_30_abstains():
    """ER exactly 0.30 is not trending: the gate is strict (>), not >=.

    20 diffs: six +1.0 (direction 6, movement 6) plus seven +1/-1
    pairs (direction 0, movement 14): ER = 6/20 = 0.30. Replace one
    -1.0 with -0.9 and the ratio is 6.1/19.9 > 0.30.
    """
    closes = [100.0]
    for move in [1.0] * 6 + [1.0, -1.0] * 7:
        closes.append(closes[-1] + move)
    assert len(closes) == 21
    assert efficiency_ratio(closes) == 0.30
    mask = regime2_training.trending_mask(_closes_series(closes))
    assert not mask.iloc[-1]
    closes2 = [100.0]
    for move in [1.0] * 6 + [1.0, -0.9] + [1.0, -1.0] * 6:
        closes2.append(closes2[-1] + move)
    assert efficiency_ratio(closes2) == pytest.approx(6.1 / 19.9)
    mask2 = regime2_training.trending_mask(_closes_series(closes2))
    assert mask2.iloc[-1]


def test_census_numbers_on_standard_fixture():
    """The census measures before training: per-venue trending bars and
    per-split trending labelled rows, on the standard synthetic fixture
    (binance 10,488/17,280 = 60.7%, bitget 10,606/17,280 = 61.4%)."""
    result = _train(_frames())
    assert result["publish_kind"] == "model"
    assert result["report"]["gate_failures"] == []
    census = result["report"]["trending_census"]
    assert census["per_venue"]["binance"] == {
        "bars": 17280, "trending_bars": 10488,
        "trending_share": pytest.approx(10488 / 17280),
    }
    assert census["per_venue"]["bitget"] == {
        "bars": 17280, "trending_bars": 10606,
        "trending_share": pytest.approx(10606 / 17280),
    }
    splits = census["splits"]
    assert splits["train"] == {
        "labelled": 22198, "trending_labelled": 13545,
        "trending_share": pytest.approx(13545 / 22198),
    }
    assert splits["validation"]["trending_labelled"] == 3512
    assert splits["test"]["trending_labelled"] == 3476
    # The fit trained on the trending rows only.
    assert result["report"]["evaluation"]["train_rows"] == 13545


def test_floors_report_only_below_minimums():
    """Under the D1 floors (train >= 2000, val/test >= 300 trending rows
    per asset/horizon) the trainer refuses and the census rides on the
    exception, so the blocked report measures the shortfall. Chop frames
    have plenty of labelled rows but ZERO trending rows: the floor (not
    a data shortage) is what refuses them. 17,280 bars span the
    standard splits, so the split-size check passes and the floor
    check is the one that fires."""
    chop = _chop_frames(n_bars=17280)
    with pytest.raises(TrainingDataUnavailable) as excinfo:
        _train(chop)
    assert "floors 2000/300/300" in str(excinfo.value)
    census = excinfo.value.census
    assert census is not None
    assert census["per_venue"]["binance"]["bars"] == 17280
    assert census["per_venue"]["binance"]["trending_bars"] == 0
    assert census["splits"]["train"]["labelled"] >= 300
    assert census["splits"]["train"]["trending_labelled"] == 0


def test_too_few_labelled_rows_still_carries_census():
    """A frame set too short to label at all is blocked the same way:
    the census rides on the exception (zero labelled rows measured,
    not merely named)."""
    tiny = _regime_frames(n_bars=200)
    with pytest.raises(TrainingDataUnavailable) as excinfo:
        _train(tiny)
    assert "not enough labelled rows: 0" in str(excinfo.value)
    census = excinfo.value.census
    assert census is not None
    assert census["per_venue"]["binance"]["bars"] == 200
    assert census["splits"]["train"]["labelled"] == 0


def test_census_standalone_entry_point(tmp_path):
    """`--trending-census` prints the census without training: the same
    numbers the report carries, available before a run is attempted."""
    frames = _regime_frames(n_bars=2000)
    train_end, test_start, _ = _splits()
    census = regime2_training.trending_census(
        frames, asset="BTC", train_end=train_end, test_start=test_start,
        horizon=5,
    )
    assert census["per_venue"]["binance"]["bars"] == 2000
    assert 0.0 < census["per_venue"]["binance"]["trending_share"] < 1.0
    assert census["splits"]["train"]["trending_labelled"] > 0
    # The standalone census agrees with the trainer's census on the
    # same frames: one definition of "labelled" (the BTC-lead
    # exclusion), not two.
    std_frames = _frames()
    std_census = regime2_training.trending_census(
        std_frames, asset="BTC", train_end=train_end, test_start=test_start,
        horizon=5,
    )
    result = _train(std_frames)
    assert result["publish_kind"] == "model"
    assert result["report"]["trending_census"] == std_census


# --- D2: the frozen gate ------------------------------------------------------

def test_gate_frozen_at_0_30_and_shared_with_v1():
    """v2 inherits the v1 gate object, not a copy: ER > 0.30, frozen,
    no retuning, no sensitivity runs in the trainer."""
    assert ER_THRESHOLD == 0.30
    assert regime2_training.ER_THRESHOLD == ER_THRESHOLD
    # The mask applies that same threshold (the boundary test pins the
    # strict >); the trainer never retunes it.
    assert regime2_training.trending_mask is not None
    assert forward_audit.ISSUE_REGIME_V2_CHALLENGERS is True


def test_train_serve_er_equivalence():
    """The trainer's mask and the live gate agree bar-for-bar on the same
    closes: a row the trainer counted as trending is a row the cycle
    would issue on, and vice versa."""
    frames = _regime_frames()
    closes = frames["binance|BTC"][0]["close"]
    mask = regime2_training.trending_mask(closes)
    # Sample every 97th bar past the window (plus the first and last):
    # the live gate sees only the last 21 closes, never the future.
    indices = list(range(20, len(closes), 97)) + [len(closes) - 1]
    for i in indices:
        window = _candles(closes.iloc[i - 20:i + 1].tolist())
        assert trending_gate(window) == bool(mask.iloc[i])


# --- D3: the subset trainer learns the trending signal -----------------------

def test_subset_trainer_learns_where_all_rows_learns_wrong_sign():
    """On alternating-momentum frames the trending rows carry +momentum
    and the chop rows carry -momentum. The v2 fit (trending rows only)
    learns a positive ret_1 weight; the all-rows archive fit learns a
    negative one. The subset trainer is not the all-rows trainer with a
    different label: it fits a different direction."""
    frames = _regime_frames()
    result = _train(frames)
    assert result["publish_kind"] == "model"
    assert result["report"]["gate_failures"] == []
    v2_weights = result["artifact"]["weights"]
    assert v2_weights[1] > 0  # ret_1 is FEATURES[0]
    train_end, test_start, trained_through = _splits()
    archive = archive_train_run(
        frames, asset="BTC", horizon=5, train_end=train_end,
        test_start=test_start, trained_through=trained_through,
        provenance=dict(PROV), code_version="test",
    )
    assert archive["publish_kind"] == "model"
    archive_weights = archive["artifact"]["weights"]
    assert archive_weights[1] < 0
    assert v2_weights[1] > archive_weights[1]
    # The v2 fit trained on the trending labelled rows only.
    assert result["report"]["evaluation"]["train_rows"] == 10839


def test_held_out_evaluation_is_trending_subset_only():
    """The held-out test slice is evaluated on trending rows only, so the
    paired gain is measured on the targets v2 actually issues on -- not
    diluted by the chop rows it abstains on."""
    frames = _regime_frames()
    result = _train(frames)
    assert result["publish_kind"] == "model"
    evaluation = result["report"]["evaluation"]
    assert evaluation["test"]["rows"] == 2815
    assert evaluation["test"]["brier_gain"] > 0.005
    assert evaluation["validation"]["rows"] == 3088
    assert evaluation["validation"]["brier_gain"] > 0.005
    # The archive fit on the same frames evaluates on all rows instead.
    train_end, test_start, trained_through = _splits()
    archive = archive_train_run(
        frames, asset="BTC", horizon=5, train_end=train_end,
        test_start=test_start, trained_through=trained_through,
        provenance=dict(PROV), code_version="test",
    )
    assert archive["report"]["evaluation"]["test"]["rows"] == 5760


def test_feature_digest_differs_from_archive():
    """The digest pins the trending-subset descriptor, so a regime_v2
    artifact can never be mistaken for an archive_v1 artifact (or vice
    versa) at import or at pin time."""
    assert FEATURE_DIGEST != ARCHIVE_FEATURE_DIGEST
    expected = hashlib.sha256(
        json.dumps([FEATURE_CONTRACT, *FEATURES, SUBSET_DESCRIPTOR],
                   separators=(",", ":")).encode()).hexdigest()
    assert FEATURE_DIGEST == expected


def test_no_signal_run_publishes_report_only():
    """A trending subset with no learnable signal fails its gates and
    publishes a report only -- no model artifact. (Zero-momentum
    frames: the returns are pure noise, so no feature predicts the
    label; the trending subset is large enough that the floors pass
    and the gates, not the floors, refuse it.)"""
    zero = _regime_frames(mom=0.0)
    result = _train(zero)
    assert result["publish_kind"] == "report"
    assert result["artifact"] is None
    assert result["report"]["gate_failures"]


# --- Prefix acceptance + issue() / settle() -----------------------------------

def test_regime2_prefix_accepted_everywhere_it_must_be():
    """D8: regime_v2 starts with regime_, so the existing prefix covers
    it -- no prefix-list change, and issue() accepts its rows."""
    assert FAMILY.startswith("regime_")
    assert "regime_" in CHALLENGER_PREFIXES
    assert FAMILY.startswith(CHALLENGER_PREFIXES)


def test_issue_accepts_regime2_challenger_rows(tmp_path):
    store = _store(tmp_path, name="regime2-issue.sqlite")
    try:
        ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
        with store.connect() as conn:
            comp = ShadowModelCompetition(store)
            saved = comp.issue(
                conn, samples=[], features=(0.25,) * 10, now=NOW,
                venue="hyperliquid", contract="BTC", quote="USDC",
                asset="BTC", horizon=5,
                reference_opened_at=NOW - timedelta(minutes=30),
                target_opened_at=NOW - timedelta(minutes=25),
                issued_at=NOW - timedelta(minutes=25),
                incumbent_up=0.6, baseline_up=0.45,
                challengers=[{"family": "regime_v2", "probability": 0.7,
                              "training_rows": 100, "model_sha256": "a" * 64}],
            )
            assert saved == 1
            row = conn.execute(
                """SELECT family,probability_up,incumbent_up,training_count,
                          training_sha256
                   FROM rival_model_shadow_forecasts
                   WHERE family='regime_v2'"""
            ).fetchone()
            assert row["probability_up"] == 0.7
            assert row["incumbent_up"] == 0.6
            assert row["training_count"] == 100
            assert row["training_sha256"] == "a" * 64
    finally:
        store.close()


def test_regime2_rows_settle_from_incumbent_outcome(tmp_path):
    store = _store(tmp_path, name="regime2-settle.sqlite")
    try:
        market = PerpetualMarketStore(store)
        ForwardAccuracyMonitor(market).initialize()
        comp = ShadowModelCompetition(store)
        opened = NOW - timedelta(minutes=35)
        target = NOW - timedelta(minutes=25)
        issued = NOW - timedelta(minutes=30)
        settled = NOW - timedelta(minutes=19)
        with store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_forward_audit (
                     venue,contract,quote,asset,horizon_minutes,model_version,
                     reference_opened_at,reference_close,issued_at,
                     target_opened_at,probability_up,baseline_up,
                     training_evaluated,settled_at,outcome_up)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("hyperliquid", "BTC", "USDC", "BTC", 5, FORWARD_MODEL,
                 opened.isoformat(), 100.0, issued.isoformat(),
                 target.isoformat(), 0.6, 0.45, 100,
                 settled.isoformat(), 1),
            )
            assert comp.issue(
                conn, samples=[], features=(0.25,) * 10, now=issued,
                venue="hyperliquid", contract="BTC", quote="USDC",
                asset="BTC", horizon=5,
                reference_opened_at=opened,
                target_opened_at=target, issued_at=issued,
                incumbent_up=0.6, baseline_up=0.45,
                challengers=[{"family": "regime_v2", "probability": 0.7,
                              "training_rows": 100, "model_sha256": "a" * 64}],
            ) == 1
        assert comp.settle(now=NOW) == 1
        with store.connect() as conn:
            row = conn.execute(
                """SELECT settled_at,outcome_up,model_brier,incumbent_brier
                   FROM rival_model_shadow_forecasts
                   WHERE family='regime_v2'"""
            ).fetchone()
            assert row["settled_at"] == settled.isoformat()
            assert row["outcome_up"] == 1
            assert row["model_brier"] == pytest.approx((0.7 - 1) ** 2)
            assert row["incumbent_brier"] == pytest.approx((0.6 - 1) ** 2)
    finally:
        store.close()


# --- regime2_calls(): gate AND artifact ---------------------------------------

def test_regime2_calls_requires_gate_and_artifact(tmp_path):
    """v2 issues only when BOTH hold: the gate passes AND a pinned
    artifact is serving. Gate-fail and unavailable are different
    outcomes (the cycle counts them separately)."""
    store = _store(tmp_path, name="regime2-calls.sqlite")
    try:
        model_set = Regime2ModelSet(store)
        trending = _candles([100 + i * 0.5 for i in range(21)])
        choppy = _candles([100 + (i % 2) for i in range(21)])
        vector = ((0.25,) * 32, (1.0,) * 32)
        # No artifact: no call, never raises.
        assert regime2_calls(model_set, asset="BTC", horizon=5,
                             vector=vector, candles=trending, now=NOW) == []
        # Gate passes + artifact present: one call, the artifact's
        # probability. weights [0, 1, 0, ...] on ret_1 = 0.25 gives
        # z = 0.25, Platt a=1 b=0, so sigmoid(0.25).
        doc = _valid_regime2_doc(weights=[0.0, 1.0] + [0.0] * 31)
        assert model_set.import_document("file-1", json.dumps(doc).encode()) == 1
        calls = regime2_calls(model_set, asset="BTC", horizon=5,
                              vector=vector, candles=trending, now=NOW)
        assert len(calls) == 1
        assert calls[0]["family"] == "regime_v2"
        assert calls[0]["probability"] == pytest.approx(
            1 / (1 + math.exp(-0.25)))
        assert calls[0]["probability"] == pytest.approx(
            regime2_training.probability(doc, vector[0]))
        assert calls[0]["training_rows"] == 1000
        assert calls[0]["model_sha256"] == model_sha256(doc)
        # Gate fails + artifact present: no call (abstention, not
        # unavailability).
        assert regime2_calls(model_set, asset="BTC", horizon=5,
                             vector=vector, candles=choppy, now=NOW) == []
        # Every feature group unavailable: no call.
        assert regime2_calls(
            model_set, asset="BTC", horizon=5,
            vector=((0.25,) * 32, (0.0,) * 32),
            candles=trending, now=NOW) == []
        # A wrong-width vector is an abstention, never a crash.
        assert regime2_calls(
            model_set, asset="BTC", horizon=5,
            vector=((0.25,) * 31, (1.0,) * 31),
            candles=trending, now=NOW) == []
    finally:
        store.close()


# --- Cycle: kill switch, abstention vs unavailable ----------------------------

def test_kill_switch_stops_regime2_issuance_in_cycle(tmp_path, monkeypatch):
    """Flag on: the cycle issues regime_v2 shadow rows (artifact imported
    first). Flag off: none new, while the incumbent cycle still runs."""
    assert forward_audit.ISSUE_REGIME_V2_CHALLENGERS is True
    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "regime2-cycle.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        _seed_shape_candles(market, now=now, shape="trend")
        model_set = Regime2ModelSet(store)
        doc = _valid_regime2_doc()
        assert model_set.import_document("file-1", json.dumps(doc).encode()) == 1
        monkeypatch.setattr(forward_audit, "ISSUE_REGIME_V2_CHALLENGERS", True)
        monitor.run_once(now=now)
        shadow_on, incumbent_on = _cycle_counts(store)
        assert shadow_on >= 1  # the control proves this cycle can issue regime_v2
        assert incumbent_on >= 1
        monkeypatch.setattr(forward_audit, "ISSUE_REGIME_V2_CHALLENGERS", False)
        monitor.run_once(now=now)
        assert _cycle_counts(store) == (shadow_on, incumbent_on)
    finally:
        store.close()


def test_choppy_cycle_abstains_but_incumbent_runs(tmp_path, monkeypatch, caplog):
    """Gate-fail is an abstention (regime2_abstained), not an
    unavailability: the artifact is there, the market is not trending."""
    import logging

    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "regime2-choppy.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        _seed_shape_candles(market, now=now, shape="choppy")
        model_set = Regime2ModelSet(store)
        doc = _valid_regime2_doc()
        assert model_set.import_document("file-1", json.dumps(doc).encode()) == 1
        monkeypatch.setattr(forward_audit, "ISSUE_REGIME_V2_CHALLENGERS", True)
        with caplog.at_level(logging.INFO):
            monitor.run_once(now=now)
        shadow, incumbent = _cycle_counts(store)
        assert shadow == 0
        assert incumbent >= 1
        match = __import__("re").search(
            r"regime2_issued=(\d+) regime2_abstained=(\d+) regime2_unavailable=(\d+)",
            caplog.text)
        assert match is not None
        assert int(match.group(1)) == 0
        assert int(match.group(2)) >= 1
        assert int(match.group(3)) == 0
    finally:
        store.close()


def test_no_artifact_cycle_is_unavailable_not_abstained(tmp_path, monkeypatch, caplog):
    """No imported artifact: the cycle counts regime2_unavailable (the
    gate may pass, but there is nothing to serve) and the incumbent
    still runs. v1 keeps issuing as the control in this state."""
    import logging

    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "regime2-unavail.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        _seed_shape_candles(market, now=now, shape="trend")
        monkeypatch.setattr(forward_audit, "ISSUE_REGIME_V2_CHALLENGERS", True)
        with caplog.at_level(logging.INFO):
            monitor.run_once(now=now)
        shadow, incumbent = _cycle_counts(store)
        assert shadow == 0
        assert incumbent >= 1
        match = __import__("re").search(
            r"regime2_issued=(\d+) regime2_abstained=(\d+) regime2_unavailable=(\d+)",
            caplog.text)
        assert match is not None
        assert int(match.group(1)) == 0
        assert int(match.group(2)) == 0
        assert int(match.group(3)) >= 1
    finally:
        store.close()


def test_gate_exception_never_blocks_incumbent(tmp_path, monkeypatch):
    """A regime2_calls that explodes is an abstention, never a failed
    incumbent cycle."""
    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "regime2-boom.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        _seed_shape_candles(market, now=now, shape="trend")

        def _boom(*args, **kwargs):
            raise RuntimeError("regime2 exploded")

        monkeypatch.setattr(forward_audit, "regime2_calls", _boom)
        monkeypatch.setattr(forward_audit, "ISSUE_REGIME_V2_CHALLENGERS", True)
        monitor.run_once(now=now)
        shadow, incumbent = _cycle_counts(store)
        assert shadow == 0
        assert incumbent >= 1
    finally:
        store.close()


# --- selected(): D14 exact pin -----------------------------------------------

def test_selected_pin_branch(tmp_path):
    """D14 pinning: the pinned fit holds the slot; a queued fit waits;
    rejection advances the queue; the promotion never transfers."""
    from parlay_bot.perpetuals import offline_model_pins

    store = _store(tmp_path, name="regime2-selected.sqlite")
    try:
        model_set = Regime2ModelSet(store)
        doc1 = _valid_regime2_doc()
        model_set.import_document("file-1", json.dumps(doc1).encode())
        identity1 = artifact_identity(store, "BTC", 5)
        assert identity1 is not None
        assert identity1["feature_digest"] == FEATURE_DIGEST
        assert identity1["graded_count"] == 0
        assert identity1["trained_through"] == doc1["trained_through"]
        _promotion_row(store, family="regime_v2", evaluated_at=NOW,
                       artifact=identity1)
        promoter = FamilyPromotion(store)
        kwargs = {"asset": "BTC", "venue": "hyperliquid", "contract": "BTC",
                  "quote": "USDC", "horizon": 5, "now": NOW}
        assert promoter.selected(**kwargs) == "regime_v2"
        # A mismatched weights SHA: not the artifact that earned promotion.
        tampered = dict(identity1, weights_sha256="0" * 64)
        _promotion_row(store, family="regime_v2", evaluated_at=NOW,
                       artifact=tampered)
        assert promoter.selected(**kwargs) is None
        # Stale promotion (older than 48h) fails safe to the incumbent.
        _promotion_row(store, family="regime_v2",
                       evaluated_at=NOW - timedelta(hours=49),
                       artifact=identity1)
        assert promoter.selected(**kwargs) is None
        _promotion_row(store, family="regime_v2", evaluated_at=NOW,
                       artifact=identity1)
        assert promoter.selected(**kwargs) == "regime_v2"
        # One graded row under the pinned fit's hash, then a retrain: the
        # pinned fit holds the slot, the new fit queues behind it.
        sha1 = model_sha256(doc1)
        with store.connect() as conn:
            _insert_shadow_row(conn, p=0.7, y=1, sha=sha1, family=FAMILY)
        assert artifact_identity(store, "BTC", 5)["graded_count"] == 1
        doc2 = _valid_regime2_doc(
            trained_through=NOW - timedelta(hours=1),
            weights=[0.02 * ((i % 5) - 2) for i in range(33)],
        )
        assert model_sha256(doc2) != sha1
        model_set.import_document("file-2", json.dumps(doc2).encode())
        model_set._loaded_at = None  # force a real reload, not the cache
        active = model_set.active("BTC", 5, now=NOW)
        assert active is not None
        assert active["_sha256"] == sha1  # pinned fit holds the slot
        assert model_set.queue_depth("BTC", 5, now=NOW) == 1
        identity_still1 = artifact_identity(store, "BTC", 5)
        assert identity_still1["weights_sha256"] == identity1["weights_sha256"]
        assert identity_still1["graded_count"] == 1
        assert promoter.selected(**kwargs) == "regime_v2"
        # Reject the pinned fit: the queued fit is admitted and starts
        # from zero; the promotion does not transfer to it.
        offline_model_pins.set_verdict(
            store, FAMILY, "BTC", 5, sha1, "rejected", now=NOW)
        model_set._loaded_at = None
        active2 = model_set.active("BTC", 5, now=NOW)
        assert active2 is not None
        assert active2["_sha256"] == model_sha256(doc2)
        assert model_set.queue_depth("BTC", 5, now=NOW) == 0
        identity2 = artifact_identity(store, "BTC", 5)
        assert identity2["weights_sha256"] != identity1["weights_sha256"]
        assert identity2["graded_count"] == 0  # no rows under the new fit
        assert promoter.selected(**kwargs) is None
        # No imported artifact at all: no identity, no serve.
        with store.connect() as conn:
            conn.execute("DELETE FROM rival_regime2_models")
        model_set._loaded_at = None
        assert artifact_identity(store, "BTC", 5) is None
        assert promoter.selected(**kwargs) is None
    finally:
        store.close()


def test_evaluate_records_regime2_artifact(tmp_path):
    store = _store(tmp_path, name="regime2-eval.sqlite")
    try:
        model_set = Regime2ModelSet(store)
        doc = _valid_regime2_doc()
        model_set.import_document("file-1", json.dumps(doc).encode())
        sha = model_sha256(doc)
        with store.connect() as conn:
            # Seeded rows must carry the pinned doc's hash: evaluate()
            # filters regime_v2 rows by the pinned artifact (D14).
            _insert_shadow_row(conn, p=0.7, y=1, sha=sha, family=FAMILY)
            _insert_shadow_row(conn, p=0.4, y=0, sha=sha, family=FAMILY,
                               ref=NOW - timedelta(minutes=60),
                               issued=NOW - timedelta(minutes=55),
                               target=NOW - timedelta(minutes=55))
        result = FamilyPromotion(store).evaluate(
            asset="BTC", venue="hyperliquid", contract="BTC", quote="USDC",
            horizon=5, now=NOW)
        assert "regime_v2" in result["families"]
        recorded = result["families"]["regime_v2"]["artifact"]
        assert recorded is not None
        assert recorded["feature_digest"] == FEATURE_DIGEST
        assert recorded["graded_count"] == 2
        assert recorded["weights_sha256"] == artifact_identity(
            store, "BTC", 5)["weights_sha256"]
        # Two graded rows is far below the Route B minimums: held, never
        # promoted on a backtest-sized sample.
        assert result["status"] == "held"
        assert result["selected_family"] is None
    finally:
        store.close()


def test_promotion_qualifies_on_regime2_subset(learning_store):
    """Route B on the issued subset: 168 winning targets, full quote
    coverage, after-cost proofs pass -> promoted_paper, and selected()
    serves it (exact pin holds: same weights SHA, same count)."""
    now = datetime(2026, 10, 31, 16, tzinfo=UTC)
    family = _seed_regime2_promotion(learning_store, now)
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


# --- D5: selection decomposition ---------------------------------------------

def _report_fixture(tmp_path, name, *, subset_incumbent_p, subset_p, rest_p,
                    v1_p=0.55):
    """5 regime_v2 rows on a subset + 5 matching regime_v1 rows + 15
    incumbent rows total in the window.

    The subset targets carry the v2 rows, the v1 rows (same targets,
    same incumbent probability) and their incumbent rows; the other 10
    incumbent rows are the rest of the window, at rest_p.
    """
    store = _store(tmp_path, name=name)
    monitor = ForwardAccuracyMonitor(PerpetualMarketStore(store))
    monitor.initialize()
    with store.connect() as conn:
        for i in range(5):
            target = NOW - timedelta(days=1, minutes=5 * i)
            _insert_regime2_row(conn, target=target,
                                issued=target - timedelta(minutes=5),
                                p=subset_p, incumbent_p=subset_incumbent_p, y=1)
            _insert_regime_row(conn, target=target,
                               issued=target - timedelta(minutes=5),
                               p=v1_p, incumbent_p=subset_incumbent_p, y=1)
            _insert_incumbent_row(conn, target=target,
                                  issued=target - timedelta(minutes=5),
                                  p=subset_incumbent_p, y=1)
        for i in range(10):
            target = NOW - timedelta(days=1, minutes=5 * (i + 5))
            _insert_incumbent_row(conn, target=target,
                                  issued=target - timedelta(minutes=5),
                                  p=rest_p, y=1)
    return store, trial_report(store, asset="BTC", horizon=5, now=NOW)


def test_trial_report_decomposition_hard_subset(tmp_path):
    """v2 beats the incumbent on its subset while the incumbent is better
    overall: paired gain positive, v2-vs-v1 gain positive, selection
    effect large positive (the subset was harder than average for the
    incumbent), coverage 5/15.

    Subset: incumbent 0.3025, v2 0.16, v1 0.2025. Rest of the window:
    incumbent 0.0625, so all-target incumbent Brier is 0.1425 -- better
    than v2's 0.16 on its subset, even though v2 beat the incumbent on
    the subset itself.
    """
    store, report = _report_fixture(
        tmp_path, "regime2-report-hard.sqlite",
        subset_incumbent_p=0.45, subset_p=0.6, rest_p=0.75)
    try:
        assert report["issued"] == 5
        assert report["decided"] == 5
        assert report["hits"] == 5
        assert report["windows"] == 5
        assert report["model_brier"] == pytest.approx(0.16)
        assert report["incumbent_brier"] == pytest.approx(0.3025)
        # Paired gain on the subset: v2 beat the incumbent there.
        assert report["paired_gain"] == pytest.approx(0.3025 - 0.16)
        # v2-vs-v1 on the same targets: v2 0.16 vs v1 0.2025.
        assert report["paired_gain_vs_regime_v1"] == pytest.approx(0.2025 - 0.16)
        # Selection effect: incumbent Brier on the subset (0.3025) minus
        # incumbent Brier on all 15 targets ((5*0.3025 + 10*0.0625)/15).
        all_brier = (5 * 0.3025 + 10 * 0.0625) / 15
        assert all_brier == pytest.approx(0.1425)
        assert report["selection_effect"] == pytest.approx(0.3025 - all_brier)
        assert report["selection_effect"] > 0
        # The incumbent is better overall (0.1425) than v2's subset
        # Brier (0.16): the win is on the subset, not overall.
        assert report["model_brier"] > all_brier
        assert report["coverage"] == pytest.approx(5 / 15)
        # Availability: the artifact was there for every issued row.
        assert report["availability_ratio"] == pytest.approx(1.0)
    finally:
        store.close()


def test_trial_report_decomposition_easy_subset(tmp_path):
    """The same decomposition with an easy subset must come out negative:
    the subset was easier than average for the incumbent, so a naive
    v2-vs-overall comparison would overstate the edge."""
    store, report = _report_fixture(
        tmp_path, "regime2-report-easy.sqlite",
        subset_incumbent_p=0.55, subset_p=0.6, rest_p=0.5)
    try:
        assert report["paired_gain"] == pytest.approx(0.2025 - 0.16)
        assert report["paired_gain"] > 0
        all_brier = (5 * 0.2025 + 10 * 0.25) / 15
        assert report["selection_effect"] == pytest.approx(0.2025 - all_brier)
        assert report["selection_effect"] < 0
    finally:
        store.close()


def test_trial_report_empty(tmp_path):
    store = _store(tmp_path, name="regime2-report-empty.sqlite")
    try:
        ForwardAccuracyMonitor(PerpetualMarketStore(store)).initialize()
        assert trial_report(store, asset="BTC", horizon=5, now=NOW) == {"issued": 0}
    finally:
        store.close()


def test_trial_report_availability_below_one(tmp_path):
    """Availability counts issued rows against the v1 control's issued
    rows on the same targets (v1 issues whenever the gate passes, so
    its rows are the gate-passed windows). With no v1 rows to measure
    against, availability is None -- not zero: the page must not claim
    the artifact was absent when there is no control to compare with.
    Coverage still measures v2 issued rows against all incumbent
    windows in the window."""
    store = _store(tmp_path, name="regime2-report-avail.sqlite")
    try:
        monitor = ForwardAccuracyMonitor(PerpetualMarketStore(store))
        monitor.initialize()
        with store.connect() as conn:
            for i in range(5):
                target = NOW - timedelta(days=1, minutes=5 * i)
                _insert_regime2_row(conn, target=target,
                                    issued=target - timedelta(minutes=5),
                                    p=0.6, incumbent_p=0.45, y=1)
                _insert_incumbent_row(conn, target=target,
                                      issued=target - timedelta(minutes=5),
                                      p=0.45, y=1)
            # Five more incumbent windows where v2 issued nothing.
            for i in range(5, 10):
                target = NOW - timedelta(days=1, minutes=5 * i)
                _insert_incumbent_row(conn, target=target,
                                      issued=target - timedelta(minutes=5),
                                      p=0.45, y=1)
        report = trial_report(store, asset="BTC", horizon=5, now=NOW)
        assert report["issued"] == 5
        # windows counts the v2 subset's distinct targets (5), while
        # coverage measures them against all incumbent windows (10).
        assert report["windows"] == 5
        assert report["coverage"] == pytest.approx(0.5)
        assert report["availability_ratio"] is None
    finally:
        store.close()


# --- D4: the weekly shared run ------------------------------------------------

async def test_weekly_archive_trains_regime2_as_third_family(tmp_path):
    """The shared run writes a regime-v2 report (with its census) next
    to the archive and GBM reports, on the same frames, same splits,
    same provenance -- and a model file when the gates pass."""
    from parlay_bot.research import weekly_archive

    train_end, test_start, trained_through = _splits()
    frames = _frames()
    training = {
        "asset": "BTC", "horizon": 5, "train_end": train_end,
        "test_start": test_start, "trained_through": trained_through,
        "provenance": dict(PROV), "code_version": "test",
    }
    await weekly_archive.train_weekly_archive(
        frames, training=training, census={}, out=tmp_path, publish=False)
    report_path = tmp_path / "regime-v2-BTC-5m-report.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text())
    assert report["family"] == "regime_v2"
    assert report["trending_census"]["per_venue"]["binance"]["trending_bars"] == 10488
    assert report["gates_passed"] is True
    assert report["training_status"] == "passed"
    model_path = tmp_path / "regime-v2-BTC-5m-model.json"
    assert model_path.exists()
    model = json.loads(model_path.read_text())
    assert model["family"] == "regime_v2"
    assert is_regime2_document(model_path.read_bytes())
    # The other two families still trained in the same run.
    assert (tmp_path / "archive-v1-BTC-5m-report.json").exists()
    assert (tmp_path / "gbm-v1-BTC-5m-report.json").exists()


async def test_weekly_archive_blocked_regime2_carries_census(tmp_path):
    """A floor failure in the shared run still writes a blocked report
    that measures the shortfall (trending_census), not just its name."""
    from parlay_bot.research import weekly_archive

    train_end, test_start, trained_through = _splits()
    frames = _regime_frames(n_bars=200)
    training = {
        "asset": "BTC", "horizon": 5, "train_end": train_end,
        "test_start": test_start, "trained_through": trained_through,
        "provenance": dict(PROV), "code_version": "test",
    }
    await weekly_archive.train_weekly_archive(
        frames, training=training, census={}, out=tmp_path, publish=False)
    report = json.loads(
        (tmp_path / "regime-v2-BTC-5m-report.json").read_text())
    assert report["training_status"] == "blocked"
    assert report["gates_passed"] is False
    assert report["trending_census"]["splits"]["train"]["trending_labelled"] < 2000
    assert not (tmp_path / "regime-v2-BTC-5m-model.json").exists()


# --- D8: never an OTG live lean ------------------------------------------------

def test_live_lean_none_for_regime2():
    """regime_v2 has no read-only recompute path: its live lean is None,
    so it can never become a WATCH/alert/DM/Auto-Hunt candidate or appear
    in the OTG display as a live call."""
    assert live_lean(None, asset="BTC", horizon=5, venue="hyperliquid",
                     model_version="regime_v2") is None
    assert live_lean(None, asset="BTC", horizon=5, venue="hyperliquid",
                     model_version="regime_v2", contract="BTC",
                     quote="USDC") is None


# --- Import validation ---------------------------------------------------------

def test_regime2_document_import_validates_and_serves(tmp_path):
    """A valid regime_v2 doc imports, serves through regime2_calls with
    the pinned weights, and a second import of the same file is a no-op.
    A doc with the archive digest is refused: the digest pins the
    trending-subset descriptor."""
    store = _store(tmp_path, name="regime2-import.sqlite")
    try:
        model_set = Regime2ModelSet(store)
        doc = _valid_regime2_doc()
        payload = json.dumps(doc).encode()
        assert is_regime2_document(payload)
        assert model_set.import_document("file-1", payload) == 1
        assert model_set.import_document("file-1", payload) == 0
        active = model_set.active("BTC", 5, now=NOW)
        assert active is not None
        assert active["_sha256"] == model_sha256(doc)
        # The archive digest on a regime_v2 doc is a different model.
        wrong = dict(doc, feature_digest=ARCHIVE_FEATURE_DIGEST)
        with pytest.raises(ValueError):
            model_set.import_document("file-2", json.dumps(wrong).encode())
        # Calibration status reads off the imported artifact: the
        # raw Platt record, not a governance label.
        status = calibration_status(store, "BTC", 5)
        assert status["status"] == "evaluated"
        assert status["ece"] == 0.02
    finally:
        store.close()
