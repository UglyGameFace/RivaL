"""Offline model import is verified, immutable, and never live-promoted."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime

import pytest

from parlay_bot.research.candidate_registry import (
    CandidateValidationError,
    ResearchCandidateRegistry,
    verified_candidate,
)
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore


def artifact(*, source_months=6, score=.57):
    months = {
        f"2026-{month:02d}": hashlib.sha256(str(month).encode()).hexdigest()
        for month in range(1, source_months+1)
    }
    spec = {
        "model": "offline-logistic-multi-horizon-v1",
        "identity": {
            "venue": "binance", "contract": "BTCUSDT",
            "quote": "USDT", "interval_minutes": 5,
        },
        "horizon_minutes": 5,
        "train_cutoff": "2026-05-01T00:00:00+00:00",
        "validation_cutoff": "2026-06-01T00:00:00+00:00",
        "weights": [0.01, 0.02, -0.02, 0.003, 0.05, 0.01],
        "train_base_rate": .5,
        "source_sha256_by_month": months,
    }
    key = hashlib.sha256(json.dumps(
        spec, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    report = {
        "research_only": True, "live_model_promoted": False,
        "model_sha256": key, "model_spec": spec,
        "validation": {
            "count": 200, "brier_gain": .01, "accuracy": score,
            "brier": .24, "baseline_brier": .25, "ece_10": .05,
        },
        "out_of_sample_test": {
            "count": 200, "brier_gain": .012, "accuracy": score,
            "brier": .238, "baseline_brier": .25, "ece_10": .05,
        },
    }
    return json.dumps(report, sort_keys=True).encode()


def test_candidate_sha_coverage_and_live_isolation(tmp_path):
    hot = SQLiteHotStore(tmp_path / "offline.sqlite")
    hot.initialize()
    registry = ResearchCandidateRegistry(hot)
    registry.initialize()
    payload = artifact()
    data = registry.import_verified("driveTest_1", payload)
    registry.import_verified("driveTest_1", payload)  # idempotent restart
    report = registry.summary(asset="BTC", horizon=5)
    assert data["source_months"] == 6
    assert data["retrospective_screen"] == "passed"
    assert report["total"] == 1
    assert report["candidate"]["holdout_accuracy"] == pytest.approx(.57)
    assert report["active_offline_model"] is False
    with hot.connect() as conn:
        row = conn.execute(
            "SELECT active_live FROM rival_offline_candidates"
        ).fetchone()
        assert row["active_live"] == 0
    hot.close()


def test_candidate_rejects_false_promotion_and_corrupted_weights():
    doc = json.loads(artifact())
    doc["live_model_promoted"] = True
    with pytest.raises(CandidateValidationError):
        verified_candidate(json.dumps(doc).encode())
    doc["live_model_promoted"] = False
    doc["model_spec"]["weights"][0] = float("nan")
    with pytest.raises(CandidateValidationError):
        verified_candidate(json.dumps(doc).encode())
    doc = json.loads(artifact())
    doc["model_sha256"] = "0"*64
    with pytest.raises(CandidateValidationError):
        verified_candidate(json.dumps(doc).encode())


def test_partial_data_is_not_approved_even_if_backtest_looks_good():
    found = verified_candidate(artifact(source_months=3, score=.95))
    assert found["retrospective_screen"] == "insufficient"


def test_candidate_import_real_postgresql16():
    dsn = os.getenv("RIVAL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("PostgreSQL CI service required")
    hot = PostgresHotStore(dsn)
    registry = ResearchCandidateRegistry(hot)
    registry.initialize()
    timestamp = datetime(2026, 10, 30, 12, tzinfo=UTC)
    try:
        registry.import_verified("drivePostgres_1", artifact(), now=timestamp)
        row = registry.summary(asset="BTC", horizon=5)
        assert row["candidate"]["first_imported_at"] == timestamp.isoformat()
        assert row["candidate"]["model_sha256"] == verified_candidate(
            artifact(),
        )["model_sha256"]
        assert row["active_offline_model"] is False
    finally:
        with hot.connect() as conn:
            conn.execute(
                "DELETE FROM rival_offline_candidates WHERE drive_file_id=?",
                ("drivePostgres_1",),
            )
        hot.close()

def test_import_receipts_show_real_db_counts_and_screen_reasons(tmp_path, caplog):
    hot = SQLiteHotStore(tmp_path / "receipts.sqlite")
    hot.initialize()
    registry = ResearchCandidateRegistry(hot)
    candidate = json.loads(artifact())
    candidate["model_spec"]["identity"]["venue"] = "binance"
    # Keep a valid model hash; only change retrospective evidence.
    candidate["validation"]["brier_gain"] = .0002
    candidate["out_of_sample_test"]["brier_gain"] = .0001
    payload = json.dumps(candidate).encode()
    detail = verified_candidate(payload)
    assert detail["retrospective_screen"] == "insufficient"
    assert detail["admission_reason"] == "validation_brier_gain_below_0.005"
    with caplog.at_level("INFO"):
        registry.import_verified("driveReceipt_1", payload)
        registry.import_verified("driveReceipt_1", payload)
    assert registry.inventory_receipt() == {
        "total": 1, "passed": 0, "compatible": 0,
    }
    messages = [r.message for r in caplog.records if "verified model imported" in r.message]
    assert len(messages) == 1
    assert detail["model_sha256"] in messages[0]
    assert "retrospective_screen=insufficient" in messages[0]
    hot.close()


def test_offline_screen_distinguishes_missing_months_and_holdout():
    assert verified_candidate(artifact(source_months=3))["admission_reason"] == (
        "fewer_than_six_source_months"
    )
    doc = json.loads(artifact())
    doc["out_of_sample_test"]["brier_gain"] = 0
    assert verified_candidate(json.dumps(doc).encode())["admission_reason"] == (
        "holdout_brier_gain_below_0.005"
    )
