"""Tests for the public-safe RivaL evidence publisher (no network or secrets)."""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from evidence_board_github import (
    PINNED_PRS,
    read_github_snapshot,
)
from evidence_board_receipt import (
    _artifact_prefix,
    _check_receipt_size,
    _collection_block,
    _nonnegative,
    _paper_proof,
    _recent_forecast_summary,
    _recent_receipt_is_fresh,
    _research_loop,
    _submit_signed_receipt,
    _xmarket_quotes,
    _xmarket_watchdog,
    build_source_details,
    classify_read_error,
    publish,
    read_drive,
    summarize_host,
    summarize_xmarket_report,
)


class ReceiptTests(unittest.TestCase):
    def test_latest_research_calls_strip_private_fields(self) -> None:
        source = {
            "status": "observed", "captured_at": "2026-11-15T00:00:00+00:00",
            "window_hours": 6, "not_trading_calls": True,
            "rows": [{
                "asset": "BTC", "horizon": 5, "venue": "okx",
                "quote": "USDT", "issued_at": "2026-11-14T23:57:00+00:00",
                "target_opened_at": "2026-11-14T23:55:00+00:00",
                "target_close_at": "2026-11-15T00:00:00+00:00",
                "settled_at": "2026-11-15T00:00:30+00:00",
                "probability_up": 0.61, "baseline_up": 0.54,
                "lead_seconds": 180.0, "direction": "UP", "status": "settled",
                "outcome_up": 1, "model_version": "prospective-logistic-persistent-v3",
                "weights_sha256": "PRIVATE", "contract": "PRIVATE",
                "api_token": "PRIVATE",
            }],
        }
        clean = _recent_forecast_summary(source)
        self.assertEqual(clean["status"], "observed")
        self.assertEqual(len(clean["rows"]), 1)
        self.assertEqual(clean["rows"][0]["probability_up"], 0.61)
        self.assertNotIn("PRIVATE", str(clean))
        host, _ = summarize_host({"recent_forward_calls": source})
        self.assertEqual(host["recent_forward_calls"]["rows"][0]["direction"], "UP")
        self.assertEqual(
            summarize_host(None)[0]["recent_forward_calls"]["status"],
            "read_unavailable"
        )
        bad = {**source, "rows": [{**source["rows"][0], "probability_up": 1.4}]}
        self.assertEqual(_recent_forecast_summary(bad)["status"], "read_unavailable")
        self.assertEqual(_recent_forecast_summary(bad)["rows"], [])

    def test_kucoin_gap_status_is_public_safe_and_never_claims_recovered(self) -> None:
        private = {"status": "recorded", "assets": [
            {"asset": "ETH", "queued_exact_gaps_14d": 2,
             "due_exact_gaps_14d": 1, "attempted_exact_gaps_14d": 1,
             "bulk_status": "upstream_empty", "contract": "PRIVATE"}
        ]}
        host, _ = summarize_host({"path_targets": {
            "kucoin_gap_recovery": private,
        }})
        status = host["kucoin_gap_recovery"]
        self.assertEqual(status, {
            "status": "recorded", "assets_observed": 1,
            "queued_exact_gaps_14d": 2, "due_exact_gaps_14d": 1,
            "attempted_exact_gaps_14d": 1, "bulk_upstream_empty_assets": 1,
        })
        self.assertNotIn("PRIVATE", str(status))
        bad, _ = summarize_host({"path_targets": {
            "kucoin_gap_recovery": {"status": "recorded", "assets": [
                {**private["assets"][0], "due_exact_gaps_14d": -1},
            ]},
        }})
        self.assertEqual(bad["kucoin_gap_recovery"]["status"], "unavailable")
        unknown, _ = summarize_host(None)
        self.assertIsNone(unknown["kucoin_gap_recovery"]["queued_exact_gaps_14d"])

    def test_pending_backlog_publishes_counts_and_unique_targets(self) -> None:
        host, _ = summarize_host({"path_targets": {
            "pending_paths_14d": 5, "due_pending_paths_14d": 4,
            "unique_due_targets_14d": 2,
        }})
        self.assertEqual(host["pending_backlog"], {
            "status": "recorded", "pending_paths_14d": 5,
            "due_pending_paths_14d": 4, "unique_due_targets_14d": 2,
        })
        bad, _ = summarize_host({"path_targets": {
            "pending_paths_14d": 5, "due_pending_paths_14d": -1,
            "unique_due_targets_14d": 2,
        }})
        self.assertEqual(bad["pending_backlog"]["status"], "unavailable")
        missing, _ = summarize_host({"path_targets": {
            "pending_paths_14d": 5, "due_pending_paths_14d": 4,
        }})
        self.assertEqual(missing["pending_backlog"]["status"], "unavailable")
        unknown, _ = summarize_host(None)
        self.assertIsNone(unknown["pending_backlog"]["pending_paths_14d"])
        self.assertIsNone(
            unknown["pending_backlog"]["unique_due_targets_14d"])

    def test_cycle_diagnostics_publishes_24h_means_and_latest_row(self) -> None:
        diag = {
            "status": "recorded", "cycles_24h": 288,
            "mean_selected_streams_24h": 33.6,
            "mean_target_expired_24h": 0.5,
            "mean_duplicate_forecasts_24h": 1.0,
            "mean_insufficient_training_24h": 1.5,
            "mean_stale_last_candle_24h": 0.5,
            "mean_short_history_24h": 1.0,
            "latest_selected_streams": 34, "latest_target_expired": 0,
            "latest_duplicate_forecasts": 0, "latest_insufficient_training": 3,
            "latest_stale_last_candle": 0, "latest_short_history": 2,
            "latest_issued_forecasts": 96, "contract": "PRIVATE",
        }
        host, _ = summarize_host({"path_targets": {"cycle_diagnostics": diag}})
        self.assertEqual(host["cycle_diagnostics"], {
            "status": "recorded", "cycles_24h": 288,
            "mean_selected_streams_24h": 33.6,
            "mean_target_expired_24h": 0.5,
            "mean_duplicate_forecasts_24h": 1.0,
            "mean_insufficient_training_24h": 1.5,
            "mean_stale_last_candle_24h": 0.5,
            "mean_short_history_24h": 1.0,
            "latest_selected_streams": 34, "latest_target_expired": 0,
            "latest_duplicate_forecasts": 0, "latest_insufficient_training": 3,
            "latest_stale_last_candle": 0, "latest_short_history": 2,
            "latest_issued_forecasts": 96,
        })
        self.assertNotIn("PRIVATE", str(host["cycle_diagnostics"]))
        bad, _ = summarize_host({"path_targets": {"cycle_diagnostics": {
            **diag, "mean_selected_streams_24h": -1,
        }}})
        self.assertEqual(bad["cycle_diagnostics"]["status"], "unavailable")
        missing, _ = summarize_host({"path_targets": {"cycle_diagnostics": {
            **diag, "latest_selected_streams": None,
        }}})
        self.assertEqual(missing["cycle_diagnostics"]["status"], "unavailable")
        unknown, _ = summarize_host(None)
        self.assertIsNone(unknown["cycle_diagnostics"]["cycles_24h"])

    def test_live_call_delivery_shows_exact_opt_in_and_model_without_ids(self) -> None:
        host, _ = summarize_host({"advance_forecasts": {
            "channel_cards": {"status": "recorded", "active": 0,
                              "guild_id": "PRIVATE"},
            "recent_deliveries": [],
            "model": "advance-candle-logistic-lead5-v1",
        }})
        self.assertEqual(host["live_call_delivery"], {
            "status": "recorded", "active_channels": 0,
            "recent_accepted_delivery_rows": 0,
            "advance_model": "advance-candle-logistic-lead5-v1",
        })
        self.assertNotIn("PRIVATE", str(host["live_call_delivery"]))
        bad, _ = summarize_host({"advance_forecasts": {
            "channel_cards": {"status": "recorded", "active": -1},
            "recent_deliveries": [], "model": "unsafe",
        }})
        self.assertEqual(bad["live_call_delivery"]["status"], "unavailable")

    def test_missing_host_is_unknown_not_zero(self) -> None:
        host, db = summarize_host(None)
        self.assertEqual(host["status"], "unavailable")
        self.assertIsNone(host["xmarket_imported_scopes"])
        self.assertIsNone(host["xmarket_shadow_rows"])
        self.assertIsNone(db["qualified_paper_scopes"])
        self.assertIsNone(db["weight_updates_observed"])
        self.assertEqual(host["xmarket_stage"], "read_unavailable")
        self.assertIsNone(host["xmarket_exports"])

    def test_export_chain_preserves_known_zero_and_discards_private_ids(self) -> None:
        host, _ = summarize_host({"xmarket": {
            "status": "waiting_for_completed_day_export", "export_coverage": {
                "published": [{"asset": "BTC", "exported_days": 1,
                               "latest_exported_day": "2026-11-12", "drive_file_id": "PRIVATE"}],
                "pending_by_asset": [{"asset": "ETH", "days": 1}],
                "recent_completed_days_pending": 1,
                "eligible_before": "2026-11-13T00:00:00+00:00",
                "database_password": "PRIVATE",
            }}})
        self.assertEqual(host["xmarket_stage"], "waiting_for_completed_day_export")
        exports = host["xmarket_exports"]
        self.assertEqual(exports["recent_completed_days_pending"], 1)
        self.assertEqual(exports["published"][0]["latest_exported_day"], "2026-11-12")
        self.assertEqual(exports["pending_by_asset"], [{"asset": "ETH", "days": 1}])
        self.assertNotIn("PRIVATE", str(host))
        self.assertTrue(exports["does_not_prove_independent_drive_restore"])

    def test_host_scope_progress_is_bounded_and_never_exports_model_documents(self) -> None:
        samples = [
            {"asset": "BTC", "horizon_minutes": 15, "status": "shadow_issuing",
             "stored_gate_passing_artifacts": 2, "shadow_rows_24h": 97,
             "latest_shadow_at": "2026-11-16T12:00:00+00:00",
             "latest_imported_at": "2026-11-15T12:00:00+00:00",
             "fresh_native_sources": 2, "expected_native_sources": 2,
             "missing_native_sources": [], "stale_native_sources": [],
             "model_document": {"private": "NEVER_PUBLISH"},
             "drive_file_id": "NEVER_PUBLISH"},
            {"asset": "ETH", "horizon_minutes": 5, "status": "waiting_for_fit",
             "stored_gate_passing_artifacts": 0, "shadow_rows_24h": 0,
             "fresh_native_sources": 1,
             "missing_native_sources": ["kalshi", "PRIVATE"],
             "stale_native_sources": ["polymarket"]},
            {"asset": "DOGE", "horizon_minutes": 5, "status": "shadow_issuing",
             "discord_user_id": "NEVER_PUBLISH"}
        ]
        host, _ = summarize_host({"xmarket": {
            "status": "shadow_issuing", "scope_progress": samples}})
        rows = host["xmarket_scope_progress"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["asset"], "BTC")
        self.assertEqual(rows[0]["horizon"], 15)
        self.assertEqual(rows[0]["shadow_rows_24h"], 97)
        self.assertEqual(rows[1]["asset"], "ETH")
        self.assertEqual(rows[1]["stored_artifacts"], 0)
        self.assertEqual(rows[1]["missing_native_sources"], ["kalshi"])
        self.assertFalse(rows[0]["proves_live_edge"])
        self.assertNotIn("NEVER_PUBLISH", str(rows))
        self.assertNotIn("PRIVATE", str(rows))
        self.assertIsNone(summarize_host(None)[0]["xmarket_scope_progress"])
        self.assertIsNone(summarize_host({"xmarket": {
            "status": "waiting_for_fit"}})[0]["xmarket_scope_progress"])

    def test_source_and_fit_failures_remain_actionable(self) -> None:
        doc = {"schema": "rival-xmarket-model-v1", "asset": "BTC",
               "horizon_minutes": 5, "training_status": "blocked", "data_checks": {
                   "polymarket": {"status": "unverified", "blocked_reason": "insufficient_exact_pairs"},
                   "kalshi": {"status": "verified"}}}
        self.assertEqual(summarize_xmarket_report(doc, None)["reason"], "insufficient_exact_pairs")
        doc["data_checks"] = {}
        doc["gate_failures"] = ["split too small: train=0 val=0 test=12"]
        self.assertEqual(summarize_xmarket_report(doc, None)["reason"], "insufficient_training_split")
        doc["evaluation_status"] = "held-out evaluated"
        doc["gates_passed"] = False
        self.assertEqual(summarize_xmarket_report(doc, None)["status"], "rejected")
        doc["gates_passed"] = True
        result = summarize_xmarket_report(doc, None)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["reason"], "offline_gates_passed")
        self.assertNotIn("after_cost_qualified", result)

    def test_real_fitted_model_identity_never_fabricated_for_blocked_report(self) -> None:
        doc = {"schema": "rival-xmarket-model-v1",
               "family": "xmarket_v1", "asset": "ETH",
               "horizon_minutes": 15, "training_status": "blocked",
               "model_version": "xmarket-offline-v1"}
        blocked = summarize_xmarket_report(doc, None)
        self.assertEqual(blocked["family"], "xmarket_v1")
        self.assertIsNone(blocked["model_version"])
        doc["training_status"] = "fitted"
        fitted = summarize_xmarket_report(doc, None)
        self.assertEqual(fitted["model_version"], "xmarket-offline-v1")
        doc["model_version"] = "PRIVATE<script>"
        self.assertIsNone(summarize_xmarket_report(doc, None)["model_version"])
        doc["model_version"] = "xmarket-offline-v1"
        doc["family"] = "unknown"
        self.assertIsNone(summarize_xmarket_report(doc, None)["model_version"])

    def test_recent_online_weight_change_is_not_prediction_edge(self) -> None:
        host, db = summarize_host({
            "outcome": "verified",
            "forecast": {"status": "fresh", "issued": 105},
            "learning": {
                "status": "fresh", "settled": 101,
                "math_ok": True, "model_review_ok": True,
            },
            "xmarket": {
                "status": "waiting_for_fit", "shadow_rows_24h": 0,
                "imported_scopes": [],
                "source_coverage": {"fresh_scopes": 8, "expected_scopes": 8},
            },
            "actual_learning": {
                "status": "weight_updates_observed",
                "streams_with_verified_weight_updates": 102,
                "streams_with_before_after": 102,
            },
            "forward_proof": {
                "status": "recorded",
                "qualified_paper_scopes": 0, "evaluated_scopes": 12,
            },
        })
        self.assertEqual(host["status"], "verified")
        self.assertEqual(host["xmarket_imported_scopes"], 0)
        self.assertEqual(host["xmarket_shadow_rows"], 0)
        self.assertEqual(host["xmarket_fresh_inputs"], 8)
        self.assertEqual(db["status"], "verified_read")
        self.assertEqual(db["weight_updates_observed"], 102)
        self.assertEqual(db["qualified_paper_scopes"], 0)
        self.assertNotIn("after_cost_qualified", db)
        self.assertNotIn("secrets", db)

    def test_stale_or_failed_math_is_not_healthy(self) -> None:
        host, _ = summarize_host({
            "outcome": "verified",
            "forecast": {"status": "fresh"},
            "learning": {"status": "fresh", "math_ok": False,
                         "model_review_ok": True},
        })
        self.assertEqual(host["status"], "unverified")

    def test_raw_report_is_reduced_to_only_bounded_aggregates(self) -> None:
        doc = {
            "schema": "rival-xmarket-model-v1", "asset": "BTC",
            "horizon_minutes": 5, "training_status": "blocked",
            "gates_passed": False,
            "data_checks": {
                "polymarket": {
                    "status": "unverified", "paired_points": 0,
                    "blocked_reason": "no_prospective_export",
                    "token": "private-should-not-escape",
                },
                "kalshi": {"status": "unverified", "paired_points": 0},
            },
            "effective_window": {"rows": 0}, "weights": [0.1],
            "database_password": "not-for-site",
        }
        public = summarize_xmarket_report(doc, "2026-11-12T15:00:00Z")
        self.assertEqual(public["status"], "blocked")
        self.assertEqual(public["pm_pairs"], 0)
        self.assertFalse(public["gate_passed"])
        self.assertEqual(public["reason"], "no_prospective_export")
        self.assertNotIn("weights", str(public))
        self.assertNotIn("private-should-not-escape", str(public))
        self.assertNotIn("database_password", str(public))

    def test_invalid_report_identity_is_not_verified(self) -> None:
        self.assertIsNone(summarize_xmarket_report(
            {"schema": "unrelated", "asset": "BTC", "horizon_minutes": 5},
            None,
        ))
        self.assertIsNone(summarize_xmarket_report(
            {"schema": "rival-xmarket-model-v1", "asset": "ABC",
             "horizon_minutes": 5},
            None,
        ))

    def test_invalid_numbers_do_not_become_zero(self) -> None:
        self.assertIsNone(_nonnegative(None))
        self.assertIsNone(_nonnegative(-1))
        self.assertIsNone(_nonnegative(True))
        self.assertEqual(_nonnegative(0), 0)

    def test_forward_history_is_bounded_per_scope_with_target_clock(self) -> None:
        base = {
            "asset": "BTC", "horizon": 5, "venue": "okx", "quote": "USDT",
            "probability_up": 0.61, "baseline_up": 0.54, "direction": "UP",
            "status": "open", "outcome_up": None,
        }
        rows = []
        for minute in range(6):
            issued = f"2026-11-14T23:{50 + minute:02d}:00+00:00"
            rows.append({**base, "issued_at": issued,
                         "target_opened_at": "2026-11-14T23:55:00+00:00",
                         "target_close_at": "2026-11-15T00:00:00+00:00",
                         "settled_at": None, "lead_seconds": 300.0})
        source = {"status": "observed", "captured_at": "2026-11-14T23:59:00+00:00",
                  "window_hours": 6, "rows": rows}
        clean = _recent_forecast_summary(source)
        self.assertEqual(len(clean["rows"]), 3)
        self.assertEqual(clean["rows_per_scope"], 3)
        self.assertEqual(clean["rows"][0]["target_opened_at"],
                         "2026-11-14T23:55:00+00:00")
        self.assertIsNone(clean["rows"][0]["settled_at"])
        # A row without the target-opened clock cannot be published.
        missing = {**rows[0]}
        del missing["target_opened_at"]
        self.assertEqual(
            _recent_forecast_summary({**source, "rows": [missing]})["rows"], [])
        # A settlement before the target close is not a settlement.
        early = {**rows[0], "status": "settled", "outcome_up": 1,
                 "settled_at": "2026-11-14T23:59:00+00:00"}
        self.assertEqual(
            _recent_forecast_summary({**source, "rows": [early]})["rows"], [])

    def test_model_artifacts_identify_serving_model_without_weights(self) -> None:
        host, _ = summarize_host({
            "forecast": {"status": "fresh"},
            "actual_learning": {
                "status": "weight_updates_observed",
                "model_version": "prospective-logistic-persistent-v3",
                "latest_weights_sha256_prefix": "abcdef012345",
                "latest_seed_sha256_prefix": "0123456789ab",
                "latest_online_training_updates": 84,
                "sampled_forecasts": 12, "sampled_model_streams": 3,
                "streams_with_verified_weight_updates": 2,
                "lab": {"status": "recorded", "sampled_streams": 4,
                        "streams_with_training_updates": 2,
                        "latest_checkpoint_at": "2026-11-16T10:00:00+00:00"},
            },
            "xmarket": {
                "status": "shadow_issuing",
                "scope_progress": [{
                    "asset": "BTC", "horizon_minutes": 15,
                    "status": "shadow_issuing",
                    "stored_gate_passing_artifacts": 2, "shadow_rows_24h": 77,
                    "latest_imported_at": "2026-11-16T12:00:00+00:00",
                    "latest_artifact_sha256_prefix": "deadbeef1234",
                    "latest_trained_through": "2026-11-15T00:00:00+00:00",
                }],
            },
        })
        artifacts = host["model_artifacts"]
        self.assertEqual(artifacts["status"], "recorded")
        incumbent = artifacts["incumbent"]
        self.assertEqual(incumbent["model_version"],
                         "prospective-logistic-persistent-v3")
        self.assertEqual(incumbent["serving_status"], "serving")
        self.assertEqual(incumbent["weights_sha256_prefix"], "abcdef012345")
        self.assertEqual(incumbent["lab"]["streams_with_training_updates"], 2)
        self.assertFalse(incumbent["proves_improvement"])
        self.assertEqual(len(artifacts["xmarket"]), 1)
        self.assertEqual(artifacts["xmarket"][0]["artifact_sha256_prefix"],
                         "deadbeef1234")
        self.assertNotIn("weights", str(artifacts).replace(
            "weights_sha256_prefix", ""))
        missing, _ = summarize_host(None)
        self.assertEqual(missing["model_artifacts"]["status"], "unavailable")
        self.assertIsNone(missing["model_artifacts"]["incumbent"])

    def test_paper_proof_keeps_zero_qualified_and_gate_detail(self) -> None:
        checks = {"future_outcomes": False, "beats_baselines": False,
                  "quote_coverage": False, "after_cost_paper": False,
                  "valid_records": True}
        proof = _paper_proof({
            "status": "recorded", "evaluated_scopes": 71, "fresh_scopes": 12,
            "qualified_paper_scopes": 0,
            "best_exact_source": {
                "asset": "XRP", "venue": "binance", "quote": "USDT",
                "horizon_minutes": 15, "fresh": True, "targets": 165,
                "days": 22, "eligible_quotes": 160, "closed_quotes": 144,
                "quote_coverage": 0.9, "qualified_paper": False,
                "checks": checks, "trades": 60, "gain": -0.002,
                "severe_lower_bps": -3.5, "promotion_status": "held",
                "evidence_json": "PRIVATE",
            },
            "xmarket_v1": {
                "status": "recorded", "evaluated_scopes": 71,
                "fresh_scopes": 12, "qualified_paper_scopes": 0,
                "scopes": [
                    {"asset": "BTC", "venue": "hyperliquid", "quote": "USDC",
                     "horizon_minutes": 15, "fresh": True, "status": "recorded",
                     "qualified_paper": False, "targets": 40, "days": 1,
                     "eligible_quotes": 27, "closed_quotes": 0,
                     "quote_coverage": 0.0, "missing_quote_windows": 27,
                     "checks": checks},
                    {"asset": "BTC", "venue": "binance", "quote": "USDT",
                     "horizon_minutes": 15, "fresh": True, "status": "recorded",
                     "qualified_paper": False, "targets": 55, "days": 2,
                     "eligible_quotes": 30, "closed_quotes": 3,
                     "quote_coverage": 0.1, "missing_quote_windows": 27,
                     "checks": checks},
                ],
                "scope_handoff": {
                    "status": "recorded", "fitted_scopes": 5,
                    "issuing_scopes": 5,
                    "issuing_scopes_with_fresh_source_evaluation": 3,
                    "issuing_scopes_missing_supported_source_evaluation": 2,
                    "scopes": [{
                        "asset": "BTC", "horizon_minutes": 15,
                        "issued_24h": 77,
                        "status": "source_evaluation_recorded",
                        "source_evaluations": 7,
                        "fresh_valid_source_evaluations": 7,
                        "quote_gate_passing_sources": 0,
                        "qualified_paper_sources": 0,
                    }],
                },
            },
        })
        self.assertEqual(proof["qualified_paper_scopes"], 0)
        best = proof["best_exact_source"]
        self.assertEqual(best["promotion_status"], "held")
        self.assertEqual(best["trades"], 60)
        self.assertEqual(best["severe_lower_bps"], -3.5)
        self.assertNotIn("PRIVATE", str(proof))
        self.assertTrue(proof["does_not_prove_real_fills"])
        scopes = proof["xmarket_v1"]["scopes"]
        self.assertEqual(len(scopes), 1)
        self.assertEqual(scopes[0]["sources_evaluated"], 2)
        self.assertEqual(scopes[0]["fresh_valid_sources"], 2)
        self.assertEqual(scopes[0]["min_targets"], 40)
        self.assertEqual(scopes[0]["min_days"], 1)
        self.assertEqual(scopes[0]["min_coverage"], 0.0)
        self.assertEqual(scopes[0]["failing_checks"]["quote_coverage"], 2)
        self.assertEqual(scopes[0]["venue_count"], 2)
        handoff = proof["xmarket_v1"]["scope_handoff"]
        self.assertEqual(handoff["issuing_scopes"], 5)
        self.assertNotIn("scopes", handoff)

    def test_paper_trade_count_is_bounded_like_every_other_count(self) -> None:
        # Delivery 4: trades used to pass through unbounded in both the
        # publisher and the receiver while every neighbouring count was
        # capped at 10,000,000.
        checks = {"future_outcomes": True, "beats_baselines": True,
                  "quote_coverage": True, "after_cost_paper": False,
                  "valid_records": True}
        base = {"asset": "XRP", "venue": "binance", "quote": "USDT",
                "horizon_minutes": 15, "fresh": True, "targets": 165,
                "days": 22, "eligible_quotes": 160, "closed_quotes": 144,
                "quote_coverage": 0.9, "qualified_paper": False,
                "checks": checks, "gain": -0.002, "severe_lower_bps": -3.5,
                "promotion_status": "held"}
        proof = _paper_proof({
            "status": "recorded", "evaluated_scopes": 71, "fresh_scopes": 12,
            "qualified_paper_scopes": 0,
            "best_exact_source": dict(base, trades=10_000_001)})
        self.assertNotIn("trades", proof["best_exact_source"])
        proof = _paper_proof({
            "status": "recorded", "evaluated_scopes": 71, "fresh_scopes": 12,
            "qualified_paper_scopes": 0,
            "best_exact_source": dict(base, trades=10_000_000)})
        self.assertEqual(proof["best_exact_source"]["trades"], 10_000_000)

    def test_research_quotes_and_watchdog_stay_bounded_and_private_free(self) -> None:
        loop = _research_loop({
            "enabled": True, "drive_folder_ids_present": True,
            "status": "recorded",
            "recent_by_status": {"screened": 3, "discarded": 1,
                                 "BAD KEY!": 9, "ablating": 2},
            "recent_submitted_to_drive": 1, "api_key": "PRIVATE",
        })
        self.assertEqual(loop["recent_by_status"],
                         {"screened": 3, "discarded": 1, "ablating": 2})
        self.assertNotIn("PRIVATE", str(loop))
        quotes = _xmarket_quotes({
            "status": "recorded", "window_start": "2026-11-16T10:00:00+00:00",
            "captured_at": "2026-11-16T12:00:00+00:00",
            "scopes": [
                {"asset": "BTC", "horizon_minutes": 15, "venue": "okx",
                 "contract": "BTCUSDT", "quote": "USDT", "forecast_rows": 10,
                 "decisive_forecast_rows": 4, "entry_rows": 3,
                 "closed_exit_rows": 2, "missed_exit_rows": 1,
                 "elapsed_unmatched_decisive_rows": 0,
                 "latest_entry_at": "2026-11-16T11:00:00+00:00",
                 "latest_exit_at": "2026-11-16T11:30:00+00:00"},
                {"asset": "BTC", "horizon_minutes": 15, "venue": "binance",
                 "contract": "BTCUSDT", "quote": "USDT", "forecast_rows": 5,
                 "decisive_forecast_rows": 2, "entry_rows": 1,
                 "closed_exit_rows": 1, "missed_exit_rows": 0,
                 "elapsed_unmatched_decisive_rows": 1,
                 "latest_entry_at": None, "latest_exit_at": None},
            ],
        })
        self.assertEqual(len(quotes["scopes"]), 1)
        scope = quotes["scopes"][0]
        self.assertEqual(scope["forecast_rows"], 15)
        self.assertEqual(scope["entry_rows"], 4)
        self.assertEqual(len(scope["venues"]), 2)
        self.assertFalse(quotes["counts_are_independent_targets"])
        watchdog = _xmarket_watchdog({
            "status": "fresh", "operational_status": "degraded",
            "census_status": "recorded",
            "scope_checked_at": "2026-11-16T12:00:00+00:00",
            "fitted_scopes": 5, "issuing_scopes": 4, "silent_scopes": 1,
            "open_silence_incidents": 1, "resolved_silence_incidents": 2,
            "scopes": [{"asset": "BTC", "horizon_minutes": 15,
                        "status": "issuing", "incumbent_rows": 10,
                        "shadow_rows": 7}],
        })
        self.assertEqual(watchdog["operational_status"], "degraded")
        self.assertEqual(watchdog["scopes"][0]["shadow_rows"], 7)

    def test_drive_report_keeps_gate_failures_and_artifact_identity(self) -> None:
        doc = {"schema": "rival-xmarket-model-v1", "asset": "BTC",
               "horizon_minutes": 5, "training_status": "blocked",
               "gates_passed": False,
               "gate_failures": ["split too small: train=0 val=0 test=12",
                                 "x" * 200],
               "data_checks": {
                   "polymarket": {"status": "unverified", "paired_points": 0,
                                  "blocked_reason": "no_prospective_export"},
                   "kalshi": {"status": "unverified", "paired_points": 0},
               },
               "effective_window": {"rows": 0}}
        public = summarize_xmarket_report(doc, "2026-11-12T15:00:00Z",
                                          "abcdef012345")
        self.assertEqual(public["gate_failures"],
                         ["split too small: train=0 val=0 test=12", "x" * 80])
        self.assertEqual(public["artifact_sha256_prefix"], "abcdef012345")
        self.assertIsNone(summarize_xmarket_report(
            doc, None, "not-a-prefix")["artifact_sha256_prefix"])
        self.assertEqual(_artifact_prefix({"b": 1, "a": 2}),
                         _artifact_prefix({"a": 2, "b": 1}))

    def test_no_drive_folder_never_touches_provider(self) -> None:
        with patch.dict(os.environ, {"RIVAL_DRIVE_REPORTS_FOLDER_ID": ""}):
            value = read_drive()
        self.assertEqual(value["status"], "unavailable")
        self.assertIsNone(value["reports_considered"])
        self.assertEqual(value["reports"], [])

    def test_read_failures_stay_distinct_from_zero_and_stale(self) -> None:
        from urllib import error as url_error

        self.assertEqual(classify_read_error(
            url_error.HTTPError("u", 401, "x", {}, None)),
            ("auth_failed", "auth_http_401"))
        self.assertEqual(classify_read_error(
            url_error.HTTPError("u", 429, "x", {}, None)),
            ("failed_read", "rate_limited_http_429"))
        self.assertEqual(classify_read_error(
            url_error.HTTPError("u", 503, "x", {}, None)),
            ("unreachable", "upstream_http_5xx"))
        self.assertEqual(classify_read_error(TimeoutError()),
                         ("unreachable", "timeout"))
        self.assertEqual(classify_read_error(
            url_error.URLError("dns")), ("unreachable", "network_error"))
        self.assertEqual(classify_read_error(ValueError("bad json")),
                         ("failed_read", "invalid_response"))
        self.assertEqual(classify_read_error(
            ValueError("drive_credentials_not_configured")),
            ("not_configured", "credentials_not_configured"))

    def test_per_source_details_keep_stale_no_records_and_zero_distinct(self) -> None:
        host, db = summarize_host({
            "outcome": "verified",
            "forecast": {"status": "stale_or_clock_invalid", "issued": 3},
            "learning": {"status": "not_recorded"},
        })
        with patch.dict(os.environ, {
            "GITHUB_TOKEN": "", "DISCLOUD_TOKEN": "",
            "RIVAL_DRIVE_REPORTS_FOLDER_ID": "",
        }):
            details = build_source_details(
                host, db,
                {"status": "unavailable", "reports_considered": None},
                {"status": "unavailable"},
                {"status": "no_pairs", "scopes": []}, {},
                checked_at="2026-11-17T02:00:00+00:00")
        self.assertEqual(details["discloud_forecast"]["status"], "stale")
        self.assertEqual(details["discloud_forecast"]["reason"],
                         "stale_or_clock_invalid")
        self.assertEqual(details["discloud_learning"]["status"], "no_records")
        self.assertEqual(details["postgres"]["status"], "no_records")
        self.assertEqual(details["drive"]["status"], "not_configured")
        self.assertEqual(details["github"]["status"], "not_configured")
        self.assertEqual(details["performance"]["status"], "measured_zero")
        self.assertEqual(details["performance"]["reason"], "no_pairs_measured")
        for entry in details.values():
            self.assertIn("freshness_seconds", entry)
            self.assertGreater(entry["freshness_seconds"], 0)
        self.assertEqual(details["discloud_forecast"]["freshness_seconds"], 900)
        self.assertEqual(details["drive"]["freshness_seconds"], 86400)

    def test_host_error_marks_both_discloud_sources_unreachable(self) -> None:
        from urllib import error as url_error

        host, db = summarize_host(None)
        details = build_source_details(
            host, db, {"status": "unavailable"},
            {"status": "unavailable"}, {"status": "unavailable"},
            {"host": url_error.URLError("dns")},
            checked_at="2026-11-17T02:00:00+00:00")
        self.assertEqual(details["discloud_forecast"]["status"], "unreachable")
        self.assertEqual(details["discloud_learning"]["status"], "unreachable")
        self.assertEqual(details["discloud_forecast"]["reason"], "network_error")

    def test_partial_performance_is_not_ok(self) -> None:
        # F11: "partial" means some scopes measured and some scope reads
        # failed, so the read did not complete. It must not read as "ok";
        # the measured scopes stay visible in the performance section.
        host, db = summarize_host(None)
        details = build_source_details(
            host, db, {"status": "unavailable"},
            {"status": "unavailable"},
            {"status": "partial", "scopes": [{"asset": "BTC"}],
             "failed_scopes": 3}, {},
            checked_at="2026-11-17T02:00:00+00:00")
        self.assertEqual(details["performance"]["status"], "failed_read")
        self.assertEqual(details["performance"]["reason"], "partial_performance")

    def test_collection_block_sanitizes_trigger_run_and_attempt(self) -> None:
        with patch.dict(os.environ, {
            "GITHUB_EVENT_NAME": "schedule", "GITHUB_RUN_ID": "44002956865",
            "GITHUB_RUN_ATTEMPT": "2",
        }):
            block = _collection_block()
        self.assertEqual(block["event"], "schedule")
        self.assertEqual(block["run_id"], "44002956865")
        self.assertEqual(block["run_attempt"], 2)
        self.assertFalse(block["coalesced"])
        with patch.dict(os.environ, {
            "GITHUB_EVENT_NAME": "pull_request;rm -rf",
            "GITHUB_RUN_ID": "not-a-run", "GITHUB_RUN_ATTEMPT": "999",
        }):
            block = _collection_block()
        self.assertEqual(block["event"], "unknown")
        self.assertIsNone(block["run_id"])
        self.assertIsNone(block["run_attempt"])
        self.assertNotIn("rm", str(block))

    def test_recent_receipt_check_fails_open_to_full_collection(self) -> None:
        from datetime import UTC, datetime, timedelta

        import evidence_board_receipt as receipt_module

        fresh = {"captured_at": (datetime.now(UTC) - timedelta(seconds=60)).isoformat()}
        with patch.object(receipt_module, "_url_json", return_value=fresh):
            self.assertTrue(_recent_receipt_is_fresh())
        stale = {"captured_at": "2026-11-17T00:00:00+00:00"}
        with patch.object(receipt_module, "_url_json", return_value=stale):
            self.assertFalse(_recent_receipt_is_fresh())
        future = {"captured_at": "2099-01-01T00:00:00+00:00"}
        with patch.object(receipt_module, "_url_json", return_value=future):
            self.assertFalse(_recent_receipt_is_fresh())
        with patch.object(receipt_module, "_url_json",
                          side_effect=TimeoutError()):
            self.assertFalse(_recent_receipt_is_fresh())

    def test_recent_receipt_check_reads_realistically_sized_receipt(self) -> None:
        # F1: the live stored receipt is ~52KB, but the freshness check
        # used to read it with limit=20_000, so _url_json always raised
        # provider_response_too_large and coalescing never fired. This test
        # stubs the HTTP layer (urlopen), not the function under test, and
        # serves a realistically-sized body through the real _url_json.
        from datetime import UTC, datetime, timedelta

        import evidence_board_receipt as receipt_module

        class _FakeLatestResponse:
            def __init__(self, body: bytes):
                self._body = body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit: int = -1) -> bytes:
                return self._body if limit < 0 else self._body[:limit]

        fresh_at = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
        body = json.dumps(
            {"captured_at": fresh_at, "padding": "x" * 51_700},
            separators=(",", ":")).encode()
        # Bigger than the old 20,000-byte limit, smaller than the 128,000
        # receipt cap: this body would have failed the old check.
        self.assertGreater(len(body), 20_000)
        self.assertLess(len(body), 128_000)
        with patch.object(receipt_module.request, "urlopen",
                          return_value=_FakeLatestResponse(body)):
            self.assertTrue(_recent_receipt_is_fresh())
        # A body over the cap cannot be a legal stored receipt: the check
        # still fails open to a full collection instead of crashing.
        oversized = json.dumps(
            {"captured_at": fresh_at, "padding": "x" * 129_000},
            separators=(",", ":")).encode()
        self.assertGreater(len(oversized), 128_000)
        with patch.object(receipt_module.request, "urlopen",
                          return_value=_FakeLatestResponse(oversized)):
            self.assertFalse(_recent_receipt_is_fresh())

    def test_flood_trigger_coalesces_instead_of_rehitting_upstreams(self) -> None:
        import evidence_board_receipt as receipt_module

        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "workflow_run"}), \
                patch.object(receipt_module, "_recent_receipt_is_fresh",
                             return_value=True), \
                patch.object(receipt_module, "_github_oidc",
                             return_value="jwt"), \
                patch.object(receipt_module, "_report_attempt") as report, \
                patch.object(receipt_module, "read_host") as host_read:
            self.assertEqual(publish(), 0)
            host_read.assert_not_called()
            report.assert_called_once_with("jwt", "coalesced", None)

    def test_schedule_and_dispatch_runs_never_coalesce(self) -> None:
        # Schedule runs are the cadence backbone and workflow_dispatch is
        # the keepalive: both always collect, even when a fresh receipt
        # stands. The freshness check is forced True here so only the
        # event gate can save the run from coalescing.
        import evidence_board_receipt as receipt_module

        seen: dict = {}

        def _capture(payload: dict, jwt: str) -> dict:
            seen["payload"] = payload
            return {"status": "accepted"}

        for event in ("schedule", "workflow_dispatch"):
            with self.subTest(event=event), \
                    patch.dict(os.environ, {
                        "GITHUB_EVENT_NAME": event,
                        "GITHUB_RUN_ID": "44002956865",
                        "GITHUB_RUN_ATTEMPT": "1", "GITHUB_TOKEN": "",
                        "DISCLOUD_TOKEN": "",
                        "RIVAL_DRIVE_REPORTS_FOLDER_ID": "",
                    }), \
                    patch.object(receipt_module, "read_host",
                                 return_value=None), \
                    patch.object(receipt_module, "_github_oidc",
                                 return_value="jwt"), \
                    patch.object(receipt_module, "_submit_signed_receipt",
                                 side_effect=_capture), \
                    patch.object(receipt_module, "_recent_receipt_is_fresh",
                                 return_value=True):
                seen.clear()
                self.assertEqual(publish(), 0)
                self.assertIn("payload", seen)
                self.assertEqual(seen["payload"]["collection"]["event"], event)

    def test_failed_publication_reports_a_signed_attempt_reason(self) -> None:
        import evidence_board_receipt as receipt_module

        with patch.dict(os.environ, {
            "GITHUB_EVENT_NAME": "schedule", "GITHUB_TOKEN": "",
            "DISCLOUD_TOKEN": "",
            "RIVAL_DRIVE_REPORTS_FOLDER_ID": "",
        }), patch.object(receipt_module, "read_host", return_value=None), \
                    patch.object(receipt_module, "_github_oidc",
                             return_value="jwt"), \
                    patch.object(receipt_module, "_submit_signed_receipt",
                             side_effect=ValueError(
                                 "receiver_http_403_authorization_required")), \
                    patch.object(receipt_module, "_report_attempt") as report, \
                    patch.object(receipt_module, "_recent_receipt_is_fresh",
                             return_value=False):
            with self.assertRaises(ValueError):
                publish()
            report.assert_called_once()
            _, outcome, reason = report.call_args.args
            self.assertEqual(outcome, "failed")
            self.assertEqual(reason, "receiver_http_403_authorization_required")

    def test_successful_publication_carries_collection_and_sources(self) -> None:
        import evidence_board_receipt as receipt_module

        seen: dict = {}

        def _capture(payload: dict, jwt: str) -> dict:
            seen["payload"] = payload
            return {"status": "accepted"}

        with patch.dict(os.environ, {
            "GITHUB_EVENT_NAME": "schedule", "GITHUB_RUN_ID": "44002956865",
            "GITHUB_RUN_ATTEMPT": "1", "GITHUB_TOKEN": "",
            "DISCLOUD_TOKEN": "",
            "RIVAL_DRIVE_REPORTS_FOLDER_ID": "",
        }), patch.object(receipt_module, "read_host", return_value=None), \
                    patch.object(receipt_module, "_github_oidc",
                             return_value="jwt"), \
                    patch.object(receipt_module, "_submit_signed_receipt",
                             side_effect=_capture), \
                    patch.object(receipt_module, "_recent_receipt_is_fresh",
                             return_value=False):
            self.assertEqual(publish(), 0)
        payload = seen["payload"]
        self.assertEqual(payload["collection"]["event"], "schedule")
        self.assertEqual(payload["collection"]["run_id"], "44002956865")
        self.assertEqual(payload["sources"]["discloud_forecast"]["status"],
                         "not_configured")
        self.assertEqual(payload["sources"]["github"]["status"],
                         "not_configured")
        self.assertEqual(payload["sources"]["performance"]["status"],
                         "failed_read")

    def test_host_read_failure_is_not_mislabeled_as_credentials(self) -> None:
        # Regression (2026-11-20): a raised host read — e.g. the bot
        # mid-restart during a deploy, so the console reply carries no
        # receipt — used to be overwritten with
        # host_credentials_not_configured because host_report stayed
        # None, and the board posted a bogus credentials warning. The
        # receipt must carry the real failure, after exactly one retry.
        import evidence_board_receipt as receipt_module

        seen: dict = {}

        def _capture(payload: dict, jwt: str) -> dict:
            seen["payload"] = payload
            return {"status": "accepted"}

        with patch.dict(os.environ, {
            "GITHUB_EVENT_NAME": "schedule", "GITHUB_RUN_ID": "44002956865",
            "GITHUB_RUN_ATTEMPT": "1", "GITHUB_TOKEN": "",
            "DISCLOUD_TOKEN": "",
            "RIVAL_DRIVE_REPORTS_FOLDER_ID": "",
        }), patch.object(receipt_module, "HOST_READ_RETRY_DELAY_SECONDS", 0), \
                patch.object(receipt_module, "read_host",
                             side_effect=ValueError(
                                 "missing host learning receipt")) as host_read, \
                patch.object(receipt_module, "_github_oidc",
                             return_value="jwt"), \
                patch.object(receipt_module, "_submit_signed_receipt",
                             side_effect=_capture), \
                patch.object(receipt_module, "_recent_receipt_is_fresh",
                             return_value=False):
            self.assertEqual(publish(), 0)
        self.assertEqual(host_read.call_count, 2)
        sources = seen["payload"]["sources"]
        self.assertEqual(sources["discloud_forecast"]["status"], "failed_read")
        self.assertEqual(sources["discloud_forecast"]["reason"],
                         "invalid_response")
        self.assertEqual(sources["discloud_learning"]["status"], "failed_read")
        self.assertEqual(sources["postgres"]["status"], "no_records")

    def test_host_read_retry_recovers_from_a_transient_failure(self) -> None:
        import evidence_board_receipt as receipt_module

        report = {
            "outcome": "verified",
            "forecast": {"status": "fresh", "issued": 105},
            "learning": {"status": "fresh", "settled": 101,
                         "math_ok": True, "model_review_ok": True},
            "forward_proof": {"status": "recorded",
                              "qualified_paper_scopes": 0,
                              "evaluated_scopes": 12},
            "actual_learning": {
                "status": "weight_updates_observed",
                "streams_with_verified_weight_updates": 102,
                "streams_with_before_after": 102,
            },
        }
        seen: dict = {}

        def _capture(payload: dict, jwt: str) -> dict:
            seen["payload"] = payload
            return {"status": "accepted"}

        with patch.dict(os.environ, {
            "GITHUB_EVENT_NAME": "schedule", "GITHUB_RUN_ID": "44002956865",
            "GITHUB_RUN_ATTEMPT": "1", "GITHUB_TOKEN": "",
            "DISCLOUD_TOKEN": "",
            "RIVAL_DRIVE_REPORTS_FOLDER_ID": "",
        }), patch.object(receipt_module, "HOST_READ_RETRY_DELAY_SECONDS", 0), \
                patch.object(receipt_module, "read_host",
                             side_effect=[ValueError(
                                 "missing host learning receipt"),
                                 report]) as host_read, \
                patch.object(receipt_module, "_github_oidc",
                             return_value="jwt"), \
                patch.object(receipt_module, "_submit_signed_receipt",
                             side_effect=_capture), \
                patch.object(receipt_module, "_recent_receipt_is_fresh",
                             return_value=False):
            self.assertEqual(publish(), 0)
        self.assertEqual(host_read.call_count, 2)
        sources = seen["payload"]["sources"]
        self.assertEqual(sources["discloud_forecast"]["status"], "ok")
        self.assertEqual(sources["discloud_learning"]["status"], "ok")
        self.assertEqual(sources["postgres"]["status"], "ok")

    def test_host_read_returning_none_is_not_retried(self) -> None:
        # None means the credentials are absent or malformed; retrying
        # cannot change that, and the label stays credentials_not_configured.
        import evidence_board_receipt as receipt_module

        seen: dict = {}

        def _capture(payload: dict, jwt: str) -> dict:
            seen["payload"] = payload
            return {"status": "accepted"}

        with patch.dict(os.environ, {
            "GITHUB_EVENT_NAME": "schedule", "GITHUB_RUN_ID": "44002956865",
            "GITHUB_RUN_ATTEMPT": "1", "GITHUB_TOKEN": "",
            "DISCLOUD_TOKEN": "",
            "RIVAL_DRIVE_REPORTS_FOLDER_ID": "",
        }), patch.object(receipt_module, "HOST_READ_RETRY_DELAY_SECONDS", 0), \
                patch.object(receipt_module, "read_host",
                             return_value=None) as host_read, \
                patch.object(receipt_module, "_github_oidc",
                             return_value="jwt"), \
                patch.object(receipt_module, "_submit_signed_receipt",
                             side_effect=_capture), \
                patch.object(receipt_module, "_recent_receipt_is_fresh",
                             return_value=False):
            self.assertEqual(publish(), 0)
        self.assertEqual(host_read.call_count, 1)
        self.assertEqual(
            seen["payload"]["sources"]["discloud_forecast"]["status"],
            "not_configured")


