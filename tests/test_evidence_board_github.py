"""Unit tests for the authenticated, public-only GitHub evidence mirror."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from evidence_board_github import (
    PINNED_PRS,
    read_github_snapshot,
    safe_pr,
    safe_run,
)


class GithubSnapshotTests(unittest.TestCase):
    def test_missing_token_is_unavailable_not_a_empty_success(self) -> None:
        with patch("evidence_board_github._github_json") as http:
            snap = read_github_snapshot("")
        self.assertEqual(snap["status"], "unavailable")
        self.assertIsNone(snap["fetched_at"])
        http.assert_not_called()

    def test_public_fields_only_and_source_urls_canonical(self) -> None:
        x = safe_pr({
            "number": 542, "title": "Evidence board",
            "state": "closed", "merged_at": "2026-11-12T21:41:23Z",
            "merge_commit_sha": "a" * 40,
            "head": {"sha": "b" * 40}, "base": {"sha": "c" * 40},
            "body": "PRIVATE TOKEN DO NOT PUBLISH",
            "email": "person@example.com",
            "html_url": "https://example.com/evil",
        })
        self.assertEqual(x["merged_at"], "2026-11-12T21:41:23Z")
        self.assertEqual(x["html_url"], "https://github.com/UglyGameFace/RivaL/pull/542")
        self.assertNotIn("PRIVATE TOKEN", json.dumps(x))
        self.assertNotIn("example.com", json.dumps(x))
        self.assertNotIn("email", x)
        self.assertIsNone(safe_pr({"number": True}))

    def test_run_has_no_secrets_or_logs(self) -> None:
        row = safe_run({
            "id": 43312718711, "name": "CI", "status": "completed",
            "conclusion": "success", "head_sha": "b" * 40,
            "created_at": "2026-11-12T21:41:25Z",
            "logs_url": "secret-log-url", "actor": {"login": "private"},
            "run_attempt": 1,
        })
        self.assertEqual(row["conclusion"], "success")
        self.assertNotIn("actor", row)
        self.assertNotIn("secret", json.dumps(row))

    def test_signed_job_can_collect_complete_bounded_public_snapshot(self) -> None:
        pulls = [
            {"number": n, "title": f"PR {n}", "state": "closed",
             "merged_at": "2026-11-12T20:00:00Z"}
            for n in PINNED_PRS
        ]
        runs = [{"id": 1, "name": "CI", "status": "completed",
                 "conclusion": "success", "created_at": "2026-11-12T20:00:00Z"}]

        def fake(url: str, token: str, **kwargs: object) -> object:
            self.assertEqual(token, "short-lived-token")
            return pulls if url.startswith("/pulls?") else {"workflow_runs": runs}

        with patch("evidence_board_github._github_json", side_effect=fake) as http:
            snap = read_github_snapshot("short-lived-token")
        self.assertEqual(http.call_count, 2)
        self.assertEqual(snap["status"], "verified")
        self.assertEqual(len(snap["prs"]), len(PINNED_PRS))
        self.assertEqual(len(snap["runs"]), 1)
        self.assertIn("fetched_at", snap)

    def test_schema_failure_does_not_mark_a_snapshot_verified(self) -> None:
        with patch("evidence_board_github._github_json", side_effect=[[], {}]):
            with self.assertRaises(TypeError):
                read_github_snapshot("short-lived-token")


if __name__ == "__main__":
    unittest.main()
