"""gbm_v1 shadow issuance + Route B wiring + D14 pinning (autonomous loop
Phase 2, spec §3.4, §11).

Covers: the GBM trainer (LightGBM on the 32-feature archive contract,
shared splits with the logistic, pooled gates + Track C context-absent
bar, portable parity ≤1e-6), the Drive import validation (provenance,
gates, calibration, trees/weights SHA), shadow issuance through
``issue()`` with the ``gbm_`` prefix, generic settlement copying the
incumbent outcome, the ``selected()`` D14 pin branch (exact weights-SHA
match; a queued fit waits behind the pinned fit; rejection advances the
queue), the ``evaluate()`` artifact recording (pinned-sha row filter),
the kill switch in the 5-minute cycle, and the queue-depth line on the
status card.
"""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.discord_app.research_alerts import research_alerts
from parlay_bot.perpetuals import forward_audit, offline_model_pins
from parlay_bot.perpetuals.family_governance import FamilyPromotion
from parlay_bot.perpetuals.forward_audit import MODEL as FORWARD_MODEL
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.gbm_model import (
    FAMILY,
    GbmModelSet,
    artifact_identity,
    calibration_status,
    gbm_calls,
    is_gbm_document,
    model_sha256,
)
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.research.archive_features import FEATURE_DIGEST
from parlay_bot.research.archive_training import train_run as archive_train_run
from parlay_bot.research.gbm_training import (
    GATES,
    train_run,
    usable,
    validate,
)
from parlay_bot.storage.hot import SQLiteHotStore
from tests.test_archive_model import PROV, _frames, _splits
from tests.test_archive_v1_shadow import (
    _insert_shadow_row,
    _promotion_row,
    _seed_cycle_candles,
    _store,
)

NOW = datetime(2026, 11, 7, 12, 0, 30, tzinfo=UTC)


def _payload(doc) -> bytes:
    return json.dumps(doc).encode()


def _frames_seeded(seed_base: int):
    """The archive synthetic frames with different RNG seeds."""
    from parlay_bot.research.archive_features import frame_features
    from tests.test_archive_model import _synthetic_frame

    out = {}
    for venue, seed in (("binance", seed_base), ("bitget", seed_base + 1)):
        k = _synthetic_frame(17280, seed)
        out[f"{venue}|BTC"] = (k, frame_features(k, None, "BTC"))
    return out


@pytest.fixture(scope="module")
def gbm_doc():
    """One real trained GBM artifact (module scope: one ~7s train)."""
    train_end, test_start, trained_through = _splits()
    result = train_run(
        _frames(), asset="BTC", horizon=5, train_end=train_end,
        test_start=test_start, trained_through=trained_through,
        provenance=dict(PROV), code_version="test-sha",
    )
    assert result["publish_kind"] == "model", result["report"]
    return result["artifact"]


@pytest.fixture(scope="module")
def gbm_doc2():
    """A second real fit on different synthetic bytes, trained later."""
    train_end, test_start, trained_through = _splits()
    result = train_run(
        _frames_seeded(11), asset="BTC", horizon=5, train_end=train_end,
        test_start=test_start,
        trained_through=trained_through + timedelta(hours=1),
        provenance=dict(PROV), code_version="test-sha",
    )
    assert result["publish_kind"] == "model", result["report"]
    return result["artifact"]