class CombinedPayloadBudgetTests(unittest.TestCase):
    """Delivery 4: the D1+D2+D3 sections together must fit the collector's
    byte cap. Before the Delivery 4 budget (40 PRs / 45 runs) a
    worst-case realistic receipt measured 106,820 bytes and even a live-like
    one 82,629 bytes -- both rejected with 413, losing that publication.
    The budget trims counts, not evidence: 20 RivaL PRs (every pinned PR
    plus the newest others), 12 runs, and 80-char gate failures. The cap was raised 80,000 -> 128,000 on
    2026-11-19 after the live receipt reached 80,034 bytes and full
    publications stopped; the ceiling here is the publisher's 96,000-byte
    warning line, so a worst-case run keeps >=32KB headroom and trips the
    warning long before the cap."""

    NOW = "2026-11-17T03:00:00+00:00"
    ASSETS = ("BTC", "ETH", "SOL", "XRP")
    HORIZONS = (5, 15, 60)

    def _max_pr(self, number: int) -> dict:
        return {"number": number, "title": "T" * 160, "state": "closed",
                "merged_at": "2026-11-17T02:00:00Z",
                "merge_commit_sha": "b" * 40,
                "created_at": "2026-11-16T02:00:00Z",
                "updated_at": "2026-11-17T02:00:00Z",
                "head": {"sha": "b" * 40}, "base": {"sha": "c" * 40}}

    def _max_run(self, run_id: int) -> dict:
        return {"id": run_id, "name": "N" * 120, "event": "E" * 45,
                "status": "completed", "conclusion": "success",
                "head_sha": "b" * 40,
                "created_at": "2026-11-17T02:00:00Z",
                "updated_at": "2026-11-17T02:00:00Z", "run_attempt": 3}

    def _max_host_report(self) -> dict:
        scope_rows = [{
            "asset": a, "horizon_minutes": h, "status": "shadow_issuing",
            "stored_gate_passing_artifacts": 3, "shadow_rows_24h": 999,
            "latest_imported_at": self.NOW, "latest_shadow_at": self.NOW,
            "fresh_native_sources": 2,
            "latest_artifact_sha256_prefix": "a" * 64,
            "latest_trained_through": self.NOW,
            "missing_native_sources": [], "stale_native_sources": [],
        } for a in self.ASSETS for h in self.HORIZONS]
        forecast_rows = [{
            "asset": a, "horizon": h, "venue": "polymarket",
            "quote": "USDT", "issued_at": "2026-11-17T02:49:00+00:00",
            "target_opened_at": "2026-11-17T02:50:00+00:00",
            "target_close_at": "2026-11-17T02:55:00+00:00",
            "settled_at": "2026-11-17T02:56:00+00:00",
            "probability_up": 0.987654, "baseline_up": 0.5,
            "direction": "UP", "status": "settled", "lead_seconds": 7199.99,
            "outcome_up": 1,
        } for a in self.ASSETS for h in self.HORIZONS for _ in range(3)]
        checks = {"future_outcomes": False, "beats_baselines": False,
                  "quote_coverage": True, "after_cost_paper": False,
                  "valid_records": True}
        paper_row = {"asset": "BTC", "horizon_minutes": 5,
                     "venue": "polymarket", "quote": "USDT", "fresh": True,
                     "qualified_paper": False, "targets": 99999,
                     "days": 999, "eligible_quotes": 99999,
                     "closed_quotes": 99999, "quote_coverage": 0.9999,
                     "missing_quote_windows": 9999, "checks": checks,
                     "trades": 999999, "gain": 9.999999,
                     "severe_lower_bps": 99999.0, "promotion_status": "held"}
        paper_rows = [dict(paper_row, asset=a, horizon_minutes=h,
                           venue=v)
                      for a in self.ASSETS for h in self.HORIZONS
                      for v in ("polymarket", "kalshi", "coinbase",
                                "kraken", "bitget", "bybit")]
        quote_rows = [{
            "asset": a, "horizon_minutes": h, "venue": v, "quote": "USDT",
            "forecast_rows": 999, "decisive_forecast_rows": 999,
            "entry_rows": 999, "closed_exit_rows": 999,
            "missed_exit_rows": 999, "elapsed_unmatched_decisive_rows": 999,
            "latest_entry_at": self.NOW, "latest_exit_at": self.NOW,
        } for a in self.ASSETS for h in self.HORIZONS
            for v in ("polymarket", "kalshi", "coinbase", "kraken",
                      "bitget", "bybit", "okx", "binance", "hyperliquid",
                      "crypto-com")]
        return {
            "outcome": "verified",
            "forecast": {"status": "fresh", "issued": 99999},
            "learning": {"status": "fresh", "settled": 99999,
                         "math_ok": True, "model_review_ok": True},
            "xmarket": {
                "status": "shadow_issuing",
                "export_coverage": {
                    "published": [{"asset": a, "exported_days": 999,
                                   "latest_exported_day": "2026-11-16"}
                                  for a in self.ASSETS],
                    "pending_by_asset": [{"asset": a, "days": 999}
                                         for a in self.ASSETS],
                    "recent_completed_days_pending": 999,
                    "eligible_before": self.NOW},
                "scope_progress": scope_rows,
                "source_coverage": {"fresh_scopes": 12,
                                    "expected_scopes": 12},
                "shadow_rows_24h": 99999,
                "imported_scopes": [f"{a}-{h}" for a in self.ASSETS
                                    for h in self.HORIZONS]},
            "path_targets": {"kucoin_gap_recovery": {
                "status": "recorded",
                "assets": [{"asset": a, "queued_exact_gaps_14d": 999,
                            "due_exact_gaps_14d": 999,
                            "attempted_exact_gaps_14d": 999,
                            "bulk_status": "upstream_empty"}
                           for a in self.ASSETS]}},
            "advance_forecasts": {
                "channel_cards": {"status": "recorded", "active": 99},
                "recent_deliveries": [{}] * 100,
                "model": "advance-candle-logistic-lead5-v1"},
            "recent_forward_calls": {"status": "observed",
                                     "captured_at": self.NOW,
                                     "rows": forecast_rows},
            "actual_learning": {
                "status": "weight_updates_observed",
                "model_version": "prospective-logistic-persistent-v3",
                "latest_weights_sha256_prefix": "a" * 64,
                "latest_seed_sha256_prefix": "a" * 64,
                "latest_online_training_updates": 999999,
                "sampled_forecasts": 99999, "sampled_model_streams": 999,
                "streams_with_verified_weight_updates": 999,
                "lab": {"status": "recorded", "sampled_streams": 999,
                        "streams_with_training_updates": 999,
                        "latest_checkpoint_at": self.NOW}},
            "research_loop": {
                "status": "recorded", "enabled": True,
                "drive_folder_ids_present": True,
                "recent_by_status": {f"experiment-status-{i:02d}": 16
                                     for i in range(16)},
                "recent_submitted_to_drive": 99999},
            "forward_proof": {
                "status": "recorded", "evaluated_scopes": 71,
                "fresh_scopes": 71, "qualified_paper_scopes": 0,
                "best_exact_source": dict(paper_row),
                "xmarket_v1": {
                    "status": "recorded", "evaluated_scopes": 71,
                    "fresh_scopes": 71, "qualified_paper_scopes": 0,
                    "scopes": paper_rows,
                    "scope_handoff": {
                        "status": "recorded", "fitted_scopes": 12,
                        "issuing_scopes": 12,
                        "issuing_scopes_with_fresh_source_evaluation": 12,
                        "issuing_scopes_missing_supported_source_evaluation": 0}}},
            "xmarket_quote_progress": {"status": "recorded",
                                       "window_start": self.NOW,
                                       "captured_at": self.NOW,
                                       "scopes": quote_rows},
            "xmarket_watchdog": {
                "status": "fresh", "operational_status": "healthy",
                "census_status": "recorded", "scope_checked_at": self.NOW,
                "scope_age_seconds": 99999, "watchdog_checked_at": self.NOW,
                "boot_started_at": self.NOW, "fitted_scopes": 12,
                "issuing_scopes": 12, "silent_scopes": 0,
                "open_silence_incidents": 0,
                "resolved_silence_incidents": 999,
                "scopes": [{"asset": a, "horizon_minutes": h,
                            "status": "issuing", "shadow_rows": 99999}
                           for a in self.ASSETS for h in self.HORIZONS]},
        }

    def test_worst_case_combined_receipt_fits_with_headroom(self) -> None:
        host, db = summarize_host(self._max_host_report())
        drive_reports = []
        for asset in self.ASSETS:
            for horizon in self.HORIZONS:
                doc = {"schema": "rival-xmarket-model-v1", "asset": asset,
                       "horizon_minutes": horizon, "family": "xmarket_v1",
                       "model_version": "xmarket-offline-v1",
                       "training_status": "fitted",
                       "evaluation_status": "held-out evaluated",
                       "gates_passed": True,
                       "gate_failures": ["x" * 200] * 4,
                       "data_checks": {
                           "polymarket": {"status": "verified",
                                          "paired_points": 99999},
                           "kalshi": {"status": "verified",
                                      "paired_points": 99999}},
                       "effective_window": {"rows": 999999}}
                drive_reports.append(summarize_xmarket_report(
                    doc, self.NOW, "a" * 64))
        drive = {"status": "verified_reports", "reports_considered": 12,
                 "latest_report_at": self.NOW, "reports": drive_reports}

        def fake_github(url: str, token: str, **kwargs: object) -> object:
            if url.startswith("/pulls?"):
                numbers = list(PINNED_PRS) + list(range(2000, 2028))
                return [self._max_pr(n) for n in numbers]
            if url.startswith("/actions/runs"):
                return {"workflow_runs": [self._max_run(44000000000 + i)
                                          for i in range(45)]}
            if url.startswith("/pulls/"):
                return self._max_pr(int(url.split("/")[-1]))
            raise AssertionError(f"unexpected url {url}")

        with patch("evidence_board_github._github_json",
                   side_effect=fake_github):
            github = read_github_snapshot("short-lived-token")
        performance = {
            "status": "observed", "captured_at": self.NOW,
            "sampling_policy": "per-asset-horizon-close-v3",
            "scopes": [{
                "asset": a, "horizon": h, "paired_outcomes": 500,
                "model_brier": 0.123456, "baseline_brier": 0.234567,
                "model_version": "prospective-logistic-persistent-v3",
                "unique_target_windows": 500, "venue_count": 20,
                "within_target_candle_pairs": 500,
                "minimum_lead_seconds": 7199.999,
                "outcome_clock": "target_5m_candle_close",
            } for a in self.ASSETS for h in self.HORIZONS]}
        env = {"GITHUB_TOKEN": "fake", "DISCLOUD_TOKEN": "fake",
               "RIVAL_DRIVE_REPORTS_FOLDER_ID": "synthetic-test-folder-id-0",
               "GITHUB_EVENT_NAME": "schedule", "GITHUB_RUN_ID": "123456789",
               "GITHUB_RUN_ATTEMPT": "1"}
        with patch.dict(os.environ, env):
            sources = build_source_details(
                host, db, drive, github, performance, {}, self.NOW)
            payload = {
                "schema_version": 1, "captured_at": self.NOW,
                "collection": _collection_block(), "sources": sources,
                "host": host, "database": db, "drive": drive,
                "github": github,
                "performance": performance}
        self.assertEqual(len(github["prs"]), 20)
        self.assertTrue({p["number"] for p in github["prs"]} >= set(PINNED_PRS))
        self.assertEqual(len(github["runs"]), 12)
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.assertLessEqual(
            len(body), 96000,
            f"worst-case combined receipt is {len(body)} bytes; "
            "the collector rejects anything over 128000 and the "
            "publisher warns past 96000")
        # F16b: the worst-case body must also pass the publisher's own
        # pre-send size guard, not just the collector's 413.
        _check_receipt_size(body)

    def test_oversized_receipt_is_refused_before_send(self) -> None:
        # F16b: the publisher refuses a receipt over the 128,000-byte cap
        # with a clear error instead of sending it for a collector 413.
        import evidence_board_receipt as receipt_module

        oversized = {"schema_version": 1, "padding": "x" * 128_001}
        with patch.object(receipt_module, "_url_json",
                          side_effect=AssertionError("must not send")), \
                self.assertRaises(ValueError) as caught:
            _submit_signed_receipt(oversized, "jwt")
        self.assertEqual(str(caught.exception), "receipt_over_cap")
        # At exactly the cap it sends (the collector accepts <= 128,000).
        at_cap = {"schema_version": 1,
                  "padding": "x" * (128_000 - len(json.dumps(
                      {"schema_version": 1, "padding": ""},
                      separators=(",", ":")).encode()))}
        self.assertEqual(len(json.dumps(
            at_cap, separators=(",", ":")).encode()), 128_000)
        with patch.object(receipt_module, "_url_json",
                          return_value={"status": "accepted"}) as sender:
            self.assertEqual(
                _submit_signed_receipt(at_cap, "jwt"), {"status": "accepted"})
            sender.assert_called_once()


if __name__ == "__main__":
    unittest.main()
