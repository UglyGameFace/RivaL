"""PostgreSQL-style cycle diagnostics and honest offline screening proofs."""
from __future__ import annotations

from datetime import UTC, datetime

from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.storage.hot import SQLiteHotStore


def test_boosted_proof_reports_real_candidate_screen_failures(tmp_path):
    store = SQLiteHotStore(tmp_path / "candidate-screening.sqlite")
    market = PerpetualMarketStore(store)
    try:
        monitor = ForwardAccuracyMonitor(market)
        monitor.initialize()
        from parlay_bot.research.candidate_registry import ResearchCandidateRegistry
        ResearchCandidateRegistry(store).initialize()
        now = datetime.now(UTC).isoformat()
        spec = '{"model":"portable-boosted-binary-v1"}'
        with store.connect() as conn:
            for i, reason in enumerate((
                "fewer_than_six_source_months",
                "fewer_than_six_source_months",
                "holdout_brier_gain_below_0.005",
            )):
                sha = f"{i+1:064x}"
                conn.execute(
                    """INSERT INTO rival_offline_candidates
                       (model_sha256,drive_file_id,asset,horizon_minutes,
                        source_months,data_version,holdout_accuracy,holdout_brier,
                        retrospective_screen,admission_reason,first_imported_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (sha, f"file_{i}", "BTC", 5, 2, "version", .49, .27,
                     "insufficient", reason, now),
                )
                conn.execute(
                    """INSERT INTO rival_offline_model_artifacts
                       (model_sha256,model_spec_json,compatible,imported_at)
                       VALUES (?,?,?,?)""",
                    (sha, spec, 1, now),
                )
        receipt = monitor.boosted_production_evidence(asset="BTC")
        assert receipt["portable_imported"] == 3
        assert receipt["portable_screened"] == 0
        assert receipt["issued"] == 0
        assert receipt["screening_blockers"] == [
            {"reason": "fewer_than_six_source_months", "count": 2},
            {"reason": "holdout_brier_gain_below_0.005", "count": 1},
        ]
        assert receipt["actual_fills_verified"] is False
    finally:
        store.close()
