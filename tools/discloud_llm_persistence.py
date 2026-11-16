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


def _scoped_command(asset: str) -> str:
    """One CPU-heavy inference per console request, not two per gateway call."""
    if asset not in ("BTC", "XRP"):
        raise ValueError("unsupported LFM3 acceptance asset")
    return (
        "python -c 'import sys;"
        'sys.path.insert(0,"src");'
        "from parlay_bot.perpetuals.llm_persistence_acceptance import main;"
        f'main(["--asset","{asset}"])' + "'"
    )


def _console_once(asset: str) -> dict | None:
    token, app_id = os.environ.get("DISCLOUD_TOKEN"), os.environ.get("DISCLOUD_APP_ID")
    if not token or not app_id:
        print("Discloud acceptance credentials unavailable")
        return None
    req = request.Request(
        "https://api.discloud.app/v2/app/"
        + parse.quote(app_id, safe="") + "/console",
        data=json.dumps({"command": _scoped_command(asset)}).encode(),
        headers={"api-token": token, "Content-Type": "application/json"},
        method="PUT",
    )
    data: object = None
    stdout = ""
    try:
        # Discloud may end long synchronous console responses at its gateway.
        # Each request now invokes ONE model, with an independent exact DB proof.
        with request.urlopen(req, timeout=150) as response:
            data = json.load(response)
        if not isinstance(data, dict):
            raise TypeError("unexpected console response")
        apps = data.get("apps")
        shell = apps.get("shell") if isinstance(apps, dict) else None
        stdout = str(shell.get("stdout", "")) if isinstance(shell, dict) else ""
        report = parse_receipt(stdout)
        if report.get("asset") != asset:
            raise ValueError("LFM3 receipt asset mismatch")
        return report
    except (error.URLError, TimeoutError, ValueError, KeyError, OSError, TypeError) as exc:
        # Never log host stdout/stderr, gateway response body or token/URL.
        apps = data.get("apps") if isinstance(data, dict) else None
        shell = apps.get("shell") if isinstance(apps, dict) else None
        diagnostic = {
            "asset": asset,
            "exception": type(exc).__name__,
            "http_status": int(exc.code) if isinstance(exc, error.HTTPError) else None,
            "structured_receipt": "invalid" if MARKER in stdout else "absent",
            "stdout_chars": len(stdout),
            "stderr_present": bool(shell.get("stderr")) if isinstance(shell, dict) else False,
            "console_object_present": isinstance(shell, dict),
            "response_fields": sorted(data)[:8] if isinstance(data, dict) else [],
            "shell_fields": sorted(shell)[:8] if isinstance(shell, dict) else [],
        }
        print("LFM3 host acceptance not confirmed:", json.dumps(diagnostic, sort_keys=True))
        return None


def _attempt() -> tuple[int, dict | None]:
    btc = _console_once("BTC")
    if btc is None:
        return 1, None
    if btc.get("outcome") != "verified":
        print("LFM3 BTC host proof:", json.dumps({
            "outcome": btc.get("outcome"), "reason": btc.get("reason"),
        }, sort_keys=True))
        return 1, btc
    if not integrated_receipt(btc):
        return 1, {"outcome": "failed", "reason": "btc_math_or_new_db_proof_missing"}
    # Never accept a healthy BTC model as evidence that the historically
    # regression-prone XRP persistence path works. Separate gateway execution,
    # same full original-source/DB confirmation requirement.
    xrp = _console_once("XRP")
    if xrp is None:
        return 1, None
    if xrp.get("outcome") != "verified":
        print("LFM3 XRP host proof:", json.dumps({
            "outcome": xrp.get("outcome"), "reason": xrp.get("reason"),
        }, sort_keys=True))
        return 1, xrp
    if not (
        xrp.get("xrp_verified") is True
        and xrp.get("model") == btc.get("model")
        and type(xrp.get("source_count")) is int
        and xrp["source_count"] >= 3
        and type(xrp.get("saved_before")) is int
        and type(xrp.get("saved_after")) is int
        and (xrp["saved_after"] > xrp["saved_before"]
             or xrp.get("from_cache") is True)
    ):
        return 1, {"outcome": "failed", "reason": "xrp_exact_db_proof_missing"}
    outcome = {"outcome": "verified", "model": btc["model"],
               "btc_math_new_db_verified": True, "xrp_exact_db_verified": True,
               "console_calls": 2}
    print("LFM3 PostgreSQL acceptance:", json.dumps(outcome, sort_keys=True))
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as stream:
            stream.write("outcome=verified\n")
    return 0, outcome


if __name__ == "__main__":
    sys.exit(main())
