"""Validate a DIRECT Discloud upload archive, never a ZIP containing another ZIP.

This validator has no external dependencies. Use it against both the staged
file tree and the exact deployment ZIP before publishing any GitHub artifact.
"""
from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path, PurePosixPath

APP_FILES = {
    "discloud.config",
    "service.py",
    "model_download.py",
    "build.sh",
    "runtime/bin/llama-server",
}
NATIVE_FILES = {
    "runtime/bin/libllama-server-impl.so",
    "runtime/bin/libllama-common.so.0",
    "runtime/bin/libllama.so.0",
    "runtime/bin/libggml.so.0",
    "runtime/bin/libggml-base.so.0",
    "runtime/bin/libggml-cpu.so.0",
    "runtime/bin/libmtmd.so.0",
}
CONFIG_KEYS = {
    "NAME", "TYPE", "MAIN", "RAM", "VERSION", "BUILD", "START",
    "AUTORESTART", "VLAN", "HOSTNAME",
}
EXPECTED = {
    "NAME": "RivaL LLM",
    "TYPE": "bot",
    "MAIN": "service.py",
    "RAM": "2450",
    "VERSION": "3.11",
    "BUILD": "bash build.sh",
    "START": "python -u service.py",
    "AUTORESTART": "true",
    "VLAN": "true",
    "HOSTNAME": "rival-llm",
}
FORBIDDEN_SUFFIXES = (".gguf", ".part", ".zip", ".env", ".pem", ".key")


def parse_config(raw: bytes) -> dict[str, str]:
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("discloud.config must be UTF-8") from exc
    found: dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("Invalid Discloud config property")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z_]*", key) or key not in CONFIG_KEYS:
            raise ValueError(f"Unknown Discloud config key: {key}")
        if key in found:
            raise ValueError(f"Duplicated Discloud config key: {key}")
        found[key] = value.strip()
    if found != EXPECTED:
        mismatch = {
            k: {"expected": v, "actual": found.get(k)}
            for k, v in EXPECTED.items() if found.get(k) != v
        }
        unexpected = sorted(set(found) - set(EXPECTED))
        raise ValueError(f"Discloud config mismatch: {mismatch}, unexpected={unexpected}")
    return found


def check_names(names: set[str]) -> None:
    # Discloud reads the root, not another archive or a single wrapper directory.
    required = APP_FILES | NATIVE_FILES
    missing = sorted(required - names)
    if missing:
        raise ValueError(f"Discloud root files missing: {missing}")
    for name in names:
        path = PurePosixPath(name)
        if name.startswith("/") or ".." in path.parts or "\\" in name:
            raise ValueError("Archive has unsafe path traversal")
        if path.name.startswith(".env") or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            raise ValueError(f"Archive contains credentials, weights, or nested ZIP: {name}")
        if name.startswith("runtime/llama.cpp/"):
            raise ValueError("Build-tree content must not be shipped")


def check_directory(root: Path) -> None:
    if not root.is_dir():
        raise ValueError("Discloud deployment directory missing")
    files: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Artifact cannot preserve symlink: {path.name}")
        if path.is_file():
            files.add(path.relative_to(root).as_posix())
    check_names(files)
    parse_config((root / "discloud.config").read_bytes())
    if (root / "runtime/bin/llama-server").stat().st_size < 15_000:
        raise ValueError("Missing or invalid prebuilt CPU binary")


def check_zip(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        file_names = {
            info.filename for info in archive.infolist() if not info.is_dir()
        }
        check_names(file_names)
        parse_config(archive.read("discloud.config"))
        binary = archive.getinfo("runtime/bin/llama-server")
        if binary.file_size < 15_000:
            raise ValueError("The server binary is missing or truncated")
        if archive.testzip() is not None:
            raise ValueError("Corrupt deployment ZIP")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    if args.target.is_dir():
        check_directory(args.target)
    else:
        check_zip(args.target)
    print("PASS: direct Discloud ZIP/root, parser-safe config, executable and libraries")


if __name__ == "__main__":
    main()
