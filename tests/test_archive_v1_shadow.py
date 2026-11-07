"""archive_v1 shadow issuance + Route B wiring (Phase 2, spec §2.8).

Covers: the Drive import validation (provenance, gates, calibration),
shadow issuance through ``issue()`` with the ``archive_`` prefix, generic
settlement copying the incumbent outcome, the ``selected()`` continuity
branch (feature digest, trained-through / graded-count monotone, no
frozen SHA), the ``evaluate()`` artifact recording, and the kill switch
in the 5-minute cycle.
"""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

import pytest

from parlay_bot.perpetuals import forward_audit
from parlay_bot.perpetuals.archive_model import (
    FAMILY,
    ArchiveModelSet,
    archive_calls,
    artifact_identity,
    calibration_status,
    is_archive_document,
    model_sha256,
)
from parlay_bot.perpetuals.family_governance import FamilyPromotion
from parlay_bot.perpetuals.forward_audit import MODEL as FORWARD_MODEL
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.model_competition import ShadowModelCompetition
from parlay_bot.research.archive_features import FEATURE_DIGEST, FEATURES
from parlay_bot.storage.hot import SQLiteHotStore

NOW = datetime(2026, 11, 7, 12, 0, 30, tzinfo=UTC)


def _valid_doc(*, asset="BTC", horizon=5, trained_through=None, weights=None):
    trained_through = trained_through or (NOW - timedelta(days=1))
    weights = (
        weights if weights is not None
        else [0.01 * ((i % 7) - 3) for i in range(33)]
    )
    return {
        "schema": "rival-archive-model-v1",
        "family": "archive_v1",
        "model_version": "archive-offline-v1",
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
        "gates_passed": True,
        "evaluation_status": "held-out evaluated",
        "provenance": {
            "provider_sha256_by_month": {"binance:2025-01": "a" * 64},
            "normalized_sha256_by_month": {"binance:2025-01": "b" * 64},
            "months_without_provider_hash": [],
        },
        "created_at": trained_through.isoformat(),
    }


def _payload(doc) -> bytes:
    return json.dumps(doc).encode()


def _store(tmp_path, name="archive.sqlite"):
    store = SQLiteHotStore(tmp_path / name)
    ShadowModelCompetition(store).initialize()
    return store


def _insert_shadow_row(conn, *, p=0.6, y=1, settled=True, ref=None,
                       issued=None, target=None, family=FAMILY):
    ref = ref or (NOW - timedelta(minutes=30))
    issued = issued or (NOW - timedelta(minutes=25))
    target = target or (NOW - timedelta(minutes=25))
    settled_at = (NOW - timedelta(minutes=10)).isoformat() if settled else None
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
         p, 0.6, 0.45, "a" * 64, settled_at, y if settled else None,
         (p - y) ** 2 if settled else None,
         (0.6 - y) ** 2 if settled else None,
         (0.45 - y) ** 2 if settled else None),
    )


