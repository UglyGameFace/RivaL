"""Workflow readers must find the content-addressed research reports."""
import re
from pathlib import Path

from parlay_bot.research.production import _report

ROOT = Path(__file__).resolve().parents[1]


def test_report_name_is_hash_suffixed_and_workflow_globs_match_it(tmp_path):
    written = _report(tmp_path / "source-deribit-research.json",
                      {"status": "source_months_incomplete"})
    assert re.fullmatch(r"source-deribit-research-[0-9a-f]{20}\.json", written.name)
    workflow = (ROOT / ".github/workflows/source-native-research.yml").read_text()
    # The OKX fallback and the summary both glob, never the bare exact name.
    assert 'glob(f"source-{sys.argv[1]}-research*.json")' in workflow
    assert 'glob("source-*-research*.json")' in workflow
    assert "source-${VENUE}-research.json" not in workflow
    assert list(tmp_path.glob("source-deribit-research*.json")) == [written]
