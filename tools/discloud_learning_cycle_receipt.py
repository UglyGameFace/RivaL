"""Read-only production heartbeat acceptance via existing Discloud console API.

Runs in GitHub Actions after the actual RivaL main application upload.
Does not spend AI allowance, execute model inference, or mutate PostgreSQL.
"""
from __future__ import annotations

import json
import os
import sys
from urllib import error, parse, request

MARKER = "RIVAL_LEARNING_CYCLE_RECEIPT="
CMD = (
    "python -c 'import sys;"
    'sys.path.insert(0,"src");'
    "from parlay_bot.perpetuals.learning_health_receipt import main;"
    "main()'"
)


def parse_receipt(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        if line.startswith(MARKER):
            report = json.loads(line[len(MARKER):])
            if not isinstance(report, dict):
                raise ValueError("invalid host report")
            if report.get("outcome") not in ("verified", "unverified"):
                raise ValueError("invalid outcome")
            for name in ("forecast", "learning"):
                value = report.get(name)
                if value is not None and (
                    not isinstance(value, dict)
                    or value.get("status") not in (
                        "fresh", "stale_or_clock_invalid",
                        "not_recorded", "read_unavailable",
                    )
                ):
                    raise ValueError("invalid cycle status")
            return report
    raise ValueError("missing host learning receipt")


def accepted(report: dict) -> bool:
    """No success for skipped, absent, stale or failed worker receipts."""
    forecast, learning = report.get("forecast"), report.get("learning")
    return (
        report.get("outcome") == "verified"
        and isinstance(forecast, dict)
        and isinstance(learning, dict)
        and forecast.get("status") == "fresh"
        and learning.get("status") == "fresh"
        and learning.get("math_ok") is True
        and learning.get("model_review_ok") is True
        and report.get("does_not_prove_prediction_edge") is True
    )


def main() -> int:
    token, app_id = os.environ.get("DISCLOUD_TOKEN"), os.environ.get("DISCLOUD_APP_ID")
    if not token or not app_id:
        print("Learning acceptance credentials unavailable")
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
        shell = data.get("apps", {}).get("shell", {})
        report = parse_receipt(str(shell.get("stdout", "")))
    except (error.URLError, TimeoutError, ValueError, KeyError,
            OSError, TypeError, AttributeError) as exc:
        # Do not echo shell output, SQL, database URL, app token or API errors.
        print("Learning acceptance unreadable:", type(exc).__name__)
        return 1
    # The only host data emitted are bounded aggregate worker metrics.
    print("RivaL learning host receipt:", json.dumps(report, sort_keys=True))
    return 0 if accepted(report) else 1


if __name__ == "__main__":
    sys.exit(main())
