"""Read-only, source-paired Brier proof without model promotion or trading."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from evidence_board_github import read_public_intelligence
from evidence_board_receipt import (
    read_intelligence_host,
    read_paired_performance,
)
from parlay_bot.perpetuals.evidence_score_receipt import (
    MODEL,
    summarize_rows,
)


class ModelScoreEvidenceTests(unittest.TestCase):
    def test_brier_is_paired_on_same_causal_rows(self) -> None:
        rows = [
            {"venue": "v", "contract": "BTC-USD", "quote": "USD",
             "horizon_minutes": 5,
             "target_opened_at": "2026-11-12T17:10:00+00:00",
             "issued_at": "2026-11-12T17:04:00+00:00",
             "model_brier": 0.20, "baseline_brier": 0.25},
            {"venue": "v", "contract": "BTC-USD", "quote": "USD",
             "horizon_minutes": 5,
             "target_opened_at": "2026-11-12T17:15:00+00:00",
             "issued_at": "2026-11-12T17:09:00+00:00",
             "model_brier": 0.30, "baseline_brier": 0.35},
            {"venue": "v", "contract": "BTC-USD", "quote": "USD",
             "horizon_minutes": 5,
             "target_opened_at": "2026-11-12T17:15:00+00:00",
             "issued_at": "2026-11-12T17:11:00+00:00",
             "model_brier": 0.01, "baseline_brier": 0.99},
        ]
        scores = summarize_rows("BTC", rows)
        self.assertEqual(scores[0]["paired_outcomes"], 2)
        self.assertEqual(scores[0]["model_brier"], 0.25)
        self.assertEqual(scores[0]["baseline_brier"], 0.30)
        self.assertEqual(scores[0]["model_version"], MODEL)
        self.assertFalse(scores[0]["after_cost_qualified"])

    def test_excludes_noncausal_and_invalid(self) -> None:
        row = {"venue": "v", "contract": "BTC-USD", "quote": "USD",
               "horizon_minutes": 15,
               "target_opened_at": "2026-11-12T17:10:00+00:00",
               "issued_at": "2026-11-12T17:12:00+00:00",
               "model_brier": 0.4, "baseline_brier": 0.5}
        self.assertEqual(summarize_rows("BTC", [row]), [])
        self.assertEqual(summarize_rows("BTC", [{**row, "issued_at":
                         "2026-11-12T17:05:00+00:00", "model_brier": 3.0}]), [])
        with self.assertRaises(ValueError):
            summarize_rows("DOGE", [])

    def test_failed_optional_discloud_probe_remains_unavailable(self) -> None:
        with patch("evidence_board_receipt._read_host_console", return_value=None):
            result = read_paired_performance()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["scopes"], [])

    def test_unavailable_intelligence_app_is_not_reported_offline(self) -> None:
        with patch.dict("os.environ", {"DISCLOUD_LLM_APP_ID": "",
                                        "DISCLOUD_TOKEN": ""}):
            result = read_intelligence_host()
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(result["inference_verified"])

    def test_public_intelligence_ci_does_not_prove_inference(self) -> None:
        fake_sha = "a" * 40
        def fake(path: str, token: str, **kwargs: object) -> object:
            self.assertEqual(kwargs["repository"], "UglyGameFace/RivaL-Intelligence")
            if path.startswith("/commits/"):
                return {"sha": fake_sha}
            if path.startswith("/pulls?"):
                return [{"number": 10, "title": "release", "state": "closed",
                         "merged_at": "2026-11-10T18:00:00Z",
                         "body": "do not copy secrets"}]
            return {"workflow_runs": [{"id": 42880099795,
                     "name": "RivaL Intelligence CI and Discloud release",
                     "status": "completed", "conclusion": "success"}]}
        with patch("evidence_board_github._github_json", side_effect=fake):
            result = read_public_intelligence("token")
        self.assertEqual(result["status"], "verified")
        self.assertFalse(result["runtime_inference_verified"])
        self.assertEqual(len(result["runs"]), 1)
        self.assertNotIn("secrets", str(result))
        self.assertEqual(result["head_sha"], fake_sha)


if __name__ == "__main__":
    unittest.main()
