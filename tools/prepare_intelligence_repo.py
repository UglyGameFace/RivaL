"""One-way source-only mirror from RivaL main to the standalone Intelligence repo.

Never mirror credentials, GGUF weights, build products or another project's files.
The downstream repository keeps unrelated, owner-managed files untouched.
"""
from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
SERVICE = Path("services/rival-llm")
SERVICE_FILES = (
    ".discloudignore",
    ".env.example",
    "benchmark.py",
    "benchmark_cases.json",
    "build.sh",
    "discloud.config",
    "discovery.py",
    "error_report.py",
    "main.py",
    "model_download.py",
    "requirements.txt",
    "service.py",
    "voice_moderation.py",
    "validate_package.py",
)
OTHER_FILES = {
    "src/parlay_bot/perpetuals/evidence_policy.py": "evidence_policy.py",
}
TEMPLATE_FILES = {
    "services/rival-llm/standalone-template/README.md": "README.md",
    "services/rival-llm/standalone-template/verify_live.py": "verify_live.py",
    "services/rival-llm/standalone-template/.github/workflows/release.yml":
        ".github/workflows/release.yml",
}
STAMP = "UPSTREAM_COMMIT"
MANIFEST = "UPSTREAM_MANAGED_FILES"


def safe_relative(path: str) -> Path:
    pure = PurePosixPath(path)
    if not path or pure.is_absolute() or ".." in pure.parts or "\\" in path:
        raise ValueError("Unsafe managed repository path")
    if path in (".git", ".env") or "/.git/" in f"/{path}/":
        raise ValueError("Refusing to manage credentials or git internals")
    return Path(*pure.parts)


def checked_destination(repository: Path, name: str) -> Path:
    relative = safe_relative(name)
    path = repository
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("Managed output must not contain a symlink")
    return path


def managed_paths() -> dict[str, str]:
    files = {str(SERVICE / name): name for name in SERVICE_FILES}
    files.update(OTHER_FILES)
    files.update(TEMPLATE_FILES)
    return files


def prepare(source: Path, destination: Path, sha: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Upstream source must be an exact 40-character commit SHA")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Staging directory must start empty")
    destination.mkdir(parents=True, exist_ok=True)
    owned = set()
    for origin, target in managed_paths().items():
        source_file = source / safe_relative(origin)
        if source_file.is_symlink() or not source_file.is_file():
            raise FileNotFoundError(f"Missing or linked authoritative file: {origin}")
        target_path = destination / safe_relative(target)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_file, target_path)
        owned.add(target)
    (destination / STAMP).write_text(sha + "\n", encoding="ascii")
    owned.add(STAMP)
    owned.add(MANIFEST)
    (destination / MANIFEST).write_text(
        "".join(name + "\n" for name in sorted(owned)), encoding="utf-8"
    )


def apply(staged: Path, repository: Path) -> None:
    if not (repository / ".git").exists():
        raise ValueError("Target must be a cloned Git repository")
    expected = {
        entry for entry in (staged / MANIFEST).read_text(encoding="utf-8").splitlines()
        if entry
    }
    actual = {
        path.relative_to(staged).as_posix()
        for path in staged.rglob("*") if path.is_file()
    }
    if expected != actual:
        raise ValueError("Staged source differs from its managed-file manifest")
    previous = checked_destination(repository, MANIFEST)
    if previous.is_file():
        old_owned = set(previous.read_text(encoding="utf-8").splitlines())
        # An unrelated RivaL/main commit changes the upstream SHA but not this
        # service. Do not rebuild/redeploy a multi-GB model for that event.
        stable = expected - {STAMP}
        stamp = checked_destination(repository, STAMP)
        stamp_valid = (
            stamp.is_file()
            and re.fullmatch(r"[0-9a-f]{40}", stamp.read_text(encoding="ascii").strip())
        )
        if old_owned == expected and stamp_valid and all(
            checked_destination(repository, name).is_file()
            and (checked_destination(repository, name).read_bytes()
                 == (staged / safe_relative(name)).read_bytes())
            for name in stable
        ):
            return
        for old in old_owned:
            obsolete = checked_destination(repository, old)
            if old not in expected and obsolete.is_file():
                obsolete.unlink()
    for name in sorted(expected):
        src = staged / safe_relative(name)
        dst = checked_destination(repository, name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


def main() -> None:
    parser = argparse.ArgumentParser()
    action = parser.add_subparsers(dest="action", required=True)
    prep = action.add_parser("prepare")
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--source-sha", required=True)
    update = action.add_parser("apply")
    update.add_argument("--staged", type=Path, required=True)
    update.add_argument("--repository", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(ROOT, args.output, args.source_sha)
    else:
        apply(args.staged, args.repository)


if __name__ == "__main__":
    main()
