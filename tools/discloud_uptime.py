"""Independent, read-only Discloud host-status checks from GitHub Actions.

This is deliberately off-host: a dead RivaL process cannot report its own death.
Never restart the bot, query its database, or log the API token or raw responses.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from urllib import error, parse, request

DISCORD_APP_STATE_ISSUE = "[Uptime] RivaL Discloud application offline"
API = "https://api.discloud.app/v2"
USER_AGENT = "RivaL-off-host-monitor/1"


class StatusUnverified(RuntimeError):
    """An unverified provider/API response is NOT proof the app is offline."""


def interpret_status(payload: object, app_id: str) -> str:
    """Official v2 status: {status:'ok', apps:{id,container:'Online'|'Offline'}}."""
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise StatusUnverified("status API response unverified")
    app = payload.get("apps")
    if not isinstance(app, dict) or str(app.get("id", "")) != app_id:
        raise StatusUnverified("status API application identity unverified")
    container = app.get("container")
    if not isinstance(container, str):
        raise StatusUnverified("missing container state")
    normalized = container.strip().lower()
    if normalized == "online":
        return "ONLINE"
    if normalized == "offline":
        return "OFFLINE"
    # Unknown provider statuses are not treated as healthy or as outages.
    raise StatusUnverified("unsupported container state")


def api_json(url: str, *, token: str, method: str = "GET",
             payload: dict | None = None) -> object:
    data = (json.dumps(payload).encode("utf-8") if payload is not None else None)
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization" if url.startswith("https://api.github.com/")
                else "api-token"] = (
            "Bearer " + token if url.startswith("https://api.github.com/") else token
        )
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = request.Request(url, headers=headers, data=data, method=method)
    try:
        with request.urlopen(req, timeout=12) as response:
            if response.status not in (200, 201):
                raise StatusUnverified("unexpected status API HTTP code")
            return json.loads(response.read(64_000).decode("utf-8"))
    except (error.URLError, TimeoutError, ValueError, UnicodeError, OSError) as exc:
        # Do not echo URL or raw provider response: both may contain sensitive
        # infrastructure values and provider-injected content.
        raise StatusUnverified("provider API request could not be verified") from exc


def sample_twice(app_id: str, api_token: str, *, pause: float = 5.0) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", app_id):
        raise StatusUnverified("application identifier missing or malformed")
    if not api_token:
        raise StatusUnverified("Discloud token not configured")
    endpoint = f"{API}/app/{parse.quote(app_id, safe='')}/status"
    first = interpret_status(api_json(endpoint, token=api_token), app_id)
    time.sleep(pause)
    second = interpret_status(api_json(endpoint, token=api_token), app_id)
    if first != second:
        raise StatusUnverified("provider app status changed during sampling")
    return second


def synchronize_incident(status: str, *, repository: str, token: str) -> str:
    """At most one issue; only transitions open/close it. Never restart."""
    if status not in ("ONLINE", "OFFLINE") or not token:
        raise StatusUnverified("incident sync not authorized")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise StatusUnverified("repository identity unavailable")
    endpoint = "https://api.github.com/repos/" + repository + "/issues"
    existing = api_json(endpoint + "?state=all&per_page=100", token=token)
    if not isinstance(existing, list):
        raise StatusUnverified("GitHub issue status unverified")
    candidates = [item for item in existing if isinstance(item, dict)
                  and item.get("title") == DISCORD_APP_STATE_ISSUE
                  and "pull_request" not in item]
    if len(candidates) > 1:
        raise StatusUnverified("duplicate uptime issues require reconciliation")
    issue = candidates[0] if candidates else None
    if status == "OFFLINE":
        if issue is None:
            api_json(endpoint, token=token, method="POST", payload={
                "title": DISCORD_APP_STATE_ISSUE,
                "body": (
                    "An **independent GitHub Actions monitor** received two "
                    "authenticated Discloud API v2 status responses identifying "
                    "RivaL as OFFLINE. This is an external host observation, "
                    "not a bot/database or provider-quote health check. "
                    "Investigate Discloud process status, last boot, and logs. "
                    "No automatic restart is authorized or attempted. "
                    "The same issue is reused on future transitions."
                ),
            })
            return "opened"
        if issue.get("state") == "closed":
            api_json(endpoint + f"/{issue['number']}", token=token,
                     method="PATCH", payload={"state": "open"})
            return "reopened"
        return "already_open"
    if issue is not None and issue.get("state") == "open":
        api_json(endpoint + f"/{issue['number']}/comments", token=token,
                 method="POST", payload={"body": (
                     "Independent Discloud status is ONLINE in two consecutive "
                     "samples. Closing this host-offline incident. This does "
                     "not certify feed, model, quote or PostgreSQL health."
                 )})
        api_json(endpoint + f"/{issue['number']}", token=token,
                 method="PATCH", payload={"state": "closed"})
        return "closed"
    return "no_active_incident"


def main() -> int:
    try:
        app_id = os.environ.get("DISCLOUD_APP_ID", "")
        token = os.environ.get("DISCLOUD_TOKEN", "")
        state = sample_twice(app_id, token)
        # No state in stdout from any unverified read.
        print(json.dumps({"observer": "github_actions", "discloud_host": state}))
        result = synchronize_incident(
            state, repository=os.environ.get("GITHUB_REPOSITORY", ""),
            token=os.environ.get("GH_TOKEN", ""),
        )
        print(json.dumps({"incident_transition": result}))
        return 0
    except StatusUnverified as exc:
        print(json.dumps({"observer": "github_actions", "status": "UNVERIFIED",
                          "reason": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
