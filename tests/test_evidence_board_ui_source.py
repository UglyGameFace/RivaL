"""Evidence Board Worker source and DOM-free UI smoke tests."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class EvidenceBoardSourceTests(unittest.TestCase):
    def test_authentic_read_only_receiver(self) -> None:
        script = (ROOT / "web/evidence-board/worker.js").read_text()
        self.assertIn("RIVAL_EVIDENCE_RECEIPTS.fetch", script)
        self.assertIn("req.method!=='GET'&&req.method!=='HEAD'", script)
        self.assertIn("not-in-signed-snapshot", script)
        self.assertIn("publicGithubRunDetails", script)
        self.assertNotIn("return tracked.map(", script)

    def test_embedded_js_with_signed_fixture(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is not installed")
        result = subprocess.run(
            [node, str(ROOT / "tools/test_evidence_board_ui.js")],
            cwd=ROOT, capture_output=True, text=True, timeout=20, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
