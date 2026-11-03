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
STAGE_MARKER = "RIVAL_INTELLIGENCE_RELEASE_STAGE="
SAFE_STAGES = frozenset({
    "boot", "status_requested", "status_unavailable", "status_not_ready",
    "status_ready", "auth_checked", "auth_not_rejected",
    "research_requested", "research_http_error", "research_rejected",
    "research_verified", "hypothesis_requested", "hypothesis_http_error",
    "hypothesis_rejected", "hypothesis_verified",
    "hypothesis_http_429_busy", "hypothesis_http_422_context_budget",
    "hypothesis_http_503_backend_timeout",
    "hypothesis_http_503_backend_unavailable",
    "hypothesis_http_503_backend_http",
    "hypothesis_http_503_invalid_json",
    "hypothesis_http_503_invalid_hypothesis",
    "hypothesis_http_503_invalid_output",
    "hypothesis_http_503_malformed_backend_response",
    "hypothesis_http_503_token_limit",
    "hypothesis_http_503_unclassified",
})

HOST_CHECK = """import json, os, urllib.error, urllib.request
print("RIVAL_INTELLIGENCE_RELEASE_STAGE=boot", flush=True)
port = int(os.environ.get("RIVAL_LLM_PORT", "8765"))
token = os.environ["RIVAL_LLM_SHARED_TOKEN"]
root = "http://127.0.0.1:%d" % port
headers = {"Authorization": "Bearer " + token}
req = urllib.request.Request(root + "/v1/status", headers=headers)
print("RIVAL_INTELLIGENCE_RELEASE_STAGE=status_requested", flush=True)
try:
    with urllib.request.urlopen(req, timeout=8) as response:
        data = json.loads(response.read(5000))
except (urllib.error.URLError, TimeoutError):
    print("RIVAL_INTELLIGENCE_RELEASE_STAGE=status_unavailable", flush=True)
    raise
if not data.get("ready"):
    print("RIVAL_INTELLIGENCE_RELEASE_STAGE=status_not_ready", flush=True)
    raise RuntimeError("private model not ready")
print("RIVAL_INTELLIGENCE_RELEASE_STAGE=status_ready", flush=True)
try:
    urllib.request.urlopen(urllib.request.Request(root + "/v1/status"), timeout=8)
except urllib.error.HTTPError as exc:
    auth_rejected = exc.code == 401
else:
    auth_rejected = False
if not auth_rejected:
    print("RIVAL_INTELLIGENCE_RELEASE_STAGE=auth_not_rejected", flush=True)
    raise RuntimeError("private status permits anonymous access")
print("RIVAL_INTELLIGENCE_RELEASE_STAGE=auth_checked", flush=True)
# One synthetic, read-only request: no historical claims, external APIs,
# database writes, market orders, real quotes or user evidence.
probe = {"asset": "BTC", "evidence": {
    "asset": "BTC", "analysis": "unvalidated_research",
    "horizon_minutes": 5, "sources": []}}
req = urllib.request.Request(
    root + "/v1/research", data=json.dumps(probe).encode("utf-8"),
    headers={**headers, "Content-Type": "application/json"}, method="POST")
print("RIVAL_INTELLIGENCE_RELEASE_STAGE=research_requested", flush=True)
try:
    with urllib.request.urlopen(req, timeout=55) as response:
        result = json.loads(response.read(5000))
except urllib.error.HTTPError:
    print("RIVAL_INTELLIGENCE_RELEASE_STAGE=research_http_error", flush=True)
    raise
smoke_ok = (result.get("model") == data.get("model")
    and result.get("direction") == "uncertain"
    and result.get("confidence") == "low"
    and result.get("grounding_status") == "insufficient_evidence"
    and result.get("grounded_by") == "deterministic_historical_evidence_policy"
    and result.get("informational_only") is True
    and result.get("uncalibrated") is True)
if not smoke_ok:
    print("RIVAL_INTELLIGENCE_RELEASE_STAGE=research_rejected", flush=True)
    raise RuntimeError("synthetic research was not safely grounded")
print("RIVAL_INTELLIGENCE_RELEASE_STAGE=research_verified", flush=True)
# Exercise the new real LFM3 research-to-feature path without predictions.
req = urllib.request.Request(
    root + "/v1/hypothesis", data=json.dumps(probe).encode("utf-8"),
    headers={**headers, "Content-Type": "application/json"}, method="POST")
print("RIVAL_INTELLIGENCE_RELEASE_STAGE=hypothesis_requested", flush=True)
try:
    with urllib.request.urlopen(req, timeout=55) as response:
        hypothesis = json.loads(response.read(5000))
except urllib.error.HTTPError as exc:
    # Only fixed HTTP statuses and approved classes; no private model body.
    if exc.code == 429:
        label = "hypothesis_http_429_busy"
    elif exc.code == 422:
        label = "hypothesis_http_422_context_budget"
    elif exc.code == 503:
        kind = "unclassified"
        try:
            body = json.loads(exc.read(1000))
            if isinstance(body, dict) and body.get("error") == "inference_failed":
                proposed = body.get("failure_kind")
                if isinstance(proposed, str) and proposed in {
                    "backend_timeout", "backend_unavailable", "backend_http",
                    "invalid_json", "invalid_hypothesis", "invalid_output",
                    "malformed_backend_response", "token_limit",
                }:
                    kind = proposed
        except (OSError, ValueError):
            pass
        label = "hypothesis_http_503_" + kind
    else:
        label = "hypothesis_http_error"
    print("RIVAL_INTELLIGENCE_RELEASE_STAGE=" + label, flush=True)
    raise
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
    print("RIVAL_INTELLIGENCE_RELEASE_STAGE=hypothesis_rejected", flush=True)
    raise RuntimeError("private LFM3 feature hypothesis rejected")
print("RIVAL_INTELLIGENCE_RELEASE_STAGE=hypothesis_verified", flush=True)
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


def stage_reason(stdout: str, *, stderr_present: bool = False) -> str:
    """Return only a fixed stage name; never expose shell output or errors."""
    stage = "host_stage_absent"
    for line in stdout.splitlines():
        if not line.startswith(STAGE_MARKER):
            continue
        candidate = line[len(STAGE_MARKER):].strip()
        if candidate in SAFE_STAGES:
            stage = candidate
    return stage + ("_stderr" if stderr_present else "")


def check_host(token: str, app_id: str, expected_sha: str) -> tuple[dict | None, str]:
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
    # The gateway may spend up to 40s on EACH CPU request. The two host
    # smoke calls are allowed 55s each and do not run concurrently.
    with request.urlopen(req, timeout=145) as response:
        data = json.load(response)
    apps = data.get("apps") if isinstance(data, dict) else None
    shell = apps.get("shell") if isinstance(apps, dict) else None
    if not isinstance(shell, dict):
        return None, "console_shell_missing"
    stdout = str(shell.get("stdout", ""))
    receipt = parse_receipt(stdout, expected_sha)
    return (
        (receipt, "verified") if receipt else
        (None, stage_reason(stdout, stderr_present=bool(shell.get("stderr"))))
    )


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
        reason = "no_matching_ready_host_receipt"
        try:
            receipt, reason = check_host(token, app_id, sha)
        except (OSError, error.URLError, ValueError, KeyError, TimeoutError, TypeError) as exc:
            # Class only: shell output can contain credentials or private
            # model data, so never print the raw API or inference response.
            reason = type(exc).__name__
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
        print(
            f"Live standalone LFM3 receipt not yet verified "
            f"(attempt {attempt}/7, reason={reason})",
            flush=True,
        )
        if attempt < 7:
            time.sleep(20)
    print("::error::Deployment not accepted: no fresh matching ready-host LFM3 receipt")
    return 1


if __name__ == "__main__":
    sys.exit(main())
