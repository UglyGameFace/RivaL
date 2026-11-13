"""Read-only, source-paired Brier proof without model promotion or trading."""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from evidence_board_github import read_public_intelligence
from evidence_board_receipt import (
    _oidc_contract_summary,
    _receiver_http_error,
    _safe_receiver_error,
    read_intelligence_host,
    read_paired_performance,
    receiver_preflight,
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

    def test_safe_signed_receipt_error_never_leaks_provider_body(self) -> None:
        raw = b'{"error":"unverified_github_identity","reason":"claim_mismatch","secret":"DO_NOT_ECHO"}'
        label = _safe_receiver_error(403, raw)
        self.assertEqual(
            label, "receiver_http_403_unverified_github_identity_claim_mismatch"
        )
        self.assertNotIn("DO_NOT_ECHO", label)
        self.assertEqual(
            _safe_receiver_error(502, b'{"error":"PRIVATE_TOKEN_EXPOSED"}'),
            "receiver_http_502",
        )

    def test_upstream_403_is_not_mistaken_for_oidc_denial(self) -> None:
        err = HTTPError(
            "https://not-logged.example/ingest", 403, "denied",
            {"Content-Type": "text/html"}, io.BytesIO(b"TOP_SECRET_HTML"),
        )
        self.assertEqual(_receiver_http_error(err),
                         "receiver_http_403_non_json_upstream")
        self.assertNotIn("TOP_SECRET", _receiver_http_error(err))

    def test_explicit_worker_oidc_denial_has_only_fixed_labels(self) -> None:
        body = b'{"error":"unverified_github_identity","reason":"claim_mismatch","credential":"DO_NOT_ECHO"}'
        err = HTTPError(
            "https://not-logged.example/ingest", 403, "forbidden",
            {"Content-Type": "application/json; charset=utf-8"}, io.BytesIO(body),
        )
        self.assertEqual(_receiver_http_error(err),
                         "receiver_http_403_unverified_github_identity_claim_mismatch")

    def test_receiver_preflight_rejects_any_non_worker_or_open_ingest(self) -> None:
        health = {"ok": True, "receiver": "rival-evidence-collector"}
        denied = HTTPError(
            "https://not-logged.example/ingest", 401, "unauthorized",
            {"Content-Type": "application/json"},
            io.BytesIO(b'{"error":"authorization_required"}'),
        )
        with patch("evidence_board_receipt._url_json",
                   side_effect=[health, denied]) as fetch:
            receiver_preflight()
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args_list[1].kwargs["body"], b"{}")
        self.assertNotIn("Authorization", fetch.call_args_list[1].kwargs["headers"])

        with patch("evidence_board_receipt._url_json",
                   return_value={"ok": True, "receiver": "someone_else"}):
            with self.assertRaisesRegex(ValueError, "identity_mismatch"):
                receiver_preflight()
        with patch("evidence_board_receipt._url_json", side_effect=[
                health, {"status": "accepted"}]):
            with self.assertRaisesRegex(ValueError, "write_not_rejected"):
                receiver_preflight()
        blocked = HTTPError(
            "https://not-logged.example/ingest", 403, "forbidden",
            {"Content-Type": "text/html"}, io.BytesIO(b"DO_NOT_ECHO"),
        )
        with patch("evidence_board_receipt._url_json",
                   side_effect=[health, blocked]):
            with self.assertRaisesRegex(
                    ValueError, "receiver_anonymous_receiver_http_403_non_json_upstream"):
                receiver_preflight()

    def test_oidc_diagnostic_returns_only_mismatch_field_names(self) -> None:
        import base64

        def make_token(claims: dict) -> str:
            payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
            return "header." + payload + ".signature"

        claims = {
            "iss": "https://token.actions.githubusercontent.com",
            "aud": "https://rival-evidence-collector.staytrippynlw.workers.dev",
            "repository": "UglyGameFace/RivaL",
            "ref": "refs/heads/main",
            "workflow_ref": (
                "UglyGameFace/RivaL/.github/workflows/"
                "evidence-board-receipt.yml@refs/heads/main"
            ),
            "run_id": "43330102811", "event_name": "push",
        }
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "43330102811",
                                     "GITHUB_EVENT_NAME": "push"}):
            self.assertEqual(_oidc_contract_summary(make_token(claims)),
                             "local_oidc_contract_match")
            bad = {**claims, "aud": "https://attacker.example/private-credential"}
            self.assertEqual(_oidc_contract_summary(make_token(bad)),
                             "local_oidc_mismatch_aud")
        self.assertNotIn("attacker", _oidc_contract_summary("bad.token.value"))

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
