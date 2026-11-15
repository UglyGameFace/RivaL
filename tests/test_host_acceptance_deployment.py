"""Ensure production actually ships and can run the host acceptance probe."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_native_discloud_autodeploy_is_only_production_writer():
    workflow = (ROOT / ".github/workflows/discloud-deploy.yml").read_text()
    assert (ROOT / "discloud.config").is_file()
    assert (ROOT / "main.py").is_file()
    assert (ROOT / "tools/unattended_acceptance.py").is_file()
    assert (ROOT / "src/parlay_bot/perpetuals/acceptance_cli.py").is_file()
    assert "discloud/deploy-action" not in workflow
    assert "Deploy tested RivaL app" not in workflow
    assert "python tools/discloud_native_release.py" in workflow
    assert "Verify native Discloud source parity" in workflow


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


def test_native_release_check_requires_successful_current_main_ci():
    workflow = (ROOT / ".github/workflows/discloud-deploy.yml").read_text()
    assert 'workflows: ["CI"]' in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert '"$SOURCE_BRANCH" != "main"' in workflow
    assert '"$SOURCE_REPO" != "$GITHUB_REPOSITORY"' in workflow
    assert '"$SOURCE_SHA" != "$current_sha"' in workflow
    assert "cancel-in-progress: false" in workflow
    assert "sleep 900" not in workflow
    assert "deploy-action" not in workflow