def test_gbm_trains_passes_gates_and_shared_splits(gbm_doc):
    doc = validate(gbm_doc)
    assert doc["schema"] == "rival-gbm-model-v1"
    assert doc["family"] == "gbm_v1"
    assert doc["model_version"] == "gbm-offline-v1"
    assert doc["feature_contract"] == "archive-candle-premium-calendar-v1"
    assert doc["feature_digest"] == FEATURE_DIGEST
    assert len(doc["trees"]) == 400
    assert doc["gates_passed"] is True
    ev = doc["evaluation"]
    # The pooled-precedent gates are exactly 0.005 / 0.005 / 0.55 / 0.03
    # (D7); the artifact must clear them, not merely equal them.
    assert GATES["min_validation_gain"] == 0.005
    assert GATES["min_test_gain"] == 0.005
    assert GATES["min_test_daily_positive"] == 0.55
    assert GATES["max_test_ece"] == 0.03
    assert ev["validation"]["brier_gain"] > GATES["min_validation_gain"]
    assert ev["test"]["brier_gain"] > GATES["min_test_gain"]
    for split in ("validation", "test"):
        assert ev[split]["daily_positive_fraction"] >= GATES["min_test_daily_positive"]
        assert ev[split]["ece"] <= GATES["max_test_ece"]
    # Track C: the context-absent test (tape/premium/funding groups NaN,
    # as the live vector does when the bundle is absent) clears the same
    # 0.005 bar or the artifact does not ship as serving-capable.
    assert ev["context_absent_test"]["brier_gain"] > 0.005
    calibration = doc["calibration"]
    assert calibration["method"] == "platt"
    assert calibration["status"] == "evaluated"
    assert calibration["ece"] <= 0.10
    assert usable(doc, now=datetime(2025, 3, 2, tzinfo=UTC))
    assert 0.02 <= doc["calibration"]["top_bucket_hit_rate"] <= 1.0
    # Portable parity (pooled precedent): the trainer refuses to publish
    # unless the pure-Python evaluator reproduces native LightGBM within
    # 1e-6 on the test slice -- enforced inside train_run, so reaching
    # here proves it; probability() itself must stay finite and clamped.
    from parlay_bot.research.gbm_training import probability

    assert 0.02 <= probability(doc, [0.0] * 32) <= 0.98
    # Shared splits with the logistic (D6 / Phase 2 workflow): the same
    # restored bytes and the same --train-end / --test-start /
    # --trained-through train the same rows for both families.
    train_end, test_start, trained_through = _splits()
    archive_result = archive_train_run(
        _frames(), asset="BTC", horizon=5, train_end=train_end,
        test_start=test_start, trained_through=trained_through,
        provenance=dict(PROV), code_version="test-sha",
    )
    assert archive_result["publish_kind"] == "model"
    archive_doc = archive_result["artifact"]
    assert doc["training_rows"] == archive_doc["training_rows"]
    assert doc["trained_through"] == archive_doc["trained_through"]
    assert (doc["evaluation"]["test"]["rows"]
            == archive_doc["evaluation"]["test"]["rows"])


def test_gbm_document_import_validates_and_serves(tmp_path, gbm_doc):
    store = _store(tmp_path, name="gbm.sqlite")
    try:
        payload = _payload(gbm_doc)
        assert is_gbm_document(payload)
        assert not is_gbm_document(b'{"schema": "rival-archive-model-v1"}')
        model_set = GbmModelSet(store)
        assert model_set.import_document("file-1", payload) == 1
        assert model_set.import_document("file-1", payload) == 0
        assert model_set.imported_file_ids() == {"file-1"}
        active = model_set.active("BTC", 5, now=NOW)
        assert active is not None and active["asset"] == "BTC"
        assert active["horizon_minutes"] == 5
        assert model_set.active("ETH", 5, now=NOW) is None
        assert model_set.serving("BTC", 5, now=NOW) == [active]
        assert model_set.queue_depth("BTC", 5, now=NOW) == 0
        # Calibration record for receipts comes from the artifact itself.
        status = calibration_status(store, "BTC", 5)
        assert status["status"] == "evaluated" and status["a"] == pytest.approx(
            gbm_doc["calibration"]["a"])
        assert calibration_status(store, "ETH", 5)["status"] == "unevaluated"
        # A provider hash that IS the normalized hash is refused (P1-4).
        bad = json.loads(payload)
        bad["provenance"]["normalized_sha256_by_month"] = {
            "binance:2025-01": "a" * 64}
        with pytest.raises(ValueError):
            GbmModelSet(store).import_document("file-bad", _payload(bad))
        # A wrong feature digest is refused.
        bad = json.loads(payload)
        bad["feature_digest"] = "0" * 64
        with pytest.raises(ValueError):
            GbmModelSet(store).import_document("file-bad2", _payload(bad))
        # Gates not passed is refused.
        bad = json.loads(payload)
        bad["gates_passed"] = False
        with pytest.raises(ValueError):
            GbmModelSet(store).import_document("file-bad3", _payload(bad))
        # A tampered tree (leaf value changed, weights SHA not updated)
        # is refused: the weights SHA is recomputed from the stored
        # trees, never trusted from a stored column.
        bad = json.loads(payload)
        for node in bad["trees"][0]:
            if len(node) == 1:
                node[0] = float(node[0]) + 0.5
                break
        with pytest.raises(ValueError):
            GbmModelSet(store).import_document("file-bad4", _payload(bad))
    finally:
        store.close()


