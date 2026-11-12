"""Off-host GitHub Actions relay for the installed RivaL receipt command."""
from __future__ import annotations

import json
import os
import sys
import time
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


# Right after a deploy the bot is still restarting and has not collected
# fresh prices yet, so the host check defers with this reason. That is not
# a failure: wait for a few 5-minute collection cycles and ask again.
RESTART_REASONS = frozenset({"insufficient_live_venues", "minimum_spacing"})
RESTART_WAIT_SECONDS = 120
RESTART_ATTEMPTS = 5


def main() -> int:
    for attempt in range(1, RESTART_ATTEMPTS + 1):
        code, report = _attempt()
        if (report is None or report.get("outcome") != "deferred"
                or report.get("reason") not in RESTART_REASONS
                or attempt == RESTART_ATTEMPTS):
            return code
        print(f"Bot still warming up after restart ({report['reason']}); "
              f"retrying in {RESTART_WAIT_SECONDS}s (attempt {attempt}/{RESTART_ATTEMPTS})")
        time.sleep(RESTART_WAIT_SECONDS)
    return 1


def _attempt() -> tuple[int, dict | None]:
    token = os.environ.get("DISCLOUD_TOKEN", "")
    app_id = os.environ.get("DISCLOUD_APP_ID", "")
    if not token or not app_id:
        print("Discloud acceptance credentials unavailable")
        return 1, None
    req = request.Request(
        "https://api.discloud.app/v2/app/"
        + parse.quote(app_id, safe="") + "/console",
        data=json.dumps({"command": CMD}).encode(),
        headers={"api-token": token, "Content-Type": "application/json"},
        method="PUT",
    )
    data: object = None
    stdout = ""
    try:
        # Two sequential CPU inferences now (BTC, then the XRP regression
        # ask in llm_persistence_acceptance.verify_once): each can take
        # most of the 40 s gateway deadline, so 110 s could cut the second
        # ask off and fail a healthy deploy.
        with request.urlopen(req, timeout=240) as response:
            data = json.load(response)
        if not isinstance(data, dict):
            raise TypeError("unexpected console response")
        apps = data.get("apps")
        shell = apps.get("shell") if isinstance(apps, dict) else None
        stdout = str(shell.get("stdout", "")) if isinstance(shell, dict) else ""
        report = parse_receipt(stdout)
    except (error.URLError, TimeoutError, ValueError, KeyError, OSError, TypeError) as exc:
        # Never print host stdout/stderr: either may include private app data.
        # Output bounded structural diagnostics so missing/malformed console
        # receipts are actionable instead of an opaque ValueError.
        apps = data.get("apps") if isinstance(data, dict) else None
        shell = apps.get("shell") if isinstance(apps, dict) else None
        detail = {
            "exception": type(exc).__name__,
            "structured_receipt": "invalid" if MARKER in stdout else "absent",
            "stdout_chars": len(stdout),
            "stderr_present": bool(shell.get("stderr")) if isinstance(shell, dict) else False,
            "console_object_present": isinstance(shell, dict),
            "response_fields": sorted(data)[:8] if isinstance(data, dict) else [],
            "shell_fields": sorted(shell)[:8] if isinstance(shell, dict) else [],
        }
        print("LFM3 host acceptance not confirmed:", json.dumps(detail, sort_keys=True))
        return 1, None

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
    return (0 if outcome == "verified" else 1), report


if __name__ == "__main__":
    sys.exit(main())
