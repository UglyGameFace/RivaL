"""Tests for the public-safe RivaL evidence publisher (no network or secrets)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from evidence_board_github import (
    PINNED_PRS,
    read_github_snapshot,
    read_public_intelligence,
)
from evidence_board_receipt import (
    LLM_GENERATION_PROBE_PROGRAM,
    LLM_METRICS_CMD,
    LLM_METRICS_MARKER,
    _artifact_prefix,
    _collection_block,
    _nonnegative,
    _paper_proof,
    _recent_forecast_summary,
    _recent_receipt_is_fresh,
    _research_loop,
    _xmarket_quotes,
    _xmarket_watchdog,
    build_source_details,
    classify_read_error,
    publish,
    read_drive,
    read_intelligence_metrics,
    summarize_host,
    summarize_llm_metrics,
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
            "DISCLOUD_LLM_APP_ID": "", "RIVAL_DRIVE_REPORTS_FOLDER_ID": "",
        }):
            details = build_source_details(
                host, db,
                {"status": "unavailable", "reports_considered": None},
                {"status": "unavailable"}, {"status": "unavailable"},
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
            {"status": "unavailable"}, {"status": "unavailable"},
            {"host": url_error.URLError("dns")},
            checked_at="2026-11-17T02:00:00+00:00")
        self.assertEqual(details["discloud_forecast"]["status"], "unreachable")
        self.assertEqual(details["discloud_learning"]["status"], "unreachable")
        self.assertEqual(details["discloud_forecast"]["reason"], "network_error")

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

    def test_failed_publication_reports_a_signed_attempt_reason(self) -> None:
        import evidence_board_receipt as receipt_module

        with patch.dict(os.environ, {
            "GITHUB_EVENT_NAME": "schedule", "GITHUB_TOKEN": "",
            "DISCLOUD_TOKEN": "", "DISCLOUD_LLM_APP_ID": "",
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
            "DISCLOUD_TOKEN": "", "DISCLOUD_LLM_APP_ID": "",
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


class LlmMetricsTests(unittest.TestCase):
    def _valid_raw(self) -> dict:
        return {
            "model": "LiquidAI/LFM3-1.7B-GGUF:Q4_K_M", "ready": True,
            "source_sha": "a" * 40, "repair_state": "healthy",
            "repair_count": 2, "runtime_context_tokens": 8192,
            "memory": {"current_mb": 1190, "limit_mb": 4088,
                       "peak_mb": 1300, "anon_mb": 900, "file_mb": 200,
                       "file_mapped_mb": 150, "gateway_rss_mb": 60,
                       "backend_rss_mb": 1100, "oom_kills": 0},
            "status_latency_ms": 42.5, "status_http": 200,
            "token": "PRIVATE", "prompt": "PRIVATE",
        }

    def test_status_probe_is_observed_but_never_proves_generation(self) -> None:
        clean = summarize_llm_metrics(self._valid_raw())
        self.assertEqual(clean["status"], "observed")
        self.assertEqual(clean["model"], "LiquidAI/LFM3-1.7B-GGUF:Q4_K_M")
        self.assertTrue(clean["ready"])
        self.assertFalse(clean["inference_verified"])
        self.assertAlmostEqual(clean["memory_pressure_pct"], 29.1, places=1)
        self.assertEqual(clean["memory"]["oom_kills"], 0)
        self.assertEqual(clean["repair_state"], "healthy")
        self.assertEqual(clean["status_latency_ms"], 42.5)
        self.assertNotIn("PRIVATE", str(clean))
        self.assertNotIn("prompt", str(clean))

    def test_generation_claim_needs_an_authenticated_generation_probe(self) -> None:
        raw = self._valid_raw()
        raw["inference_verified"] = True
        self.assertFalse(summarize_llm_metrics(raw)["inference_verified"])
        raw["generation_probe"] = "authenticated_generation"
        self.assertTrue(summarize_llm_metrics(raw)["inference_verified"])

    def test_generation_timing_fields_are_carried_when_probe_verified(self) -> None:
        raw = self._valid_raw()
        raw.update({
            "inference_verified": True,
            "generation_probe": "authenticated_generation",
            "latency_ms_p50": 5230.4, "latency_ms_p95": 5230.4,
            "tokens_per_second": 8.024, "requests_sampled": 1,
            "generation_errors": 0,
        })
        clean = summarize_llm_metrics(raw)
        self.assertTrue(clean["inference_verified"])
        self.assertEqual(clean["latency_ms_p50"], 5230.4)
        self.assertEqual(clean["latency_ms_p95"], 5230.4)
        self.assertEqual(clean["tokens_per_second"], 8.024)
        self.assertEqual(clean["requests_sampled"], 1)
        self.assertEqual(clean["generation_errors"], 0)

    def test_generation_failure_stays_unverified_but_observed(self) -> None:
        raw = self._valid_raw()
        raw.update({"inference_verified": False, "requests_sampled": 1,
                    "generation_errors": 1})
        clean = summarize_llm_metrics(raw)
        self.assertEqual(clean["status"], "observed")
        self.assertFalse(clean["inference_verified"])
        self.assertEqual(clean["generation_errors"], 1)
        self.assertIsNone(clean["latency_ms_p50"])
        self.assertIsNone(clean["tokens_per_second"])

    def test_generation_marker_is_parsed_and_verifies(self) -> None:
        raw = self._valid_raw()
        raw.update({
            "inference_verified": True,
            "generation_probe": "authenticated_generation",
            "latency_ms_p50": 5230.4, "latency_ms_p95": 5230.4,
            "tokens_per_second": 8.024, "requests_sampled": 1,
            "generation_errors": 0,
        })
        marker = LLM_METRICS_MARKER + json.dumps(raw)
        shell = {"stdout": "some log line\n" + marker + "\n trailing\n"}
        with patch("evidence_board_receipt._read_llm_console",
                   return_value=shell):
            result = read_intelligence_metrics()
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["probe"], "llm_console_v1_status")
        self.assertTrue(result["inference_verified"])
        self.assertEqual(result["latency_ms_p95"], 5230.4)
        self.assertEqual(result["tokens_per_second"], 8.024)

    def test_invalid_or_missing_host_fields_are_unavailable_not_healthy(self) -> None:
        self.assertEqual(summarize_llm_metrics(None)["status"], "unavailable")
        self.assertIsNone(summarize_llm_metrics(None)["memory_pressure_pct"])
        self.assertFalse(summarize_llm_metrics(None)["inference_verified"])
        bad = self._valid_raw()
        bad["model"] = "unknown-model"
        self.assertEqual(summarize_llm_metrics(bad)["status"], "unavailable")
        bad = self._valid_raw()
        bad["ready"] = "yes"
        self.assertEqual(summarize_llm_metrics(bad)["status"], "unavailable")
        bad = self._valid_raw()
        bad["status_http"] = 503
        self.assertEqual(summarize_llm_metrics(bad)["status"], "unavailable")
        bad = self._valid_raw()
        bad["memory"] = {"current_mb": 999999999, "limit_mb": 4088}
        clean = summarize_llm_metrics(bad)
        self.assertEqual(clean["status"], "observed")
        self.assertIsNone(clean["memory"]["current_mb"])
        self.assertIsNone(clean["memory_pressure_pct"])
        bad = self._valid_raw()
        bad["repair_state"] = "tampered"
        self.assertIsNone(summarize_llm_metrics(bad)["repair_state"])
        bad = self._valid_raw()
        bad["db_pressure"] = "exploding"
        self.assertIsNone(summarize_llm_metrics(bad)["db_pressure"])

    def test_unemitted_generation_fields_stay_null(self) -> None:
        clean = summarize_llm_metrics(self._valid_raw())
        self.assertIsNone(clean["latency_ms_p50"])
        self.assertIsNone(clean["tokens_per_second"])
        self.assertIsNone(clean["cpu_pct"])
        self.assertIsNone(clean["dns_ok"])
        self.assertIsNone(clean["db_pressure"])

    def test_console_marker_is_parsed_and_nothing_else_is_trusted(self) -> None:
        import json

        marker = LLM_METRICS_MARKER + json.dumps(self._valid_raw())
        shell = {"stdout": "some log line\n" + marker + "\n trailing\n"}
        with patch("evidence_board_receipt._read_llm_console",
                   return_value=shell):
            result = read_intelligence_metrics()
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["probe"], "llm_console_v1_status")
        self.assertIsNotNone(result["captured_at"])
        self.assertFalse(result["inference_verified"])
        with patch("evidence_board_receipt._read_llm_console",
                   return_value={"stdout": "no marker here\n"}):
            self.assertEqual(read_intelligence_metrics()["status"], "unavailable")
        with patch("evidence_board_receipt._read_llm_console",
                   return_value=None):
            self.assertEqual(read_intelligence_metrics()["status"], "unavailable")
        with patch("evidence_board_receipt._read_llm_console",
                   return_value={"stdout": LLM_METRICS_MARKER + "{not json"}):
            self.assertEqual(read_intelligence_metrics()["status"], "unavailable")


class LlmGenerationProbeTests(unittest.TestCase):
    """The console probe program itself, run against a stub gateway.

    The program runs inside the LFM3 container in production; here it runs
    in a subprocess against a local stub that mimics the gateway's
    authenticated /v1/status and /v1/research routes.
    """

    TOKEN = "s" * 48

    def setUp(self) -> None:
        self.mode = "ok"
        self.research_calls = 0
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _auth_ok(self):
                return (self.headers.get("Authorization", "")
                        == "Bearer " + LlmGenerationProbeTests.TOKEN)

            def _json(self, data, status=200):
                payload = json.dumps(data).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                if self.path != "/v1/status":
                    self._json({"error": "not_found"}, 404)
                    return
                if not self._auth_ok():
                    self._json({"error": "unauthorized"}, 401)
                    return
                self._json({
                    "model": "LiquidAI/LFM3-1.7B-GGUF:Q4_K_M",
                    "ready": True, "source_sha": "b" * 40,
                    "repair_state": "healthy", "repair_count": 0,
                    "runtime_context_tokens": 4096,
                    "memory": {"current_mb": 1190, "limit_mb": 4088,
                               "peak_mb": 1300, "oom_kills": 0},
                })

            def do_POST(self):
                if self.path != "/v1/research":
                    self._json({"error": "not_found"}, 404)
                    return
                if not self._auth_ok():
                    self._json({"error": "unauthorized"}, 401)
                    return
                test.research_calls += 1
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                if test.mode == "busy_then_ok" and test.research_calls == 1:
                    self._json({"error": "inference_busy"}, 429)
                    return
                if test.mode == "error_503":
                    self._json({"error": "inference_failed",
                                "failure_kind": "backend_timeout"}, 503)
                    return
                time.sleep(0.05)
                grounded = {
                    "model": "LiquidAI/LFM3-1.7B-GGUF:Q4_K_M",
                    "direction": "uncertain", "confidence": "low",
                    "grounding_status": "insufficient_evidence",
                    "grounded_by": "deterministic_historical_evidence_policy",
                    "informational_only": True, "uncalibrated": True,
                }
                if test.mode == "ungrounded":
                    grounded["direction"] = "up"
                    grounded["confidence"] = "high"
                if test.mode != "no_usage":
                    grounded["usage"] = {"prompt_tokens": 180,
                                         "completion_tokens": 42,
                                         "total_tokens": 222}
                self._json(grounded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _run_probe(self, token=TOKEN):
        env = dict(os.environ)
        env["RIVAL_LLM_PORT"] = str(self.port)
        env["RIVAL_LLM_SHARED_TOKEN"] = token
        return subprocess.run(
            [sys.executable, "-c", LLM_GENERATION_PROBE_PROGRAM],
            env=env, capture_output=True, text=True, timeout=90,
            check=False,
        )

    def _marker(self, stdout):
        for line in stdout.splitlines():
            if line.startswith(LLM_METRICS_MARKER):
                return json.loads(line[len(LLM_METRICS_MARKER):])
        return None

    def test_probe_verifies_real_authenticated_generation(self):
        result = self._run_probe()
        self.assertEqual(result.returncode, 0, result.stderr)
        marker = self._marker(result.stdout)
        self.assertIsNotNone(marker)
        self.assertTrue(marker["inference_verified"])
        self.assertEqual(marker["generation_probe"],
                         "authenticated_generation")
        self.assertGreater(marker["latency_ms_p50"], 0)
        self.assertEqual(marker["latency_ms_p50"], marker["latency_ms_p95"])
        self.assertGreater(marker["tokens_per_second"], 0)
        self.assertLess(marker["tokens_per_second"], 5000)
        self.assertEqual(marker["generation_errors"], 0)
        self.assertEqual(marker["requests_sampled"], 1)
        self.assertEqual(marker["status_http"], 200)
        self.assertEqual(marker["model"], "LiquidAI/LFM3-1.7B-GGUF:Q4_K_M")

    def test_probe_retries_once_when_gateway_busy(self):
        self.mode = "busy_then_ok"
        result = self._run_probe()
        self.assertEqual(result.returncode, 0, result.stderr)
        marker = self._marker(result.stdout)
        self.assertTrue(marker["inference_verified"])
        self.assertEqual(self.research_calls, 2)

    def test_probe_reports_failure_without_poisoning_status(self):
        self.mode = "error_503"
        result = self._run_probe()
        self.assertEqual(result.returncode, 0, result.stderr)
        marker = self._marker(result.stdout)
        self.assertFalse(marker["inference_verified"])
        self.assertEqual(marker["generation_errors"], 1)
        self.assertEqual(marker["status_http"], 200)
        self.assertTrue(marker["ready"])
        self.assertNotIn("generation_probe", marker)
        self.assertNotIn("latency_ms_p95", marker)

    def test_probe_rejects_ungrounded_generation(self):
        self.mode = "ungrounded"
        result = self._run_probe()
        self.assertEqual(result.returncode, 0, result.stderr)
        marker = self._marker(result.stdout)
        self.assertFalse(marker["inference_verified"])
        self.assertEqual(marker["generation_errors"], 1)

    def test_probe_verifies_without_usage_but_reports_no_rate(self):
        self.mode = "no_usage"
        result = self._run_probe()
        self.assertEqual(result.returncode, 0, result.stderr)
        marker = self._marker(result.stdout)
        self.assertTrue(marker["inference_verified"])
        self.assertNotIn("tokens_per_second", marker)

    def test_probe_fails_closed_with_wrong_token(self):
        result = self._run_probe(token="w" * 48)
        self.assertIsNone(self._marker(result.stdout))

    def test_metrics_command_carries_the_probe_program(self):
        import base64
        import re

        match = re.search(
            r"base64\.b64decode\('([^']+)'\)", LLM_METRICS_CMD)
        self.assertIsNotNone(match)
        decoded = base64.b64decode(match.group(1)).decode("utf-8")
        self.assertEqual(decoded, LLM_GENERATION_PROBE_PROGRAM)


class CombinedPayloadBudgetTests(unittest.TestCase):
    """Delivery 4: the D1+D2+D3 sections together must fit the collector's
    80,000-byte cap. Before the Delivery 4 budget (40 PRs / 45 runs) a
    worst-case realistic receipt measured 106,820 bytes and even a live-like
    one 82,629 bytes -- both rejected with 413, losing that publication.
    The budget trims counts, not evidence: 20 RivaL PRs (every pinned PR
    plus the newest others), 12 runs, 8 Intelligence PRs / runs, and
    80-char gate failures. The ceiling here is 76,000 bytes so a worst-case
    run keeps >=4KB headroom."""

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

        def fake_intelligence(url: str, token: str, **kwargs: object) -> object:
            if url.startswith("/commits/main"):
                return {"sha": "c" * 40}
            if url.startswith("/pulls?"):
                return [self._max_pr(3000 + i) for i in range(12)]
            if url.startswith("/actions/runs"):
                return {"workflow_runs": [self._max_run(45000000000 + i)
                                          for i in range(15)]}
            raise AssertionError(f"unexpected url {url}")

        with patch("evidence_board_github._github_json",
                   side_effect=fake_github):
            github = read_github_snapshot("short-lived-token")
        with patch("evidence_board_github._github_json",
                   side_effect=fake_intelligence):
            intelligence = read_public_intelligence("short-lived-token")
        metrics = summarize_llm_metrics({
            "model": "LiquidAI/LFM3-1.7B-GGUF:lfm3-q4_k_m", "ready": True,
            "source_sha": "c" * 40, "repair_state": "healthy",
            "repair_count": 999, "runtime_context_tokens": 999999,
            "memory": {"current_mb": 3999, "limit_mb": 4088,
                       "peak_mb": 4088, "anon_mb": 3999, "file_mb": 99,
                       "file_mapped_mb": 99, "gateway_rss_mb": 999,
                       "backend_rss_mb": 999},
            "status_latency_ms": 999.9, "status_http": 200,
            "latency_ms_p50": 999.9, "latency_ms_p95": 999.9,
            "tokens_per_second": 999.9, "requests_sampled": 99999,
            "generation_errors": 999, "fallback_count": 999,
            "cpu_pct": 99.9, "dns_ok": True, "upstream_ok": True,
            "db_pressure": "high", "captured_at": self.NOW})
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
               "DISCLOUD_LLM_APP_ID": "fake-app",
               "RIVAL_DRIVE_REPORTS_FOLDER_ID": "synthetic-test-folder-id-0",
               "GITHUB_EVENT_NAME": "schedule", "GITHUB_RUN_ID": "123456789",
               "GITHUB_RUN_ATTEMPT": "1"}
        with patch.dict(os.environ, env):
            sources = build_source_details(
                host, db, drive, github, intelligence,
                {"status": "online", "inference_verified": False},
                performance, {}, self.NOW)
            payload = {
                "schema_version": 1, "captured_at": self.NOW,
                "collection": _collection_block(), "sources": sources,
                "host": host, "database": db, "drive": drive,
                "github": github, "intelligence": intelligence,
                "intelligence_host": {"status": "online",
                                      "inference_verified": False},
                "intelligence_metrics": metrics,
                "performance": performance}
        self.assertEqual(len(github["prs"]), 20)
        self.assertTrue({p["number"] for p in github["prs"]} >= set(PINNED_PRS))
        self.assertEqual(len(github["runs"]), 12)
        self.assertEqual(len(intelligence["prs"]), 8)
        self.assertEqual(len(intelligence["runs"]), 8)
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.assertLessEqual(
            len(body), 76000,
            f"worst-case combined receipt is {len(body)} bytes; "
            "the collector rejects anything over 80000")


if __name__ == "__main__":
    unittest.main()
