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


def experiments(path: Path) -> tuple[str, list]:
    raw = path.read_bytes()
    report = json.loads(raw)
    trials = ((report.get("llm_hypothesis_research") or {}).get("experiments") or [])
    return hashlib.sha256(raw).hexdigest(), [t for t in trials if isinstance(t, dict)][:48]


def send(trials: list, sha: str, *, token: str, app_id: str) -> dict:
    payload = base64.b64encode(json.dumps(trials, separators=(",", ":")).encode()).decode()
    script = HOST_SCRIPT.replace("__TRIALS__", repr(payload)).replace("__SHA__", repr(sha))
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
        if line.startswith(MARKER):
            return json.loads(line[len(MARKER):])
    raise ValueError("no on-host receipt")


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
            print(f"RivaL hypothesis results: {path.name} has no idea experiments")
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
