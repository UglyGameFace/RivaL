"""Ensure production actually ships and can run the host acceptance probe."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_deploy_archive_contains_host_acceptance_probe():
    workflow = (ROOT / ".github/workflows/discloud-deploy.yml").read_text()
    assert (ROOT / "tools/unattended_acceptance.py").is_file()
    assert "test -f tools/unattended_acceptance.py" in workflow
    assert "            tools/unattended_acceptance.py" in workflow.split("glob: |", 1)[1]
    assert (ROOT / "src/parlay_bot/perpetuals/acceptance_cli.py").is_file()
    assert "            src/**" in workflow.split("glob: |", 1)[1]


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


def test_discloud_main_entrypoint_works_even_if_tools_directory_is_missing(tmp_path):
    """Acceptance runs from the exact app launch layout with tools/ omitted.

    Do not depend on a passing GitHub upload as proof tools/ shipped.
    """
    shipped = tmp_path / "discloud-app"
    shipped.mkdir()
    shutil.copy(ROOT / "main.py", shipped / "main.py")
    shutil.copytree(ROOT / "src", shipped / "src", ignore=shutil.ignore_patterns(
        "__pycache__", "*.pyc",
    ))
    assert not (shipped / "tools").exists()
    env = dict(os.environ)
    env.update({
        "RIVAL_STORAGE_BACKEND": "sqlite",
        "RIVAL_DB_PATH": str(tmp_path / "empty.sqlite"),
        "RIVAL_DATABASE_URL": "",
        "PYTHONPATH": "",
    })
    for option in ("--acceptance", "--health", "--mark-restart", "--verify-restart"):
        run = subprocess.run(
            [sys.executable, "main.py", option], cwd=shipped, env=env,
            capture_output=True, text=True, timeout=30, check=False,
        )
        assert run.returncode == 2, (option, run.stderr, run.stdout)
        result = json.loads(run.stdout)
        assert result["status"] == "UNVERIFIED", (option, result)
        assert "ModuleNotFoundError" not in run.stderr
