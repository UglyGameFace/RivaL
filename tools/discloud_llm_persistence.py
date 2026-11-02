"""Off-host GitHub Actions relay for the installed RivaL receipt command."""
from __future__ import annotations

import json
import os
import sys
from urllib import error, parse, request

MARKER = "RIVAL_LFM3_PERSISTENCE_RECEIPT="
CMD = (
    "python -c 'import sys;"
    'sys.path.insert(0,"src");'
    "from parlay_bot.perpetuals.llm_persistence_acceptance import main;"
    "main()'"
)


def parse_receipt(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        if line.startswith(MARKER):
            data = json.loads(line[len(MARKER):])
            if isinstance(data, dict) and data.get("outcome") in (
                "verified", "deferred", "failed"
            ):
                return data
            raise ValueError("invalid receipt status")
    raise ValueError("missing structured receipt")



def integrated_receipt(report: dict) -> bool:
    """Only the NEW hosted Intelligence/math/DB handoff passes; never a green skip."""
    return (
        report.get("outcome") == "verified"
        and report.get("model") == "LiquidAI/LFM3-1.7B-GGUF:Q4_K_M"
        and report.get("mathematical_evidence_status") == "persisted_registry"
        and report.get("prospective_math_status") in (
            "supported", "insufficient", "not_improving",
        )
        and report.get("cold_archive_readback") == "not_performed_by_live_research"
        and report.get("evidence_hash_valid") is True
        and type(report.get("source_count")) is int
        and report["source_count"] >= 3
        and type(report.get("fresh_six_month_source_audits")) is int
        and report["fresh_six_month_source_audits"] >= 0
        and report.get("from_cache") is False
        and type(report.get("saved_before")) is int
        and type(report.get("saved_after")) is int
        and report["saved_after"] > report["saved_before"]
    )


def main() -> int:
    token = os.environ.get("DISCLOUD_TOKEN", "")
    app_id = os.environ.get("DISCLOUD_APP_ID", "")
    if not token or not app_id:
        print("Discloud acceptance credentials unavailable")
        return 1
    req = request.Request(
        "https://api.discloud.app/v2/app/"
        + parse.quote(app_id, safe="") + "/console",
        data=json.dumps({"command": CMD}).encode(),
        headers={"api-token": token, "Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with request.urlopen(req, timeout=110) as response:
            data = json.load(response)
        stdout = str(data.get("apps", {}).get("shell", {}).get("stdout", ""))
        report = parse_receipt(stdout)
    except (error.URLError, TimeoutError, ValueError, KeyError, OSError) as exc:
        print("LFM3 host acceptance not confirmed:", type(exc).__name__)
        return 1

    outcome = report["outcome"]
    # An older LFM3 receipt or cache hit is not fresh evidence that the
    # post-merge math/audit integration is deployed. Do not auto-close issues.
    if outcome == "verified" and not integrated_receipt(report):
        outcome = "failed"
        report = {**report, "outcome": outcome,
                  "reason": "new_integrated_host_receipt_not_verified"}
    print("LFM3 PostgreSQL acceptance:", json.dumps(report, sort_keys=True))
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"outcome={outcome}\n")
    return 0 if outcome != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
