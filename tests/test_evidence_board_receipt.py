"""Tests for the public-safe RivaL evidence publisher (no network or secrets)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from evidence_board_receipt import (
    _nonnegative,
    read_drive,
    summarize_host,
    summarize_xmarket_report,
)


class ReceiptTests(unittest.TestCase):
    def test_missing_host_is_unknown_not_zero(self) -> None:
        host, db = summarize_host(None)
        self.assertEqual(host["status"], "unavailable")
        self.assertIsNone(host["xmarket_imported_scopes"])
        self.assertIsNone(host["xmarket_shadow_rows"])
        self.assertIsNone(db["qualified_paper_scopes"])
        self.assertIsNone(db["weight_updates_observed"])

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

    def test_no_drive_folder_never_touches_provider(self) -> None:
        with patch.dict(os.environ, {"RIVAL_DRIVE_REPORTS_FOLDER_ID": ""}):
            value = read_drive()
        self.assertEqual(value["status"], "unavailable")
        self.assertIsNone(value["reports_considered"])
        self.assertEqual(value["reports"], [])


if __name__ == "__main__":
    unittest.main()
