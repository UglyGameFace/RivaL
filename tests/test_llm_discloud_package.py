"""Discloud upload MUST have config and binary directly at archive root."""
from __future__ import annotations

import io
import runpy
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "services" / "rival-llm"
VALIDATOR = runpy.run_path(str(ROOT / "validate_package.py"))


@pytest.fixture
def staged(tmp_path):
    root = tmp_path / "direct-upload"
    binary_dir = root / "runtime" / "bin"
    binary_dir.mkdir(parents=True)
    for filename in VALIDATOR["APP_FILES"]:
        path = root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        if filename == "discloud.config":
            path.write_bytes((ROOT / filename).read_bytes())
        elif filename == "runtime/bin/llama-server":
            path.write_bytes(b"\x7fELF" + b"b" * 20_000)
        else:
            path.write_bytes(b"# only synthetic fixture data\n")
    for filename in VALIDATOR["NATIVE_FILES"]:
        path = root / filename
        path.write_bytes(b"mock-lib")
    return root


def test_known_discloud_config_and_complete_root_are_valid(staged):
    config = VALIDATOR["parse_config"]((staged / "discloud.config").read_bytes())
    assert config["RAM"] == "2450"
    assert config["VLAN"] == "true"
    assert config["HOSTNAME"] == "rival-llm"
    assert config["MAIN"] == "service.py"
    VALIDATOR["check_directory"](staged)


def test_exact_upload_zip_valid_and_nested_wrapper_zip_rejected(staged, tmp_path):
    path = tmp_path / "ready-to-upload.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for file in staged.rglob("*"):
            if file.is_file():
                archive.write(file, file.relative_to(staged).as_posix())
    VALIDATOR["check_zip"](path)
    wrapper = tmp_path / "wrong-outer-artifact.zip"
    with zipfile.ZipFile(wrapper, "w") as archive:
        archive.write(path, path.name)
    with pytest.raises(ValueError, match="root files missing"):
        VALIDATOR["check_zip"](wrapper)


def test_missing_config_is_fatal_even_when_binaries_exist(staged):
    (staged / "discloud.config").unlink()
    with pytest.raises(ValueError, match="root files missing"):
        VALIDATOR["check_directory"](staged)


def test_discloud_zip_contains_no_secrets_weights_or_nested_archives(staged):
    (staged / "secret.env").write_text("RIVAL_LLM_SHARED_TOKEN=unsafe")
    with pytest.raises(ValueError, match="credentials"):
        VALIDATOR["check_directory"](staged)
    (staged / "secret.env").unlink()
    (staged / "backup.zip").write_bytes(b"nested")
    with pytest.raises(ValueError, match="nested ZIP"):
        VALIDATOR["check_directory"](staged)


def test_invalid_configuration_and_duplicate_keys_fail(staged):
    raw = (staged / "discloud.config").read_bytes()
    with pytest.raises(ValueError, match="Duplicated"):
        VALIDATOR["parse_config"](raw + b"RAM=100\n")
    with pytest.raises(ValueError, match="mismatch"):
        VALIDATOR["parse_config"](raw.replace(b"RAM=2450", b"RAM=100"))
    with pytest.raises(ValueError, match="Unknown"):
        VALIDATOR["parse_config"](raw + b"ROOT=nested\n")


def test_dangling_runtime_symlink_rejected(staged):
    extra = staged / "runtime/bin/libbad.so"
    extra.symlink_to(staged / "does_not_exist")
    with pytest.raises(ValueError, match="symlink"):
        VALIDATOR["check_directory"](staged)


def test_bad_binary_and_archive_paths_rejected(staged, tmp_path):
    (staged / "runtime/bin/llama-server").write_bytes(b"ELF")
    with pytest.raises(ValueError, match="binary"):
        VALIDATOR["check_directory"](staged)
    with pytest.raises(ValueError, match="unsafe path"):
        VALIDATOR["check_names"]({"../other", *VALIDATOR["APP_FILES"],
                                   *VALIDATOR["NATIVE_FILES"]})


def test_invalid_utf8_configuration_rejected():
    with pytest.raises(ValueError, match="UTF-8"):
        VALIDATOR["parse_config"](b"\xff\xfe\x00")
