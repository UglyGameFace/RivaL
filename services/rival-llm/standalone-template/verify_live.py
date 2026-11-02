"""Verify the deployed standalone model via its authenticated on-host status.

Use the Discloud console from Actions, never a public LLM endpoint. Do not
include bearer tokens, model responses, console stdout or private evidence in CI.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from urllib import error, parse, request

MARKER = "RIVAL_INTELLIGENCE_RELEASE_RECEIPT="
HOST_CHECK = """import json, os, urllib.request
port = int(os.environ.get("RIVAL_LLM_PORT", "8765"))
token = os.environ["RIVAL_LLM_SHARED_TOKEN"]
req = urllib.request.Request("http://127.0.0.1:%d/v1/status" % port, headers={"Authorization": "Bearer " + token})
with urllib.request.urlopen(req, timeout=8) as response:
    data = json.loads(response.read(5000))
print("RIVAL_INTELLIGENCE_RELEASE_RECEIPT=" + json.dumps(data, separators=(",", ":")))
"""


def parse_receipt(stdout: str, expected_sha: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise ValueError("Invalid expected source commit")
    for line in reversed(stdout.splitlines()):
        if not line.startswith(MARKER):
            continue
        try:
            receipt = json.loads(line[len(MARKER):])
        except ValueError:
            return None
        if not isinstance(receipt, dict):
            return None
        if (
            receipt.get("source_sha") != expected_sha
            or receipt.get("model") != "LiquidAI/LFM3-1.7B-GGUF:Q4_K_M"
            or receipt.get("ready") is not True
            or receipt.get("repair_state") not in ("healthy", "warming")
            or type(receipt.get("repair_count")) is not int
            or receipt["repair_count"] < 0
        ):
            return None
        return receipt
    return None


def check_host(token: str, app_id: str, expected_sha: str) -> dict | None:
    # A single-line shell argument is reliable across Discloud console relays.
    payload = base64.b64encode(HOST_CHECK.encode("utf-8")).decode("ascii")
    program = f"import base64;exec(base64.b64decode({payload!r}))"
    command = "python -c " + shlex.quote(program)
    endpoint = "https://api.discloud.app/v2/app/" + parse.quote(app_id, safe="") + "/console"
    req = request.Request(
        endpoint,
        data=json.dumps({"command": command}).encode("utf-8"),
        method="PUT",
        headers={"api-token": token, "Content-Type": "application/json"},
    )
    with request.urlopen(req, timeout=45) as response:
        data = json.load(response)
    apps = data.get("apps") if isinstance(data, dict) else None
    shell = apps.get("shell") if isinstance(apps, dict) else None
    if not isinstance(shell, dict):
        return None
    return parse_receipt(str(shell.get("stdout", "")), expected_sha)


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: verify_live.py UPSTREAM_COMMIT", file=sys.stderr)
        return 2
    sha = Path(sys.argv[1]).read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        print("Invalid source commit", file=sys.stderr)
        return 2
    token = os.getenv("DISCLOUD_TOKEN", "")
    app_id = os.getenv("DISCLOUD_LLM_APP_ID", "")
    if not token or not app_id:
        print("Missing dedicated Discloud release credentials", file=sys.stderr)
        return 2
    for attempt in range(1, 8):
        try:
            receipt = check_host(token, app_id, sha)
        except (OSError, error.URLError, ValueError, KeyError, TimeoutError, TypeError):
            receipt = None
        if receipt:
            print(json.dumps({
                "verified": True,
                "source_sha": sha,
                "model": receipt["model"],
                "repair_state": receipt["repair_state"],
                "repair_count": receipt["repair_count"],
            }, sort_keys=True))
            return 0
        print(f"Live standalone LFM3 receipt not yet verified (attempt {attempt}/7)")
        if attempt < 7:
            time.sleep(20)
    print("::error::Deployment not accepted: no fresh matching ready-host LFM3 receipt")
    return 1


if __name__ == "__main__":
    sys.exit(main())
