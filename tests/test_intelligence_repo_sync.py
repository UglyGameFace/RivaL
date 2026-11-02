"""Standalone Intelligence repository: source provenance, safe mirror and live receipt."""
from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1]
SYNC = runpy.run_path(str(SOURCE / "tools/prepare_intelligence_repo.py"))
VERIFY = runpy.run_path(str(
    SOURCE / "services/rival-llm/standalone-template/verify_live.py"
))
SHA = "a" * 40


@pytest.fixture
def fake_source(tmp_path):
    root = tmp_path / "source"
    for origin in SYNC["managed_paths"]():
        file = root / origin
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(f"source:{origin}", encoding="utf-8")
    (root / "services/rival-llm/.env").write_text("PRIVATE=never")
    (root / "services/rival-llm/runtime/models").mkdir(parents=True)
    (root / "services/rival-llm/runtime/models/model.gguf").write_bytes(b"private")
    return root


def test_stage_exact_source_without_credentials_binary_or_weights(fake_source, tmp_path):
    staged = tmp_path / "stage"
    SYNC["prepare"](fake_source, staged, SHA)
    owned = set((staged / "UPSTREAM_MANAGED_FILES").read_text().splitlines())
    assert owned == {
        file.relative_to(staged).as_posix()
        for file in staged.rglob("*") if file.is_file()
    }
    assert (staged / "UPSTREAM_COMMIT").read_text() == SHA + "\n"
    assert (staged / ".github/workflows/release.yml").is_file()
    assert (staged / "evidence_policy.py").is_file()
    assert not (staged / ".env").exists()
    assert not (staged / "runtime").exists()
    assert not list(staged.rglob("*.gguf"))
    assert not list(staged.rglob("*.zip"))
    with pytest.raises(ValueError, match="empty"):
        SYNC["prepare"](fake_source, staged, SHA)


def test_source_commit_must_be_immutable_and_valid(fake_source, tmp_path):
    for bad in ("main", "f" * 39, "z" * 40, "../" + SHA):
        with pytest.raises(ValueError, match="exact"):
            SYNC["prepare"](fake_source, tmp_path / ("bad-" + str(len(bad))), bad)


def test_mirror_does_not_remove_unrelated_owner_files(fake_source, tmp_path):
    stage = tmp_path / "stage"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "NOTES.md").write_text("owner-owned", encoding="utf-8")
    SYNC["prepare"](fake_source, stage, SHA)
    SYNC["apply"](stage, repo)
    assert (repo / "NOTES.md").read_text() == "owner-owned"
    assert (repo / "main.py").read_text().endswith("services/rival-llm/main.py")
    assert (repo / "UPSTREAM_COMMIT").read_text() == SHA + "\n"
    (repo / "main.py").write_text("stale downstream code")
    SYNC["apply"](stage, repo)
    assert (repo / "main.py").read_text().endswith("services/rival-llm/main.py")


def test_mirror_retires_only_old_managed_files(fake_source, tmp_path):
    stage = tmp_path / "stage"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    SYNC["prepare"](fake_source, stage, SHA)
    SYNC["apply"](stage, repo)
    (repo / "manual.txt").write_text("retain")
    (stage / "benchmark.py").unlink()
    manifest = stage / "UPSTREAM_MANAGED_FILES"
    manifest.write_text(
        "\n".join(x for x in manifest.read_text().splitlines() if x != "benchmark.py") + "\n"
    )
    SYNC["apply"](stage, repo)
    assert not (repo / "benchmark.py").exists()
    assert (repo / "manual.txt").read_text() == "retain"


def test_mirror_rejects_unsafe_old_paths_and_target_symlinks(fake_source, tmp_path):
    stage = tmp_path / "stage"
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    SYNC["prepare"](fake_source, stage, SHA)
    (repo / "UPSTREAM_MANAGED_FILES").write_text("../dangerous\n")
    with pytest.raises(ValueError, match="Unsafe"):
        SYNC["apply"](stage, repo)
    (repo / "UPSTREAM_MANAGED_FILES").unlink()
    (repo / "main.py").symlink_to(tmp_path / "somewhere")
    with pytest.raises(ValueError, match="symlink"):
        SYNC["apply"](stage, repo)


def test_live_acceptance_requires_exact_ready_model_source_and_repair_fields():
    receipt = {
        "source_sha": SHA,
        "model": "LiquidAI/LFM3-1.7B-GGUF:Q4_K_M",
        "ready": True,
        "repair_state": "healthy",
        "repair_count": 1,
    }
    parser = VERIFY["parse_receipt"]
    line = VERIFY["MARKER"] + json.dumps(receipt)
    assert parser("noise\n" + line, SHA) == receipt
    for invalid in (
        {"source_sha": "b" * 40},
        {"model": "fake"},
        {"ready": False},
        {"repair_state": "quarantined"},
        {"repair_count": -1},
        {"repair_count": True},
    ):
        bad = {**receipt, **invalid}
        assert parser(VERIFY["MARKER"] + json.dumps(bad), SHA) is None
    assert parser("just a successful job log", SHA) is None
    assert parser(VERIFY["MARKER"] + "not json", SHA) is None