def _promotion_row(store, *, family, evaluated_at, artifact, asset="BTC"):
    FamilyPromotion(store).initialize()
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO rival_family_promotion
               (venue,contract,quote,asset,horizon_minutes,evaluated_at,
                selected_family,status,reason,evidence_json)
               VALUES ('hyperliquid','BTC','USDC',?,5,?,?,'promoted_paper','ok',?)
               ON CONFLICT(venue,contract,quote,horizon_minutes)
               DO UPDATE SET evaluated_at=excluded.evaluated_at,
                selected_family=excluded.selected_family,
                status=excluded.status, reason=excluded.reason,
                evidence_json=excluded.evidence_json""",
            (asset, evaluated_at.isoformat(), family,
             json.dumps({family: {"artifact": artifact}})))


def test_archive_document_import_validates_provenance_and_serves(tmp_path):
    store = _store(tmp_path)
    try:
        doc = _valid_doc()
        payload = _payload(doc)
        assert is_archive_document(payload)
        assert not is_archive_document(b'{"schema": "rival-pooled-model-v1"}')
        model_set = ArchiveModelSet(store)
        assert model_set.import_document("file-1", payload) == 1
        assert model_set.import_document("file-1", payload) == 0
        assert model_set.imported_file_ids() == {"file-1"}
        active = model_set.active("BTC", 5, now=NOW)
        assert active is not None and active["asset"] == "BTC"
        assert active["horizon_minutes"] == 5
        assert model_set.active("ETH", 5, now=NOW) is None
        assert model_set.serving("BTC", 5, now=NOW) == [active]
        # Calibration record for receipts comes from the artifact itself.
        status = calibration_status(store, "BTC", 5)
        assert status["status"] == "evaluated" and status["a"] == 1.0
        assert calibration_status(store, "ETH", 5)["status"] == "unevaluated"
        # A provider hash that IS the normalized hash is refused (P1-4).
        bad = _valid_doc()
        bad["provenance"]["normalized_sha256_by_month"] = {
            "binance:2025-01": "a" * 64}
        with pytest.raises(ValueError):
            ArchiveModelSet(store).import_document("file-bad", _payload(bad))
        # A wrong feature digest is refused.
        bad = _valid_doc()
        bad["feature_digest"] = "0" * 64
        with pytest.raises(ValueError):
            ArchiveModelSet(store).import_document("file-bad2", _payload(bad))
        # Gates not passed is refused.
        bad = _valid_doc()
        bad["gates_passed"] = False
        with pytest.raises(ValueError):
            ArchiveModelSet(store).import_document("file-bad3", _payload(bad))
    finally:
        store.close()


def test_archive_calls_skip_without_artifact_or_vector(tmp_path):
    store = _store(tmp_path)
    try:
        model_set = ArchiveModelSet(store)
        vector = ([0.0] * 32, (1.0,) * 7)
        assert archive_calls(model_set, asset="BTC", horizon=5,
                             vector=vector, now=NOW) == []
        assert archive_calls(None, asset="BTC", horizon=5,
                             vector=vector, now=NOW) == []
        doc = _valid_doc()
        model_set.import_document("file-1", _payload(doc))
        # All-zero flags: no issuance that cycle (the flow_v3 skip rule).
        assert archive_calls(model_set, asset="BTC", horizon=5,
                             vector=([0.0] * 32, (0.0,) * 7), now=NOW) == []
        assert archive_calls(model_set, asset="BTC", horizon=5,
                             vector=None, now=NOW) == []
        calls = archive_calls(model_set, asset="BTC", horizon=5,
                              vector=vector, now=NOW)
        assert len(calls) == 1
        call = calls[0]
        assert call["family"] == "archive_v1"
        assert 0.02 <= call["probability"] <= 0.98
        assert call["training_rows"] == 1000
        assert call["model_sha256"] == model_sha256(doc)
        # An all-NaN vector (every group absent but flags set by the
        # caller) still scores at the group means, never NaN.
        nan_calls = archive_calls(
            model_set, asset="BTC", horizon=5,
            vector=([float("nan")] * 32, (1.0,) * 7), now=NOW)
        assert len(nan_calls) == 1
        assert math.isfinite(nan_calls[0]["probability"])
    finally:
        store.close()


def test_issue_accepts_archive_challenger_rows(tmp_path):
    store = _store(tmp_path)
    try:
        market = PerpetualMarketStore(store)
        ForwardAccuracyMonitor(market).initialize()
        comp = ShadowModelCompetition(store)
        opened = NOW - timedelta(minutes=5)
        target = NOW + timedelta(minutes=5)
        issued = NOW - timedelta(seconds=20)
        doc = _valid_doc()
        sha = model_sha256(doc)
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
                challengers=[{"family": "archive_v1", "probability": 0.7,
                              "training_rows": 1000, "model_sha256": sha}],
            )
            assert saved == 1
            row = conn.execute(
                """SELECT family,probability_up,training_count,training_sha256
                   FROM rival_model_shadow_forecasts
                   WHERE family='archive_v1'"""
            ).fetchone()
            assert row["family"] == "archive_v1"
            assert row["probability_up"] == 0.7
            assert row["training_count"] == 1000
            assert row["training_sha256"] == sha
            # A family outside the challenger prefixes is not issued.
            assert comp.issue(
                conn, samples=[], features=(.25,) * 10, now=NOW,
                venue="hyperliquid", contract="BTC", quote="USDC", asset="BTC",
                horizon=5, reference_opened_at=opened,
                target_opened_at=target, issued_at=issued,
                incumbent_up=.6, baseline_up=.45,
                challengers=[{"family": "weird_v1", "probability": 0.7,
                              "training_rows": 1, "model_sha256": sha}],
            ) == 0
    finally:
        store.close()


def test_shadow_rows_settle_from_incumbent_outcome(tmp_path):
    store = _store(tmp_path)
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
        doc = _valid_doc()
        sha = model_sha256(doc)
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
                challengers=[{"family": "archive_v1", "probability": 0.7,
                              "training_rows": 1000, "model_sha256": sha}],
            ) == 1
        assert comp.settle(now=NOW) == 1
        with store.connect() as conn:
            row = conn.execute(
                """SELECT settled_at,outcome_up,model_brier
                   FROM rival_model_shadow_forecasts
                   WHERE family='archive_v1'"""
            ).fetchone()
            assert row["settled_at"] == settled.isoformat()
            assert row["outcome_up"] == 1
            assert row["model_brier"] == pytest.approx((0.7 - 1) ** 2)
    finally:
        store.close()


def test_selected_continuity_branch(tmp_path):
    store = _store(tmp_path)
    try:
        model_set = ArchiveModelSet(store)
        doc1 = _valid_doc()
        model_set.import_document("file-1", _payload(doc1))
        identity1 = artifact_identity(store, "BTC", 5)
        assert identity1 is not None
        assert identity1["feature_digest"] == FEATURE_DIGEST
        assert identity1["graded_count"] == 0
        assert identity1["trained_through"] == doc1["trained_through"]
        _promotion_row(store, family="archive_v1", evaluated_at=NOW,
                       artifact=identity1)
        promoter = FamilyPromotion(store)
        kwargs = {"asset": "BTC", "venue": "hyperliquid", "contract": "BTC",
                  "quote": "USDC", "horizon": 5, "now": NOW}
        assert promoter.selected(**kwargs) == "archive_v1"
        # A mismatched weights SHA with no further graded learning: not
        # the artifact that earned promotion.
        tampered = dict(identity1, weights_sha256="0" * 64)
        _promotion_row(store, family="archive_v1", evaluated_at=NOW,
                       artifact=tampered)
        assert promoter.selected(**kwargs) is None
        # Stale promotion (older than 48h) fails safe to the incumbent.
        _promotion_row(store, family="archive_v1",
                       evaluated_at=NOW - timedelta(hours=49),
                       artifact=identity1)
        assert promoter.selected(**kwargs) is None
        _promotion_row(store, family="archive_v1", evaluated_at=NOW,
                       artifact=identity1)
        assert promoter.selected(**kwargs) == "archive_v1"
        # One graded row, then a retrain: new weights + later
        # trained-through, graded count advanced -- the retrain serves.
        with store.connect() as conn:
            _insert_shadow_row(conn, p=0.7, y=1)
        doc2 = _valid_doc(
            trained_through=NOW - timedelta(hours=1),
            weights=[0.02 * ((i % 5) - 2) for i in range(33)],
        )
        assert model_sha256(doc2) != model_sha256(doc1)
        model_set.import_document("file-2", _payload(doc2))
        identity2 = artifact_identity(store, "BTC", 5)
        assert identity2["graded_count"] == 1
        assert identity2["weights_sha256"] != identity1["weights_sha256"]
        assert promoter.selected(**kwargs) == "archive_v1"
        # A reset (graded rows gone, count back to 0) with the new weights
        # never serves on the name alone.
        with store.connect() as conn:
            conn.execute(
                "DELETE FROM rival_model_shadow_forecasts WHERE family='archive_v1'")
        assert artifact_identity(store, "BTC", 5)["graded_count"] == 0
        assert promoter.selected(**kwargs) is None
        # No imported artifact at all: no identity, no serve.
        with store.connect() as conn:
            conn.execute("DELETE FROM rival_archive_models")
        model_set._loaded_at = None
        assert artifact_identity(store, "BTC", 5) is None
        assert promoter.selected(**kwargs) is None
    finally:
        store.close()


def test_evaluate_records_archive_artifact(tmp_path):
    store = _store(tmp_path)
    try:
        model_set = ArchiveModelSet(store)
        doc = _valid_doc()
        model_set.import_document("file-1", _payload(doc))
        with store.connect() as conn:
            _insert_shadow_row(conn, p=0.7, y=1)
            _insert_shadow_row(conn, p=0.4, y=0,
                               ref=NOW - timedelta(minutes=60),
                               issued=NOW - timedelta(minutes=55),
                               target=NOW - timedelta(minutes=55))
        result = FamilyPromotion(store).evaluate(
            asset="BTC", venue="hyperliquid", contract="BTC", quote="USDC",
            horizon=5, now=NOW)
        assert "archive_v1" in result["families"]
        recorded = result["families"]["archive_v1"]["artifact"]
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


def _seed_cycle_candles(market, *, now, bars=500):
    """500 live 5m bars on hyperliquid/BTC (archive needs 420 contiguous)."""
    boundary = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0)
    market.initialize()
    with market.store.connect() as conn:
        for i in range(bars):
            opened = boundary - timedelta(minutes=5 * (bars - i))
            closed = opened + timedelta(minutes=5, milliseconds=-1)
            observed = closed + timedelta(milliseconds=1)
            p = 100 + i / 15 + (i % 9) / 8
            conn.execute(
                """INSERT INTO perp_market_candles
                   (venue,contract,quote,interval_minutes,opened_at,closed_at,
                    first_observed_at,open_price,high_price,low_price,close_price,
                    volume_base,trades)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "hyperliquid", "BTC", "USDC", 5,
                    opened.isoformat(), closed.isoformat(), observed.isoformat(),
                    p, p + 2, p - 2, p + 0.5, 10, 12,
                ),
            )


