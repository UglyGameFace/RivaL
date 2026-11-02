"""Guarded GitHub Actions dispatcher for the existing historical collectors.

Only called by the source-exact coordinator workflow. No token persistence,
provider credential access, branch edits or direct trade execution.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ALLOWED_WORKFLOWS = {
    "source-native-research.yml": {"venue", "first_month", "asset"},
    "bitget-v3-overnight-archive.yml": {"first_month", "max_requests"},
    "deribit-hourly-native-backfill.yml": {"day", "hour"},
}
MAX_DISPATCH = 2


def should_dispatch(runs: list[dict], *, now: datetime) -> tuple[bool, str]:
    """Skip active and recently dispatched jobs, including queued jobs.

    This check limits duplicates; individual workflow concurrency groups remain
    authoritative for racing Actions and their own provider limits.
    """
    if now.utcoffset() is None:
        raise ValueError("timezone-aware Actions timestamp required")
    for run in runs:
        if run.get("status") in ("queued", "in_progress", "waiting", "requested",
                                 "pending"):
            return False, "existing_workflow_run_active"
        if run.get("event") != "workflow_dispatch":
            continue
        try:
            created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        except (TypeError, ValueError, KeyError):
            # An unparseable run cannot safely authorize another dispatch.
            return False, "unreadable_workflow_history"
        if created > now.astimezone(UTC) - timedelta(hours=12):
            return False, "recent_manual_or_coordinator_dispatch"
    return True, "not_running_or_recently_dispatched"


class GitHubActions:
    def __init__(self, *, repo: str, token: str):
        if (repo != "UglyGameFace/RivaL" or not token):
            raise ValueError("authorized repository and ephemeral Actions token required")
        self._repo = repo
        self._token = token

    def request(self, path: str, *, payload: dict | None = None) -> object:
        if not path.startswith("/actions/workflows/") or ".." in path:
            raise ValueError("invalid Actions-only API path")
        body = json.dumps(payload).encode() if payload is not None else None
        req = Request(
            "https://api.github.com/repos/" + self._repo + path,
            data=body, method="POST" if body is not None else "GET",
            headers={"Authorization": "Bearer " + self._token,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "Content-Type": "application/json"},
        )
        try:
            with urlopen(req, timeout=20) as response:
                if payload is not None:
                    if response.status != 204:
                        raise RuntimeError("GitHub did not accept workflow dispatch")
                    return None
                if response.status != 200:
                    raise RuntimeError("GitHub Actions history unavailable")
                return json.loads(response.read(512_000))
        except HTTPError as exc:
            raise RuntimeError(
                "GitHub Actions API HTTP " + str(exc.code)) from exc
        except URLError as exc:
            raise RuntimeError("GitHub Actions API connection unavailable") from exc

    def runs(self, workflow: str) -> list[dict]:
        args = urlencode({"branch": "main", "per_page": 30})
        doc = self.request("/actions/workflows/" + workflow + "/runs?" + args)
        if not isinstance(doc, dict) or not isinstance(doc.get("workflow_runs"), list):
            raise RuntimeError("malformed Actions history")
        return doc["workflow_runs"]

    def dispatch(self, workflow: str, inputs: dict) -> None:
        self.request("/actions/workflows/" + workflow + "/dispatches",
                     payload={"ref": "main", "inputs": inputs})


def run_plan(plan: dict, client: GitHubActions, *,
             now: datetime, max_dispatches: int = MAX_DISPATCH) -> dict:
    if (plan.get("schema_version") != "rival-historical-coordinator-v1"
        or plan.get("all_history_complete") is not False
        or not 1 <= max_dispatches <= MAX_DISPATCH):
        raise ValueError("untrusted or unbounded historical plan")
    results = []
    accepted = 0
    for task in plan.get("dispatch_candidates", []):
        if accepted >= max_dispatches:
            break
        workflow = task.get("workflow")
        inputs = task.get("inputs")
        if (workflow not in ALLOWED_WORKFLOWS or not isinstance(inputs, dict)
            or set(inputs) != ALLOWED_WORKFLOWS[workflow]
            or not all(isinstance(v, str) for v in inputs.values())):
            raise ValueError("unknown/untrusted historical collector dispatch")
        # API history is an extra guard; the target workflow still serializes
        # under its original per-provider concurrency group.
        runs = client.runs(workflow)
        permitted, reason = should_dispatch(runs, now=now)
        if permitted:
            client.dispatch(workflow, inputs)
            accepted += 1
            reason = "dispatched_on_main"
        results.append({"workflow": workflow, "scope": task["scope"],
                        "status": reason})
    return {"schema_version": "rival-historical-dispatch-v1",
            "generated_at": now.astimezone(UTC).isoformat(),
            "dispatched": accepted, "results": results,
            "overall_history_complete": False,
            "model_live_approved": False}


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded source-exact Actions dispatch")
    parser.add_argument("--plan", type=Path, default=Path(
        "data/native-reports/historical-coordinator.json"))
    parser.add_argument("--out", type=Path, default=Path(
        "data/native-reports/historical-dispatch.json"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    now = datetime.now(UTC)
    if args.dry_run:
        report = {"dry_run": True, "candidates": plan["dispatch_candidates"],
                  "dispatches_sent": 0}
    else:
        token = os.environ.get("GITHUB_TOKEN", "")
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        report = run_plan(plan, GitHubActions(repo=repo, token=token), now=now)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
