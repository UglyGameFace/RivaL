"""Read-only source parity check for Discloud's native GitHub Auto Deploy.

The old GitHub release action was a second deployment path. This checker only
reads files on the existing Discloud app through its console API and compares
their hashes with the checked-out, CI-tested main revision. Matching files
prove source parity on disk, not the process's uptime, loaded module versions,
or that any model has been imported.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib import error, parse, request

ROOT = Path(__file__).resolve().parents[1]
MARKER = "RIVAL_RUNTIME_SOURCE_SHA256="
REQUIRED = ("main.py", "requirements.txt", "pyproject.toml", "discloud.config")
# Identical algorithm on GitHub and the remote host. No script needs to be
# installed on Discloud and no private application data is included.
REMOTE_PYTHON = (
    "import hashlib,pathlib;"
    'base=pathlib.Path(".");'
    'files=[base/n for n in ("main.py","requirements.txt","pyproject.toml",'
    '"discloud.config")]+sorted((base/"src").rglob("*.py"));'
    "h=hashlib.sha256();"
    '[(h.update(p.as_posix().encode()),h.update(bytes((0,))),'
    "h.update(hashlib.sha256(p.read_bytes()).digest())) for p in files];"
    'print("RIVAL_RUNTIME_SOURCE_SHA256="+h.hexdigest())'
)
REMOTE_COMMAND = "python -c '" + REMOTE_PYTHON + "'"
ATTEMPTS = 6
RETRY_SECONDS = 30


def source_sha256(root: Path) -> str:
    files = [root / name for name in REQUIRED]
    files.extend(sorted((root / "src").rglob("*.py")))
    if len(files) <= len(REQUIRED) or any(not path.is_file() for path in files):
        raise ValueError("missing required deploy source")
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(bytes((0,)))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def parse_remote_sha(payload: dict) -> str:
    """Fail closed on malformed, ambiguous or fabricated console output."""
    if not isinstance(payload, dict):
        raise ValueError("invalid Discloud response")
    shell = payload.get("apps", {}).get("shell", {})
    stdout = shell.get("stdout") if isinstance(shell, dict) else None
    if not isinstance(stdout, str):
        raise ValueError("missing host stdout")
    found = [
        line[len(MARKER):].strip()
        for line in stdout.splitlines()
        if line.startswith(MARKER)
    ]
    if len(found) != 1 or re.fullmatch(r"[0-9a-f]{64}", found[0]) is None:
        raise ValueError("invalid host source fingerprint")
    return found[0]


def host_source_sha256(token: str, app_id: str) -> str:
    command = json.dumps({"command": REMOTE_COMMAND}).encode()
    req = request.Request(
        "https://api.discloud.app/v2/app/" + parse.quote(app_id, safe="") + "/console",
        data=command,
        headers={"api-token": token, "Content-Type": "application/json"},
        method="PUT",
    )
    with request.urlopen(req, timeout=110) as response:
        return parse_remote_sha(json.load(response))


def main() -> int:
    token = os.environ.get("DISCLOUD_TOKEN")
    app_id = os.environ.get("DISCLOUD_APP_ID")
    if not token or not app_id:
        print("Native Discloud source verification unavailable: missing credentials")
        return 1
    try:
        expected = source_sha256(ROOT)
    except (OSError, ValueError) as exc:
        print("CI checkout missing required app source:", type(exc).__name__)
        return 1
    for attempt in range(ATTEMPTS):
        try:
            observed = host_source_sha256(token, app_id)
            if observed == expected:
                print("Native Discloud source files match checked-out main (read-only).")
                print("This does not prove running-process version, model import, or prediction edge.")
                return 0
            reason = "source_not_yet_matching"
        except (error.URLError, TimeoutError, ValueError, OSError, KeyError,
                TypeError, AttributeError) as exc:
            # No token, command output, DB URL, SQL or remote stderr in logs.
            reason = type(exc).__name__
        print(f"Native Discloud source parity unverified ({reason}), "
              f"attempt {attempt + 1}/{ATTEMPTS}")
        if attempt + 1 < ATTEMPTS:
            time.sleep(RETRY_SECONDS)
    return 1


if __name__ == "__main__":
    sys.exit(main())