def test_kill_switch_stops_archive_issuance_in_cycle(tmp_path, monkeypatch):
    """Flag on: the cycle issues archive_v1 shadow rows. Flag off: none new.

    Control first, like the flow_v3 retirement test: with the flag forced
    on, the same cycle issues archive forecasts; with the flag off -- its
    kill-switch state -- the same cycle issues nothing new for archive_v1,
    while the incumbent cycle still runs.
    """
    assert forward_audit.ISSUE_ARCHIVE_V1_CHALLENGERS is True
    now = datetime.now(UTC)
    store = SQLiteHotStore(tmp_path / "archive-cycle.sqlite")
    market = PerpetualMarketStore(store)
    monitor = ForwardAccuracyMonitor(market)
    try:
        ArchiveModelSet(store).import_document(
            "file-1", _payload(_valid_doc(trained_through=now - timedelta(days=1))))
        _seed_cycle_candles(market, now=now)

        def _archive_counts():
            with store.connect() as conn:
                shadow = conn.execute(
                    "SELECT COUNT(*) AS n FROM rival_model_shadow_forecasts "
                    "WHERE family='archive_v1'"
                ).fetchone()["n"]
                incumbent = conn.execute(
                    "SELECT COUNT(*) AS n FROM perp_forward_audit "
                    "WHERE model_version=?", (FORWARD_MODEL,)
                ).fetchone()["n"]
            return shadow, incumbent

        monkeypatch.setattr(forward_audit, "ISSUE_ARCHIVE_V1_CHALLENGERS", True)
        monitor.run_once(now=now)
        shadow_on, incumbent_on = _archive_counts()
        assert shadow_on >= 1  # the control proves this cycle can issue archive_v1
        assert incumbent_on >= 1
        monkeypatch.setattr(forward_audit, "ISSUE_ARCHIVE_V1_CHALLENGERS", False)
        monitor.run_once(now=now)
        assert _archive_counts() == (shadow_on, incumbent_on)
    finally:
        store.close()