def test_gbm_calls_skip_without_artifact_or_vector(tmp_path, gbm_doc):
    store = _store(tmp_path, name="gbm-calls.sqlite")
    try:
        model_set = GbmModelSet(store)
        vector = ([0.0] * 32, (1.0,) * 7)
        assert gbm_calls(model_set, asset="BTC", horizon=5,
                         vector=vector, now=NOW) == []
        assert gbm_calls(None, asset="BTC", horizon=5,
                         vector=vector, now=NOW) == []
        model_set.import_document("file-1", _payload(gbm_doc))
        # All-zero flags: no issuance that cycle (the flow_v3 skip rule).
        assert gbm_calls(model_set, asset="BTC", horizon=5,
                         vector=([0.0] * 32, (0.0,) * 7), now=NOW) == []
        assert gbm_calls(model_set, asset="BTC", horizon=5,
                         vector=None, now=NOW) == []
        calls = gbm_calls(model_set, asset="BTC", horizon=5,
                          vector=vector, now=NOW)
        assert len(calls) == 1
        call = calls[0]
        assert call["family"] == "gbm_v1"
        assert 0.02 <= call["probability"] <= 0.98
        assert call["training_rows"] == gbm_doc["training_rows"]
        assert call["model_sha256"] == model_sha256(gbm_doc)
        # An all-NaN vector (every group absent but flags set by the
        # caller) still scores -- trees take NaN natively -- never NaN.
        nan_calls = gbm_calls(
            model_set, asset="BTC", horizon=5,
            vector=([float("nan")] * 32, (1.0,) * 7), now=NOW)
        assert len(nan_calls) == 1
        assert math.isfinite(nan_calls[0]["probability"])
    finally:
        store.close()


def test_issue_accepts_gbm_challenger_rows(tmp_path, gbm_doc):
    store = _store(tmp_path, name="gbm-issue.sqlite")
    try:
        market = PerpetualMarketStore(store)
        ForwardAccuracyMonitor(market).initialize()
        comp = ShadowModelCompetition(store)
        opened = NOW - timedelta(minutes=5)
        target = NOW + timedelta(minutes=5)
        issued = NOW - timedelta(seconds=20)
        sha = model_sha256(gbm_doc)
        with store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_forward_audit (
                     venue,contract,quote,asset,horizon_minutes,model_version,
                     reference_opened_at,reference_close,issued_at,
                     target_opened_at,probability_up,baseline_up,
                     training_evaluated)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("hyperliquid", "BTC", "USDC", "BTC", 5, FORWARD_MODEL,
                 opened.isoformat(), 100., issued.isoformat(),
                 target.isoformat(), .6, .45, 100),
            )
            saved = comp.issue(
                conn, samples=[], features=(.25,) * 10, now=NOW,
                venue="hyperliquid", contract="BTC", quote="USDC", asset="BTC",
                horizon=5, reference_opened_at=opened,
                target_opened_at=target, issued_at=issued,
                incumbent_up=.6, baseline_up=.45,
                challengers=[{"family": "gbm_v1", "probability": 0.7,
                              "training_rows": 1000, "model_sha256": sha}],
            )
            assert saved == 1
            row = conn.execute(
                """SELECT family,probability_up,training_count,training_sha256
                   FROM rival_model_shadow_forecasts
                   WHERE family='gbm_v1'"""
            ).fetchone()
            assert row["family"] == "gbm_v1"
            assert row["probability_up"] == 0.7
            assert row["training_count"] == 1000
            assert row["training_sha256"] == sha
    finally:
        store.close()


