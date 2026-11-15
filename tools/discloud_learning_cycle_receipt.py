"""Read-only production heartbeat acceptance via existing Discloud console API.

Runs in GitHub Actions after verified native Discloud source parity.
Does not spend AI allowance, execute model inference, or mutate PostgreSQL.
"""
from __future__ import annotations

import json
import os
import sys
from urllib import error, parse, request

MARKER = "RIVAL_LEARNING_CYCLE_RECEIPT="
XMARKET_MARKER = "RIVAL_XMARKET_SHADOW_DIAGNOSTIC="
CMD = (
    "python -c 'import sys;"
    'sys.path.insert(0,"src");'
    "from parlay_bot.perpetuals.learning_health_receipt import main;"
    "from parlay_bot.perpetuals.xmarket_shadow_diagnostic import main as shadow_probe;"
    "main();shadow_probe()'"
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



def parse_xmarket_probe(stdout: str) -> dict:
    """Structured source-stage evidence; never accept success without safety flags."""
    for line in reversed(stdout.splitlines()):
        if line.startswith(XMARKET_MARKER):
            probe = json.loads(line[len(XMARKET_MARKER):])
            if not isinstance(probe, dict) or probe.get("status") not in (
                "recorded", "read_unavailable",
            ):
                raise ValueError("invalid XMarket diagnostic status")
            if probe.get("read_only") is not True or probe.get(
                "does_not_prove_prospective_predictions"
            ) is not True:
                raise ValueError("invalid XMarket diagnostic safety flags")
            return probe
    raise ValueError("missing XMarket diagnostic")


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
        stdout = str(shell.get("stdout", ""))
        report = parse_receipt(stdout)
        try:
            shadow = parse_xmarket_probe(stdout)
        except (ValueError, KeyError, TypeError):
            shadow = {"status": "read_unavailable",
                      "does_not_prove_prospective_predictions": True}
    except (error.URLError, TimeoutError, ValueError, KeyError,
            OSError, TypeError, AttributeError) as exc:
        # Do not echo shell output, SQL, database URL, app token or API errors.
        print("Learning acceptance unreadable:", type(exc).__name__)
        return 1
    # The only host data emitted are bounded aggregate worker metrics.
    print("RivaL learning host receipt:", json.dumps(report, sort_keys=True))
    print("XMarket shadow input diagnosis:", json.dumps(shadow, sort_keys=True))
    return 0 if accepted(report) else 1


if __name__ == "__main__":
    sys.exit(main())
