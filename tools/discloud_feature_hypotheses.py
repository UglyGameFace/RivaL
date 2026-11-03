"""Read real private Discloud PostgreSQL LFM3 proposals into offline research.

One bounded authenticated Discloud console read; no AI inference, credentials
in reports, GitHub API writes, or auto-promotions. Missing evidence is explicit.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
from pathlib import Path
from urllib import error, parse, request

MARKER = "RIVAL_LLM_FEATURE_PROPOSALS="
HOST_SCRIPT = """import json, sys
sys.path.insert(0, 'src')
from parlay_bot.config import Settings
from parlay_bot.storage.hot import PostgresHotStore
from parlay_bot.perpetuals.research_hypotheses import recent_hypotheses
settings = Settings()
out = {'status':'unverified','proposals':[]}
try:
    if settings.rival_storage_backend == 'postgres' and settings.rival_database_url:
        store = PostgresHotStore(settings.rival_database_url.get_secret_value())
        try:
            proposals = recent_hypotheses(store, venue=__VENUE__)
        finally:
            store.close()
        out = {'status':'verified','proposals':proposals,
               'source':'live_postgresql_llm_hypotheses'}
except Exception:
    pass
print('RIVAL_LLM_FEATURE_PROPOSALS=' + json.dumps(out, separators=(',', ':')))
"""


def fetch_receipt(*, venue: str, token: str, app_id: str) -> dict:
    script = HOST_SCRIPT.replace("__VENUE__", repr(venue))
    encoded = base64.b64encode(script.encode()).decode("ascii")
    command = "python -c " + shlex.quote(
        f"import base64;exec(base64.b64decode({encoded!r}))"
    )
    req = request.Request(
        "https://api.discloud.app/v2/app/"
        + parse.quote(app_id, safe="") + "/console",
        headers={"api-token": token, "Content-Type": "application/json"},
        data=json.dumps({"command": command}).encode(), method="PUT",
    )
    with request.urlopen(req, timeout=65) as response:
        data = json.load(response)
    shell = data.get("apps", {}).get("shell", {})
    stdout = shell.get("stdout", "")
    if not isinstance(stdout, str) or len(stdout) > 32_000:
        raise ValueError("unbounded Discloud research stdout")
    for line in reversed(stdout.splitlines()):
        if line.startswith(MARKER):
            result = json.loads(line[len(MARKER):])
            if (not isinstance(result, dict)
                or result.get("status") not in ("verified", "unverified")
                or not isinstance(result.get("proposals"), list)
                or len(result["proposals"]) > 4):
                raise ValueError("invalid LFM3 proposal receipt")
            return result
    raise ValueError("no on-host proposal receipt")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venue", required=True, choices=(
        "hyperliquid", "dydx", "bitget", "gate", "okx", "kucoin",
        "deribit", "lighter", "phemex",
    ))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = {"status": "unverified", "proposals": []}
    token = os.getenv("DISCLOUD_TOKEN")
    app = os.getenv("DISCLOUD_APP_ID")
    if token and app:
        try:
            result = fetch_receipt(venue=args.venue, token=token, app_id=app)
        except (error.URLError, ValueError, KeyError, TypeError,
                OSError, TimeoutError, AttributeError):
            result = {"status": "unverified", "proposals": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
    print("RivaL bounded LFM3 proposals: status="
          + result["status"] + " count=" + str(len(result["proposals"])))
    # The baseline model run is independent; missing AI evidence never
    # blocks it or invents a successful LLM experiment.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
