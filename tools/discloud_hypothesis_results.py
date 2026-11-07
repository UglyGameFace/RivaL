"""Send offline LFM3-idea experiment results back to RivaL's PostgreSQL.

Closes the research loop: the next idea request can see which earlier ideas
scored better or worse on real source data. One bounded authenticated
Discloud console call per report; only trials that name a stored proposal
are saved (``record_trials`` checks this on the host). Never fails the job.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shlex
from pathlib import Path
from urllib import error, parse, request

MARKER = "RIVAL_HYPOTHESIS_TRIALS_SAVED="
BLOCKED_MARKER = "RIVAL_BLOCKED_RECEIPT_SAVED="
HOST_SCRIPT = """import base64, json, sys
sys.path.insert(0, 'src')
from parlay_bot.config import Settings
from parlay_bot.storage.hot import PostgresHotStore
from parlay_bot.perpetuals.research_hypotheses import record_trials
out = {'status': 'unavailable', 'saved': 0}
try:
    settings = Settings()
    if settings.rival_storage_backend == 'postgres' and settings.rival_database_url:
        store = PostgresHotStore(settings.rival_database_url.get_secret_value())
        try:
            trials = json.loads(base64.b64decode(__TRIALS__))
            out = {'status': 'saved', 'saved': record_trials(store, trials, report_sha256=__SHA__)}
        finally:
            store.close()
except Exception as exc:
    out = {'status': 'failed', 'saved': 0, 'error': type(exc).__name__}
print('RIVAL_HYPOTHESIS_TRIALS_SAVED=' + json.dumps(out))
"""
BLOCKED_HOST_SCRIPT = """import base64, json, sys
sys.path.insert(0, 'src')
from parlay_bot.config import Settings
from parlay_bot.storage.hot import PostgresHotStore
from parlay_bot.perpetuals.research_hypotheses import record_blocked_receipt
out = {'status': 'unavailable', 'saved': 0}
try:
    settings = Settings()
    if settings.rival_storage_backend == 'postgres' and settings.rival_database_url:
        store = PostgresHotStore(settings.rival_database_url.get_secret_value())
        try:
            receipt = json.loads(base64.b64decode(__RECEIPT__))
            out = {'status': 'saved', 'saved': record_blocked_receipt(store, **receipt)}
        finally:
            store.close()
except Exception as exc:
    out = {'status': 'failed', 'saved': 0, 'error': type(exc).__name__}
print('RIVAL_BLOCKED_RECEIPT_SAVED=' + json.dumps(out))
"""


def experiments(path: Path) -> tuple[str, list]:
    raw = path.read_bytes()
    report = json.loads(raw)
    trials = ((report.get("llm_hypothesis_research") or {}).get("experiments") or [])
    return hashlib.sha256(raw).hexdigest(), [t for t in trials if isinstance(t, dict)][:48]


def blocked_receipt(path: Path) -> dict | None:
    """One blocked-run receipt from a source-native report, or None.

    A blocked report (status ``source_months_incomplete``) carries no
    trials, so the trials path records nothing for the run (spec D17).
    The receipt names the venue, the asset (Bitget only, else None),
    the requested months, and the union of the missing months, so the
    driver can close the affected experiment with a finding naming the
    missing months and the card can show the actual blocker.
    """
    report = json.loads(path.read_bytes())
    if not isinstance(report, dict):
        return None
    if report.get("status") != "source_months_incomplete":
        return None
    missing = sorted({
        month
        for scope in report.get("source_scopes") or []
        if isinstance(scope, dict)
        for month in scope.get("missing_or_incomplete_months") or []
        if isinstance(month, str)
    })
    months = report.get("requested_months") or []
    return {
        "venue": str(report.get("venue")),
        "asset": report.get("asset"),
        "requested_months": len(months) if isinstance(months, list) else 0,
        "missing_months": missing,
    }


def _send_console(script: str, marker: str, *, token: str, app_id: str) -> dict:
    encoded = base64.b64encode(script.encode()).decode("ascii")
    command = "python -c " + shlex.quote(f"import base64;exec(base64.b64decode({encoded!r}))")
    req = request.Request(
        "https://api.discloud.app/v2/app/" + parse.quote(app_id, safe="") + "/console",
        headers={"api-token": token, "Content-Type": "application/json"},
        data=json.dumps({"command": command}).encode(), method="PUT",
    )
    with request.urlopen(req, timeout=65) as response:
        data = json.load(response)
    stdout = data.get("apps", {}).get("shell", {}).get("stdout", "")
    for line in reversed(str(stdout).splitlines()):
        if line.startswith(marker):
            return json.loads(line[len(marker):])
    raise ValueError("no on-host receipt")


def send(trials: list, sha: str, *, token: str, app_id: str) -> dict:
    payload = base64.b64encode(json.dumps(trials, separators=(",", ":")).encode()).decode()
    script = HOST_SCRIPT.replace("__TRIALS__", repr(payload)).replace("__SHA__", repr(sha))
    return _send_console(script, MARKER, token=token, app_id=app_id)


def send_blocked(receipt: dict, sha: str, *, token: str, app_id: str) -> dict:
    payload = base64.b64encode(json.dumps(
        {**receipt, "report_sha256": sha},
        separators=(",", ":")).encode()).decode()
    script = BLOCKED_HOST_SCRIPT.replace("__RECEIPT__", repr(payload))
    return _send_console(script, BLOCKED_MARKER, token=token, app_id=app_id)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reports-dir", type=Path, default=Path("data/native-reports"))
    args = parser.parse_args(argv)
    token, app = os.getenv("DISCLOUD_TOKEN"), os.getenv("DISCLOUD_APP_ID")
    for path in sorted(args.reports_dir.glob("source-*-research*.json")):
        try:
            sha, trials = experiments(path)
        except (OSError, ValueError):
            print(f"RivaL hypothesis results: unreadable report {path.name}")
            continue
        if not trials:
            # A blocked run produces zero trials and previously recorded
            # nothing (spec D17). Its receipt reaches Postgres through
            # the same console bridge as the trials.
            try:
                receipt = blocked_receipt(path)
            except (OSError, ValueError):
                receipt = None
            if receipt is None:
                print(f"RivaL hypothesis results: {path.name} has no idea experiments")
                continue
            if not (token and app):
                print("RivaL hypothesis results: Discloud not configured; not saved")
                return 0
            try:
                result = send_blocked(receipt, sha, token=token, app_id=app)
            except (error.URLError, ValueError, KeyError, TypeError, OSError, TimeoutError) as exc:
                result = {"status": "unavailable", "saved": 0, "error": type(exc).__name__}
            print(f"RivaL hypothesis results: {path.name} blocked "
                  f"venue={receipt.get('venue')} status={result.get('status')} "
                  f"saved={result.get('saved')}")
            continue
        if not (token and app):
            print("RivaL hypothesis results: Discloud not configured; not saved")
            return 0
        try:
            result = send(trials, sha, token=token, app_id=app)
        except (error.URLError, ValueError, KeyError, TypeError, OSError, TimeoutError) as exc:
            result = {"status": "unavailable", "saved": 0, "error": type(exc).__name__}
        print(f"RivaL hypothesis results: {path.name} trials={len(trials)} "
              f"status={result.get('status')} saved={result.get('saved')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
