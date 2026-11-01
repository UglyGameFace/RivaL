"""Private Discloud Python launcher for RivaL's local inference service.

Discloud's standard Python startup requires main.py and requirements.txt.
Only the optional RIVAL_LLM_SHARED_TOKEN can be loaded from a private .env
file. Discloud-provided environment variables always have priority.
"""
from __future__ import annotations

import logging
import os
import re
import stat
from pathlib import Path

from service import launch_server

SECRET_NAME = "RIVAL_LLM_SHARED_TOKEN"
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,256}")


def load_optional_local_secret() -> None:
    """Accept one private .env token without a dependency or unsafe eval.

    This is an alternative to Discloud's environment-variable settings, not
    an instruction to put secrets into git or public deployment artifacts.
    """
    if os.environ.get(SECRET_NAME):
        return
    path = Path(".env")
    if not path.exists():
        return
    if path.is_symlink() or not path.is_file():
        raise ValueError("Private .env must be a regular file, not a link")
    if path.stat().st_size > 4096:
        raise ValueError("Private .env exceeds configuration size limit")
    # Secure the uploaded file before reading its secret.
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    if path.stat().st_mode & 0o077:
        raise ValueError("Private .env permissions cannot be restricted")
    seen = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key.strip() != SECRET_NAME:
            continue
        if seen:
            raise ValueError("Duplicate RIVAL_LLM_SHARED_TOKEN in private .env")
        seen = True
        candidate = value.strip().strip('"').strip("'")
        if not TOKEN_PATTERN.fullmatch(candidate):
            raise ValueError("Private RIVAL_LLM_SHARED_TOKEN is invalid")
        os.environ[SECRET_NAME] = candidate


def main() -> None:
    load_optional_local_secret()
    binary = Path(os.getenv("RIVAL_LLAMA_SERVER_BIN", "runtime/bin/llama-server"))
    if binary.is_file() and not os.access(binary, os.X_OK):
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    launch_server()


if __name__ == "__main__":
    main()