def test_shadow_rows_settle_from_incumbent_outcome(tmp_path, gbm_doc):
    store = _store(tmp_path, name="gbm-settle.sqlite")
    try:
        market = PerpetualMarketStore(store)
        ForwardAccuracyMonitor(market).initialize()
        comp = ShadowModelCompetition(store)
        opened = NOW - timedelta(minutes=35)
        target = NOW - timedelta(minutes=25)
        issued = NOW - timedelta(minutes=30)
        # Settlement must be strictly after the target bar closes
        # (target + 5 min), or _settle_batch refuses the row.
        settled = NOW - timedelta(minutes=19)
        sha = model_sha256(gbm_doc)
        with store.connect() as conn:
            conn.execute(
                """INSERT INTO perp_forward_audit (
                     venue,contract,quote,asset,horizon_minutes,model_version,
                     reference_opened_at,reference_close,issued_at,
                     target_opened_at,probability_up,baseline_up,
                     training_evaluated,settled_at,outcome_up)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("hyperliquid", "BTC", "USDC", "BTC", 5, FORWARD_MODEL,
                 opened.isoformat(), 100., issued.isoformat(),
                 target.isoformat(), .6, .45, 100,
                 settled.isoformat(), 1),
            )
            assert comp.issue(
                conn, samples=[], features=(.25,) * 10, now=issued,
                venue="hyperliquid", contract="BTC", quote="USDC", asset="BTC",
                horizon=5, reference_opened_at=opened,
                target_opened_at=target, issued_at=issued,
                incumbent_up=.6, baseline_up=.45,
                challengers=[{"family": "gbm_v1", "probability": 0.7,
                              "training_rows": 1000, "model_sha256": sha}],
            ) == 1
        assert comp.settle(now=NOW) == 1
        with store.connect() as conn:
            row = conn.execute(
                """SELECT settled_at,outcome_up,model_brier
                   FROM rival_model_shadow_forecasts
                   WHERE family='gbm_v1'"""
            ).fetchone()
            assert row["settled_at"] == settled.isoformat()
            assert row["outcome_up"] == 1
            assert row["model_brier"] == pytest.approx((0.7 - 1) ** 2)
    finally:
        store.close()


def test_selected_pin_branch(tmp_path, gbm_doc, gbm_doc2):
    """D14 pinning: the pinned fit holds the slot; a queued fit waits;
    rejection advances the queue; the promotion never transfers."""
    store = _store(tmp_path, name="gbm-pin.sqlite")
    try:
        model_set = GbmModelSet(store)
        model_set.import_document("file-1", _payload(gbm_doc))
        identity1 = artifact_identity(store, "BTC", 5)
        assert identity1 is not None
        assert identity1["feature_digest"] == FEATURE_DIGEST
        assert identity1["graded_count"] == 0
        assert identity1["trained_through"] == gbm_doc["trained_through"]
        _promotion_row(store, family="gbm_v1", evaluated_at=NOW,
                       artifact=identity1)
        promoter = FamilyPromotion(store)
        kwargs = {"asset": "BTC", "venue": "hyperliquid", "contract": "BTC",
                  "quote": "USDC", "horizon": 5, "now": NOW}
        assert promoter.selected(**kwargs) == "gbm_v1"
        # A mismatched weights SHA with no further graded learning: not
        # the artifact that earned promotion.
        tampered = dict(identity1, weights_sha256="0" * 64)
        _promotion_row(store, family="gbm_v1", evaluated_at=NOW,
                       artifact=tampered)
        assert promoter.selected(**kwargs) is None
        # Stale promotion (older than 48h) fails safe to the incumbent.
        _promotion_row(store, family="gbm_v1",
                       evaluated_at=NOW - timedelta(hours=49),
                       artifact=identity1)
        assert promoter.selected(**kwargs) is None
        _promotion_row(store, family="gbm_v1", evaluated_at=NOW,
                       artifact=identity1)
        assert promoter.selected(**kwargs) == "gbm_v1"
        # One graded row under the pinned fit's hash, then a retrain: the
        # pinned fit holds the slot, the new fit queues behind it.
        sha1 = model_sha256(gbm_doc)
        with store.connect() as conn:
            _insert_shadow_row(conn, p=0.7, y=1, sha=sha1, family=FAMILY)
        assert artifact_identity(store, "BTC", 5)["graded_count"] == 1
        assert model_sha256(gbm_doc2) != sha1
        model_set.import_document("file-2", _payload(gbm_doc2))
        model_set._loaded_at = None  # force a real reload, not the cache
        active = model_set.active("BTC", 5, now=NOW)
        assert active is not None
        assert active["_sha256"] == sha1  # pinned fit holds the slot
        assert model_set.queue_depth("BTC", 5, now=NOW) == 1
        identity_still1 = artifact_identity(store, "BTC", 5)
        assert identity_still1["weights_sha256"] == identity1["weights_sha256"]
        assert identity_still1["graded_count"] == 1
        assert promoter.selected(**kwargs) == "gbm_v1"
        # Reject the pinned fit: the queued fit is admitted and starts
        # from zero; the promotion does not transfer to it.
        offline_model_pins.set_verdict(
            store, FAMILY, "BTC", 5, sha1, "rejected", now=NOW)
        model_set._loaded_at = None
        active2 = model_set.active("BTC", 5, now=NOW)
        assert active2 is not None
        assert active2["_sha256"] == model_sha256(gbm_doc2)
        assert model_set.queue_depth("BTC", 5, now=NOW) == 0
        identity2 = artifact_identity(store, "BTC", 5)
        assert identity2["weights_sha256"] != identity1["weights_sha256"]
        assert identity2["graded_count"] == 0  # no rows under the new fit
        assert promoter.selected(**kwargs) is None
        # No imported artifact at all: no identity, no serve.
        with store.connect() as conn:
            conn.execute("DELETE FROM rival_gbm_models")
        model_set._loaded_at = None
        assert artifact_identity(store, "BTC", 5) is None
        assert promoter.selected(**kwargs) is None
    finally:
        store.close()


def test_evaluate_records_gbm_artifact(tmp_path, gbm_doc):
    store = _store(tmp_path, name="gbm-eval.sqlite")
    try:
        model_set = GbmModelSet(store)
        model_set.import_document("file-1", _payload(gbm_doc))
        sha = model_sha256(gbm_doc)
        with store.connect() as conn:
            # Rows under the pinned fit's hash count; a row under any
            # other hash is not this family's evidence (D14).
            _insert_shadow_row(conn, p=0.7, y=1, sha=sha, family=FAMILY)
            _insert_shadow_row(conn, p=0.4, y=0, sha=sha, family=FAMILY,
                               ref=NOW - timedelta(minutes=60),
                               issued=NOW - timedelta(minutes=55),
                               target=NOW - timedelta(minutes=55))
            _insert_shadow_row(conn, p=0.9, y=1, sha="c" * 64, family=FAMILY,
                               ref=NOW - timedelta(minutes=90),
                               issued=NOW - timedelta(minutes=85),
                               target=NOW - timedelta(minutes=85))
        result = FamilyPromotion(store).evaluate(
            asset="BTC", venue="hyperliquid", contract="BTC", quote="USDC",
            horizon=5, now=NOW)
        assert "gbm_v1" in result["families"]
        recorded = result["families"]["gbm_v1"]["artifact"]
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


def test_resolve_pin_holds_then_promotes(tmp_path, gbm_doc):
    store = _store(tmp_path, name="gbm-resolve-promote.sqlite")
    try:
        model_set = GbmModelSet(store)
        model_set.import_document("file-1", _payload(gbm_doc))
        sha = model_sha256(gbm_doc)
        assert model_set.active("BTC", 5, now=NOW) is not None
        # A fresh unsettled row: evidence is not exhausted, pin holds.
        with store.connect() as conn:
            _insert_shadow_row(conn, p=0.7, y=1, sha=sha, family=FAMILY, settled=False)
        held = offline_model_pins.resolve_pin(
            store, FAMILY, "BTC", 5, selected=False, now=NOW)
        assert held is not None and held["verdict"] is None
        assert offline_model_pins.current_pin(store, FAMILY, "BTC", 5)["model_sha256"] == sha
        # Selected by this evaluation: the pin is promoted.
        promoted = offline_model_pins.resolve_pin(
            store, FAMILY, "BTC", 5, selected=True, now=NOW)
        assert promoted is not None and promoted["verdict"] == "promoted"
        assert offline_model_pins.current_pin(store, FAMILY, "BTC", 5)["model_sha256"] == sha
        # A verdict, once set, is never overwritten by a later call.
        again = offline_model_pins.resolve_pin(
            store, FAMILY, "BTC", 5, selected=False, now=NOW)
        assert again is not None and again["verdict"] == "promoted"
    finally:
        store.close()


def test_resolve_pin_rejects_exhausted_fit(tmp_path, gbm_doc):
    store = _store(tmp_path, name="gbm-resolve.sqlite")
    try:
        model_set = GbmModelSet(store)
        model_set.import_document("file-1", _payload(gbm_doc))
        sha = model_sha256(gbm_doc)
        assert model_set.active("BTC", 5, now=NOW) is not None
        # One settled row whose last issuance is older than the 30-day
        # lookback, and nothing left unsettled: evidence exhausted.
        with store.connect() as conn:
            _insert_shadow_row(
                conn, p=0.7, y=1, sha=sha, family=FAMILY,
                ref=NOW - timedelta(days=40, minutes=30),
                issued=NOW - timedelta(days=40, minutes=25),
                target=NOW - timedelta(days=40, minutes=25))
        rejected = offline_model_pins.resolve_pin(
            store, FAMILY, "BTC", 5, selected=False, now=NOW)
        assert rejected is not None and rejected["verdict"] == "rejected"
        model_set._loaded_at = None
        assert model_set.active("BTC", 5, now=NOW) is None
    finally:
        store.close()


def test_resolve_pin_preserves_collection_time_after_150_windows(tmp_path, gbm_doc):
    store = _store(tmp_path, name="gbm-pin-clock.sqlite")
    try:
        sha = model_sha256(gbm_doc)
        first = NOW - timedelta(days=2)
        offline_model_pins.pin(store, FAMILY, "BTC", 5, sha, now=first)
        with store.connect() as conn:
            for i in range(150):
                issued = first + timedelta(minutes=5 * i)
                _insert_shadow_row(conn, sha=sha, family=FAMILY, issued=issued,
                                   ref=issued - timedelta(minutes=5), target=issued)
        pin = offline_model_pins.resolve_pin(store, FAMILY, "BTC", 5, selected=False, now=NOW)
        assert pin["verdict"] is None
        pin = offline_model_pins.resolve_pin(
            store, FAMILY, "BTC", 5, selected=False, now=first + timedelta(days=31),
        )
        assert pin["verdict"] == "rejected"
    finally:
        store.close()


def test_dry_run_cli_writes_model_and_report(tmp_path):
    """The workflow step's exact command (tools/train_gbm_model.py with
    the same flags as the logistic step) writes a model + report."""
    from parlay_bot.research.gbm_training import main as gbm_train_main
    from tests.test_archive_model import _synthetic_frame

    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    for venue, seed in (("binance", 1), ("bitget", 2)):
        k = _synthetic_frame(17280, seed)
        out = k.reset_index().rename(columns={"index": "time"})
        out.to_csv(frames_dir / f"{venue}__BTC.csv", index=False)
    (tmp_path / "prov.json").write_text(json.dumps(PROV), encoding="utf-8")
    out_dir = tmp_path / "out"
    rc = gbm_train_main(
        [
            "--asset", "BTC",
            "--horizon", "5",
            "--frames-dir", str(frames_dir),
            "--provenance", str(tmp_path / "prov.json"),
            "--train-end", "2025-02-10T00:00:00+00:00",
            "--test-start", "2025-02-20T00:00:00+00:00",
            "--trained-through", "2025-03-01T23:55:00+00:00",
            "--out", str(out_dir),
            "--code-version", "test-sha",
        ]
    )
    assert rc == 0
    assert (out_dir / "gbm-v1-BTC-5m-model.json").exists()
    assert (out_dir / "gbm-v1-BTC-5m-report.json").exists()
    doc = json.loads((out_dir / "gbm-v1-BTC-5m-model.json").read_text())
    assert validate(doc)["family"] == "gbm_v1"


def test_kill_switch_stops_gbm_issuance_in_cycle(tmp_path, monkeypatch, gbm_doc):
    """Flag on: the cycle issues gbm_v1 shadow rows. Flag off: none new.

    Control first, like the archive kill-switch test: with the flag
    forced on, the same cycle issues GBM forecasts; with the flag off --
    its kill-switch state -- the same cycle issues nothing new for
    gbm_v1, while the incumbent cycle still runs.
    """
    assert forward_audit.ISSUE_GBM_V1_CHALLENGERS is True
    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "gbm-cycle.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        GbmModelSet(store).import_document("file-1", _payload(gbm_doc))
        _seed_cycle_candles(market, now=now)

        def _gbm_counts():
            with store.connect() as conn:
                shadow = conn.execute(
                    "SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts "
                    "WHERE family='gbm_v1'"
                ).fetchone()["n"]
                incumbent = conn.execute(
                    "SELECT COUNT(*) AS n FROM perp_forward_audit "
                    "WHERE model_version=?", (FORWARD_MODEL,)
                ).fetchone()["n"]
            return shadow, incumbent

        monkeypatch.setattr(forward_audit, "ISSUE_GBM_V1_CHALLENGERS", True)
        monitor.run_once(now=now)
        shadow_on, incumbent_on = _gbm_counts()
        assert shadow_on >= 1  # the control proves this cycle can issue gbm_v1
        assert incumbent_on >= 1
        monkeypatch.setattr(forward_audit, "ISSUE_GBM_V1_CHALLENGERS", False)
        monitor.run_once(now=now)
        assert _gbm_counts() == (shadow_on, incumbent_on)
    finally:
        store.close()


def test_queue_depth_line_on_status_card(tmp_path, gbm_doc, gbm_doc2):
    store = _store(tmp_path, name="gbm-queue.sqlite")
    try:
        model_set = GbmModelSet(store)
        model_set.import_document("file-1", _payload(gbm_doc))
        # No queue yet: no queue line on the card.
        keys = {key for key, _ in research_alerts(store, now=NOW)}
        assert "offline-queue:gbm_v1:BTC:5m" not in keys
        model_set.import_document("file-2", _payload(gbm_doc2))
        alerts = dict(research_alerts(store, now=NOW))
        assert "offline-queue:gbm_v1:BTC:5m" in alerts
        assert "1 queued fit(s)" in alerts["offline-queue:gbm_v1:BTC:5m"]
    finally:
        store.close()
