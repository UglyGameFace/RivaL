"""Ensure production actually ships and can run the host acceptance probe."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_deploy_archive_contains_host_acceptance_probe():
    workflow = (ROOT / ".github/workflows/discloud-deploy.yml").read_text()
    assert (ROOT / "tools/unattended_acceptance.py").is_file()
    assert "test -f tools/unattended_acceptance.py" in workflow
    assert "            tools/unattended_acceptance.py" in workflow.split("glob: |", 1)[1]


def test_standalone_discloud_probe_runs_without_installed_package(tmp_path):
    # Reproduce the Discloud in-place source tree, without PYTHONPATH or
    # installing an editable Python project. Missing proof MUST fail closed.
    env = dict(os.environ)
    env.update({
        "RIVAL_STORAGE_BACKEND": "sqlite",
        "RIVAL_DB_PATH": str(tmp_path / "empty.sqlite"),
        "RIVAL_DATABASE_URL": "",
        "PYTHONPATH": "",
    })
    result = subprocess.run(
        [sys.executable, "tools/unattended_acceptance.py"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=25, check=False,
    )
    assert result.returncode == 2, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "UNVERIFIED"
    assert "ModuleNotFoundError" not in result.stderr
