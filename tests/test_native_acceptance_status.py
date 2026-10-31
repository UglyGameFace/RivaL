from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.native_acceptance_status import MARKER, latest_report, render_status

RUN = "https://github.com/UglyGameFace/RivaL/actions/runs/123"


class NativeAcceptanceStatusTests(unittest.TestCase):
    def test_missing_report_never_claims_success(self):
        body = render_status(None, RUN)
        self.assertIn(MARKER, body)
        self.assertIn("BLOCKED", body)
        self.assertIn("not a successful training run", body)
        self.assertIn("NO CALL", body)

    def test_incomplete_months_are_explicit(self):
        body = render_status({
            "status": "source_months_incomplete",
            "venue": "kucoin",
            "published_models": 0,
            "restored_manifests": 2,
            "source_scopes": [
                {"contract": "BTCUSDT", "quote": "USDT",
                 "verified_full_months": 2,
                 "missing_or_incomplete_months": ["2026-06", "2026-07"]}
            ],
        }, RUN)
        self.assertIn("2/6 complete months", body)
        self.assertIn("2026-06, 2026-07", body)
        self.assertIn("production", body.lower())
        self.assertNotIn("RESEARCH PUBLISHED", body)

    def test_research_publication_does_not_claim_production_import(self):
        body = render_status({
            "status": "verified_research_published", "venue": "okx",
            "published_models": 3, "restored_manifests": 6,
        }, RUN)
        self.assertIn("RESEARCH PUBLISHED", body)
        self.assertIn("Production import remains unverified", body)
        self.assertIn("21 UTC days", body)
        self.assertIn("NO CALL", body)

    def test_zero_publication_never_passes(self):
        body = render_status({
            "status": "verified_research_published", "venue": "okx",
            "published_models": 0,
        }, RUN)
        self.assertIn("BLOCKED", body)

    def test_bounded_report_reads_final_checksum_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = {
                "schema_version": "rival-native-source-research-v1",
                "status": "source_months_incomplete",
            }
            (root / "source-okx-research-deadbeef.json").write_text(
                json.dumps(report), encoding="utf-8")
            self.assertEqual(latest_report(root), report)

    def test_rejects_invalid_report_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source-okx-research-deadbeef.json").write_text(
                '{"status":"verified_research_published"}', encoding="utf-8")
            with self.assertRaises(ValueError):
                latest_report(root)


if __name__ == "__main__":
    unittest.main()
