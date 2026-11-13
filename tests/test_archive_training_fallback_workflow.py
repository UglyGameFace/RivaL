"""Contract tests for the one-time, main-only XMarket archive-training fallback.

These execute the actual GitHub Actions date guard under bash, without
accessing private Drive, retraining models or installing GitHub Actions.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "archive-model-training.yml"


def _guard_script(day: str) -> str:
    contents = WORKFLOW.read_text()
    section = contents.split("      - name: Check scheduled date gates\n", 1)[1]
    section = section.split("  train:\n", 1)[0]
    script = section.split("        run: |\n", 1)[1]
    script = "\n".join(line[10:] if line.startswith("          ") else line
                       for line in script.splitlines())
    expected = 'today="$(date -u +%F)"'
    assert script.count(expected) == 1
    return script.replace(expected, f'today="{day}"')


@pytest.mark.parametrize(("event", "cron", "day", "expected"), [
    ("push", "", "2026-11-13", "true"),
    ("push", "", "2026-11-14", "false"),
    ("push", "", "2027-11-13", "false"),
    ("schedule", "30 7 * * 0", "2026-11-15", "false"),
    ("schedule", "30 7 * * 0", "2026-11-22", "true"),
    ("workflow_dispatch", "", "2026-11-15", "true"),
])
def test_one_off_xmarket_archive_training_guard(
    tmp_path: Path, event: str, cron: str, day: str, expected: str,
) -> None:
    out = tmp_path / "github-output"
    environment = {
        **os.environ, "EVENT_NAME": event, "EVENT_CRON": cron,
        "GITHUB_OUTPUT": str(out),
    }
    result = subprocess.run(
        ["bash", "-eu", "-c", _guard_script(day)],
        env=environment, capture_output=True, text=True,
        timeout=5, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert out.read_text().splitlines() == [f"should_run={expected}"]


def test_fallback_cannot_trigger_on_arbitrary_source_changes() -> None:
    workflow = WORKFLOW.read_text()
    assert '  push:\n    branches: [main]\n    paths:\n      - ".github/workflows/archive-model-training.yml"' in workflow
    assert '17 15 13 11 *' not in workflow, "expired annual cron must not rerun"
    assert '    - cron: "30 7 * * 0"' in workflow, "weekly training must be preserved"
    assert '          if [[ "${{ github.event_name }}" == "schedule" || "${{ github.event_name }}" == "push" || "${{ inputs.publish }}" == "true" ]]; then' in workflow
    assert '          for horizon in 5 15 60; do' in workflow
    assert '        asset: [BTC, ETH, SOL, XRP]' in workflow
