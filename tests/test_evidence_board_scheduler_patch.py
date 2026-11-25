"""Delivery 1 scheduler evidence: run-timing diagnosis and the workflow patch.

The patch itself lives outside the repo (the owner places workflow files by hand):
~/workspace/your_files/evidence-board-scheduler-workflow.patch. These tests
assert the diagnosis numbers from the in-repo fixture always, and the patch
contents whenever the patch file is present.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "evidence_board_run_timing_20261117.json"
PATCH = Path(__file__).resolve().parents[2] / "your_files" / "evidence-board-scheduler-workflow.patch"


class SchedulerDiagnosisTests(unittest.TestCase):
    def test_github_cron_delivered_a_fraction_of_scheduled_runs(self) -> None:
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(data["workflow_id"], 416610844)
        self.assertEqual(data["window"]["total_runs"], 299)
        self.assertEqual(data["events"]["schedule"], 15)
        self.assertEqual(data["events"]["workflow_run"], 230)
        self.assertEqual(data["events"]["push"], 54)
        self.assertEqual(data["schedule_runs"]["expected_at_10min_cadence"], 594)
        self.assertAlmostEqual(data["schedule_runs"]["coverage_pct"], 2.5)
        self.assertAlmostEqual(data["schedule_runs"]["gap_hours"]["median"], 5.96)
        self.assertEqual(data["schedule_runs"]["latest"], "2026-11-16T20:55:47Z")
        self.assertEqual(data["conclusions_by_event"]["schedule"], {"success": 15})
        self.assertEqual(data["conclusions_by_event"]["workflow_run"]["failure"], 53)
        self.assertEqual(data["conclusions_by_event"]["workflow_run"]["skipped"], 120)

    def test_flood_does_not_explain_the_missing_schedule_runs(self) -> None:
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(data["flood_source"]["workflow"], "RivaL Production Release")
        self.assertEqual(data["flood_source"]["total_runs"], 4835)
        # The 207-minute queue delay is GitHub-side, not our concurrency group.
        self.assertIn("NOT explained by this group", data["concurrency"]["note"])
        self.assertEqual(data["concurrency"]["group"],
                         "rival-evidence-board-source-receipt")
        self.assertFalse(data["concurrency"]["cancel_in_progress"])

    @unittest.skipUnless(PATCH.exists(), "scheduler patch not present in your_files")
    def test_scheduler_patch_removes_the_flood_trigger(self) -> None:
        content = PATCH.read_text(encoding="utf-8")
        self.assertIn('-    workflows: ["RivaL Production Release", "RivaL independent Discloud uptime"]',
                      content)
        self.assertIn('+    workflows: ["RivaL independent Discloud uptime"]', content)
        self.assertIn('workflows: ["RivaL Production Release"', content)
        # The uptime trigger is the retained fallback, not a removal.
        self.assertNotIn('-    workflows: ["RivaL independent Discloud uptime"]', content)

    @unittest.skipUnless(PATCH.exists(), "scheduler patch not present in your_files")
    def test_scheduler_patch_keeps_cadence_and_uses_per_event_lanes(self) -> None:
        content = PATCH.read_text(encoding="utf-8")
        self.assertIn('group: rival-evidence-board-${{ github.event_name }}', content)
        self.assertIn('cancel-in-progress: false', content)
        # The patch touches neither the cron cadence nor any OIDC/permission line.
        self.assertNotIn('cron:', content)
        self.assertNotIn('\n+id-token', content)
        self.assertNotIn('\n-id-token', content)
        self.assertNotIn('\n+permissions', content)
        self.assertNotIn('\n-permissions', content)
        workflow = (Path(__file__).resolve().parents[1]
                    / ".github/workflows/evidence-board-receipt.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "7,17,27,37,47,57 * * * *"', workflow)


if __name__ == "__main__":
    unittest.main()
