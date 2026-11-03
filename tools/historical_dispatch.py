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
    # Self-continuing Binance core collectors (issue #295); no inputs.
    "binance-deep-file-backfill.yml": set(),
    "binance-original-auxiliary.yml": set(),
}
MAX_DISPATCH = 3
# Only existing read-only acquisition workflows with workflow_dispatch support.
# Production is hosted from main, and recovery never promotes models or trades.
RECOVERY_WORKFLOWS = (
    "bitget-v3-overnight-archive.yml",
    "official-okx-advanced-archive.yml",
    "gate-official-archive-backfill.yml",
    "official-okx-bulk-bootstrap.yml",
    "binance-deep-file-backfill.yml",
    "binance-original-auxiliary.yml",
    "historical-source-provenance.yml",
)



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
        event = run.get("event")
        if event not in ("workflow_dispatch", "schedule", "push"):
            continue
        try:
            created = datetime.fromisoformat(run["created_at"])
        except (TypeError, ValueError, KeyError):
            # An unparseable run cannot safely authorize another dispatch.
            return False, "unreadable_workflow_history"
        if event == "workflow_dispatch" and (created > now.astimezone(UTC)
                                             - timedelta(hours=12)):
            return False, "recent_manual_or_coordinator_dispatch"
        if event in ("schedule", "push") and (created > now.astimezone(UTC)
                                              - timedelta(hours=4)):
            # Let source-native schedules do their work instead of dispatching
            # a duplicate collector while its last receipt is still fresh.
            return False, "recent_scheduled_or_main_source_run"
    return True, "not_running_or_recently_dispatched"


def should_recover_failed(runs: list[dict], *, now: datetime) -> tuple[bool, str]:
    """Recover a failed source workflow only if no newer success or active run exists."""
    if now.utcoffset() is None:
        raise ValueError("timezone-aware Actions timestamp required")
    if not runs:
        return False, "no_source_run_to_recover"
    if any(r.get("status") in ("queued", "in_progress", "waiting", "requested",
                               "pending") for r in runs):
        return False, "source_job_currently_active"
    observed = []
    for item in runs[:30]:
        if item.get("event") not in ("push", "schedule", "workflow_dispatch"):
            continue
        try:
            stamped = datetime.fromisoformat(item["created_at"])
        except (KeyError, TypeError, ValueError):
            return False, "invalid_source_job_history"
        if stamped.utcoffset() is None:
            return False, "invalid_source_job_history"
        observed.append((stamped.astimezone(UTC), item))
    if not observed:
        return False, "no_applicable_main_source_run"
    observed.sort(key=lambda x: x[0], reverse=True)
    last_time, last = observed[0]
    if last.get("conclusion") != "failure":
        return False, "newest_source_run_not_failed"
    elapsed = now.astimezone(UTC) - last_time
    if elapsed < timedelta(hours=6) or elapsed > timedelta(days=14):
        return False, "failure_cooldown_or_stale"
    recent_errors = [
        run for stamp, run in observed
        if stamp >= now.astimezone(UTC) - timedelta(days=1)
        and run.get("conclusion") == "failure"
    ]
    if len(recent_errors) >= 3:
        return False, "repeated_failure_requires_provider_review"
    # Scheduled workflows and their own per-provider concurrency remain primary.
    return True, "recoverable_last_source_run_failed"


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
            raise TypeError("malformed Actions history")
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
    # Check one bounded rotating subset of existing collectors per coordinator
    # run. Recovery has priority over optional sample/research dispatches.
    offset = now.date().toordinal() % len(RECOVERY_WORKFLOWS)
    selected = (RECOVERY_WORKFLOWS[offset:] +
                RECOVERY_WORKFLOWS[:offset])[:4]
    for workflow in selected:
        if accepted >= 1:
            break
        runs = client.runs(workflow)
        permitted, reason = should_recover_failed(runs, now=now)
        if permitted:
            client.dispatch(workflow, {})
            accepted += 1
            reason = "failed_source_collector_restarted_on_main"
        results.append({"workflow": workflow, "scope": "failed_source_recovery",
                        "status": reason})
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
