"""Retrospective rejection provenance and exact source-native selection."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from parlay_bot.research.candidate_registry import ResearchCandidateRegistry
from parlay_bot.storage.hot import SQLiteHotStore


def artifact():
    months = {
        f"2026-{month:02d}": hashlib.sha256(str(month).encode()).hexdigest()
        for month in range(1, 7)
    }
    spec = {
        "model": "offline-logistic-multi-horizon-v1",
        "identity": {
            "venue": "binance", "contract": "BTCUSDT", "quote": "USDT",
            "interval_minutes": 5,
        },
        "horizon_minutes": 5,
        "train_cutoff": "2026-05-01T00:00:00+00:00",
        "validation_cutoff": "2026-06-01T00:00:00+00:00",
        "weights": [0.01, 0.02, -0.02, 0.003, 0.05, 0.01],
        "train_base_rate": 0.5,
        "source_sha256_by_month": months,
    }
    sha = hashlib.sha256(json.dumps(
        spec, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return json.dumps({
        "research_only": True,
        "live_model_promoted": False,
        "model_sha256": sha,
        "model_spec": spec,
        "validation": {
            "count": 200, "brier_gain": 0.01, "accuracy": 0.57,
            "brier": 0.24, "baseline_brier": 0.25, "ece_10": 0.05,
        },
        "out_of_sample_test": {
            "count": 200, "brier_gain": 0.012, "accuracy": 0.57,
            "brier": 0.238, "baseline_brier": 0.25, "ece_10": 0.05,
        },
    }, sort_keys=True).encode()


def test_rejected_reason_is_durable_across_restart(tmp_path):
    path = tmp_path / "admission.sqlite"
    store = SQLiteHotStore(path)
    store.initialize()
    doc = json.loads(artifact())
    doc["validation"]["brier_gain"] = 0.0002
    doc["out_of_sample_test"]["brier_gain"] = 0.0001
    registry = ResearchCandidateRegistry(store)
    result = registry.import_verified("sameDriveFile", json.dumps(doc).encode())
    assert result["retrospective_screen"] == "insufficient"
    assert registry.summary(asset="BTC", horizon=5)["admission_reasons"] == [
        {"reason": "validation_brier_gain_below_0.005", "count": 1},
    ]
    store.close()

    store = SQLiteHotStore(path)
    registry = ResearchCandidateRegistry(store)
    detail = registry.summary(asset="BTC", horizon=5)
    assert detail["candidate"]["admission_reason"] == (
        "validation_brier_gain_below_0.005"
    )
    assert detail["slot_passed"] == 0
    assert not detail["active_offline_model"]
    store.close()


def test_legacy_reason_only_backfills_from_original_file(tmp_path):
    store = SQLiteHotStore(tmp_path / "legacy.sqlite")
    store.initialize()
    registry = ResearchCandidateRegistry(store)
    original = artifact()
    candidate = registry.import_verified("origId", original)
    with store.connect() as con:
        con.execute(
            "UPDATE rival_offline_candidates SET admission_reason=NULL "
            "WHERE model_sha256=?", (candidate["model_sha256"],),
        )
    registry.import_verified("differentId", original)
    report = registry.summary(asset="BTC", horizon=5)
    assert report["admission_reasons"] == [
        {"reason": "legacy_reason_unreported", "count": 1},
    ]
    registry.import_verified("origId", original)
    report = registry.summary(asset="BTC", horizon=5)
    assert report["admission_reasons"] == [
        {"reason": "retrospective_pass", "count": 1},
    ]
    assert report["slot_compatible"] == 0  # Legacy Binance inputs stay isolated.
    store.close()


def test_old_sqlite_schema_adds_explanation_without_replacing_rows(tmp_path):
    store = SQLiteHotStore(tmp_path / "old.sqlite")
    store.initialize()
    with store.connect() as con:
        con.execute(
            "CREATE TABLE rival_offline_candidates ("
            "model_sha256 TEXT PRIMARY KEY, drive_file_id TEXT NOT NULL, "
            "asset TEXT NOT NULL, horizon_minutes INTEGER NOT NULL, "
            "source_months INTEGER NOT NULL, data_version TEXT NOT NULL, "
            "holdout_accuracy REAL, holdout_brier REAL, "
            "retrospective_screen TEXT NOT NULL, "
            "first_imported_at TEXT NOT NULL, "
            "active_live INTEGER NOT NULL DEFAULT 0)"
        )
    registry = ResearchCandidateRegistry(store)
    candidate = registry.import_verified("legacyId", artifact())
    with store.connect() as con:
        assert "admission_reason" in store.database.column_names(
            con, "rival_offline_candidates"
        )
        row = con.execute(
            "SELECT retrospective_screen,admission_reason,active_live "
            "FROM rival_offline_candidates WHERE model_sha256=?",
            (candidate["model_sha256"],),
        ).fetchone()
        assert row["retrospective_screen"] == "passed"
        assert row["admission_reason"] == "retrospective_pass"
        assert row["active_live"] == 0
    store.close()


def test_native_contract_selection_is_visible_but_not_approved(tmp_path):
    store = SQLiteHotStore(tmp_path / "selection.sqlite")
    store.initialize()
    registry = ResearchCandidateRegistry(store)
    registry.import_verified("original", artifact())
    with store.connect() as con:
        con.execute(
            "INSERT INTO rival_history_selection "
            "(venue,contract,quote,horizon_minutes,evaluated_at,"
            "selected_sha256,reason,evidence_json) VALUES (?,?,?,?,?,?,?,?)",
            ("okx", "BTC-USDT-SWAP", "USDT", 5,
             datetime.now(UTC).isoformat(), None,
             "Insufficient future evidence", "{}"),
        )
    summary = registry.summary(asset="BTC", horizon=5, live_venues={"okx"})
    assert summary["historical_selection"]["contract"] == "BTC-USDT-SWAP"
    assert summary["reason"] == "Insufficient future evidence"
    assert summary["active_offline_model"] is False
    store.close()
