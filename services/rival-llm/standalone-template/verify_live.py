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
HOST_CHECK = """import json, os, urllib.error, urllib.request
port = int(os.environ.get("RIVAL_LLM_PORT", "8765"))
token = os.environ["RIVAL_LLM_SHARED_TOKEN"]
root = "http://127.0.0.1:%d" % port
headers = {"Authorization": "Bearer " + token}
req = urllib.request.Request(root + "/v1/status", headers=headers)
with urllib.request.urlopen(req, timeout=8) as response:
    data = json.loads(response.read(5000))
if not data.get("ready"):
    raise RuntimeError("private model not ready")
try:
    urllib.request.urlopen(urllib.request.Request(root + "/v1/status"), timeout=8)
except urllib.error.HTTPError as exc:
    auth_rejected = exc.code == 401
else:
    auth_rejected = False
if not auth_rejected:
    raise RuntimeError("private status permits anonymous access")
# One synthetic, read-only request: no historical claims, external APIs,
# database writes, market orders, real quotes or user evidence.
probe = {"asset": "BTC", "evidence": {
    "asset": "BTC", "analysis": "unvalidated_research",
    "horizon_minutes": 5, "sources": []}}
req = urllib.request.Request(
    root + "/v1/research", data=json.dumps(probe).encode("utf-8"),
    headers={**headers, "Content-Type": "application/json"}, method="POST")
with urllib.request.urlopen(req, timeout=38) as response:
    result = json.loads(response.read(5000))
smoke_ok = (result.get("model") == data.get("model")
    and result.get("direction") == "uncertain"
    and result.get("confidence") == "low"
    and result.get("grounding_status") == "insufficient_evidence"
    and result.get("grounded_by") == "deterministic_historical_evidence_policy"
    and result.get("informational_only") is True
    and result.get("uncalibrated") is True)
if not smoke_ok:
    raise RuntimeError("synthetic research was not safely grounded")
# Exercise the new real LFM3 research-to-feature path without predictions.
req = urllib.request.Request(
    root + "/v1/hypothesis", data=json.dumps(probe).encode("utf-8"),
    headers={**headers, "Content-Type": "application/json"}, method="POST")
with urllib.request.urlopen(req, timeout=38) as response:
    hypothesis = json.loads(response.read(5000))
indices = hypothesis.get("feature_indices")
choice = hypothesis.get("hypothesis")
feature_selection_valid = (
    (choice in ("trend_only", "momentum_only", "volatility_only", "volume_only")
     and indices is None)
    or (choice == "feature_subset_v2"
        and isinstance(indices, list) and 2 <= len(indices) <= 8
        and all(type(i) is int and 0 <= i < 10 for i in indices)
        and indices == sorted(set(indices)))
)
hypothesis_ok = (
    hypothesis.get("model") == data.get("model")
    and feature_selection_valid
    and hypothesis.get("research_only") is True
    and hypothesis.get("live_model_promoted") is False
    and hypothesis.get("retrospective_screen") == "not_tested"
    and hypothesis.get("prediction") is None
    and 5 <= len(str(hypothesis.get("rationale", ""))) <= 120
    and 5 <= len(str(hypothesis.get("falsification", ""))) <= 120
)
if not hypothesis_ok:
    raise RuntimeError("private LFM3 feature hypothesis rejected")
# Only report low-cardinality verification facts; no model prose or tokens.
data["hypothesis_smoke_ok"] = True
data["research_smoke_ok"] = True
data["auth_rejected"] = auth_rejected
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
            or receipt.get("research_smoke_ok") is not True
            or receipt.get("hypothesis_smoke_ok") is not True
            or receipt.get("auth_rejected") is not True
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
                "research_smoke_ok": receipt["research_smoke_ok"],
                "hypothesis_smoke_ok": receipt["hypothesis_smoke_ok"],
                "auth_rejected": receipt["auth_rejected"],
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
