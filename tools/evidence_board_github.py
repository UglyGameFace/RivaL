"""Safe public GitHub metadata for RivaL's read-only evidence dashboard.

Runs under the first-party GitHub Actions token. Never publishes raw PR bodies,
workflow logs, private repository contents, account identities, or credentials.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from urllib import parse, request

REPO = "UglyGameFace/RivaL"
INTELLIGENCE_REPO = "UglyGameFace/RivaL-Intelligence"
API_ROOT = "https://api.github.com/repos/" + REPO
PINNED_PRS = (447, 463, 526, 529, 531, 532, 533, 534, 536, 538, 542, 543)
_SHA = re.compile(r"[a-fA-F0-9]{40}\Z")


def _github_json(path: str, token: str, limit: int = 2_000_000, *,
                 repository: str = REPO) -> object:
    if not path.startswith("/") or "://" in path:
        raise ValueError("github_path_invalid")
    if repository not in (REPO, INTELLIGENCE_REPO):
        raise ValueError("github_repository_not_allowed")
    url = "https://api.github.com/repos/" + repository + path
    req = request.Request(
        url,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "RivaL-readonly-evidence-receipt",
        },
    )
    with request.urlopen(req, timeout=24) as response:
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("github_response_too_large")
    return json.loads(raw)


def _sha(value: object) -> str | None:
    return value.lower() if isinstance(value, str) and _SHA.fullmatch(value) else None


def _date(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 36:
        return None
    if re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\Z", value):
        return value
    return None


def safe_pr(value: dict, *, repository: str = REPO) -> dict | None:
    """Allowlist PR fields. No body, email, branch URLs, labels, or comments."""
    number = value.get("number")
    if type(number) is not int or not 1 <= number <= 1_000_000:
        return None
    head, base = value.get("head"), value.get("base")
    merged = _date(value.get("merged_at"))
    return {
        "number": number,
        "title": str(value.get("title") or "")[:160],
        "state": value.get("state") if value.get("state") in ("open", "closed") else "unknown",
        "merged_at": merged,
        "merge_commit_sha": _sha(value.get("merge_commit_sha")),
        "created_at": _date(value.get("created_at")),
        "updated_at": _date(value.get("updated_at")),
        "html_url": f"https://github.com/{repository}/pull/{number}",
        "head_sha": _sha(head.get("sha")) if isinstance(head, dict) else None,
        "base_sha": _sha(base.get("sha")) if isinstance(base, dict) else None,
    }


def safe_run(value: dict, *, repository: str = REPO) -> dict | None:
    number = value.get("id")
    if type(number) is not int or not 1 <= number <= 999_999_999_999_999:
        return None
    status = value.get("status")
    conclusion = value.get("conclusion")
    return {
        "id": number,
        "name": str(value.get("name") or "")[:120],
        "event": str(value.get("event") or "unknown")[:45],
        "status": status if status in ("queued", "in_progress", "completed", "waiting", "pending") else "unknown",
        "conclusion": conclusion if conclusion in (
            "success", "failure", "cancelled", "skipped", "timed_out",
            "neutral", "action_required", "stale", None
        ) else "unknown",
        "head_sha": _sha(value.get("head_sha")),
        "created_at": _date(value.get("created_at")),
        "updated_at": _date(value.get("updated_at")),
        "html_url": f"https://github.com/{repository}/actions/runs/{number}",
        "run_attempt": value.get("run_attempt") if type(value.get("run_attempt")) is int else None,
    }


def read_github_snapshot(token: str) -> dict:
    """Read only RivaL public repo. Failure means unavailable, never an empty zero."""
    if not token:
        return {"status": "unavailable", "fetched_at": None, "prs": [], "runs": []}
    pulls = _github_json(
        "/pulls?" + parse.urlencode({
            "state": "all", "sort": "updated", "direction": "desc", "per_page": "25"
        }), token
    )
    result = _github_json("/actions/runs?per_page=45", token)
    if not isinstance(pulls, list) or not isinstance(result, dict):
        raise TypeError("github_response_schema_invalid")
    raw_runs = result.get("workflow_runs")
    if not isinstance(raw_runs, list):
        raise TypeError("github_runs_missing")
    by_number: dict[int, dict] = {}
    for item in pulls:
        if isinstance(item, dict):
            clean = safe_pr(item)
            if clean:
                by_number[clean["number"]] = clean
    for number in PINNED_PRS:
        if number not in by_number:
            item = _github_json(f"/pulls/{number}", token, limit=350_000)
            clean = safe_pr(item) if isinstance(item, dict) else None
            if clean and clean["number"] == number:
                by_number[number] = clean
    clean_runs = [
        value for item in raw_runs if isinstance(item, dict)
        if (value := safe_run(item)) is not None
    ]
    if not by_number:
        raise ValueError("github_pulls_empty")
    return {
        "status": "verified",
        "fetched_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "prs": sorted(by_number.values(), key=lambda x: x["number"], reverse=True)[:40],
        "runs": clean_runs[:45],
    }



def read_public_intelligence(token: str) -> dict:
    """Public repository-only state, NOT evidence of live LLM inference.

    Never read private environment data, workflow logs, release secrets, model
    weights, prompts, or conversation inputs.
    """
    unavailable = {
        "status": "unavailable", "repository": INTELLIGENCE_REPO,
        "fetched_at": None, "head_sha": None, "prs": [], "runs": [],
        "runtime_inference_verified": False,
    }
    if not token:
        return unavailable
    try:
        commit = _github_json("/commits/main", token, limit=250_000,
                              repository=INTELLIGENCE_REPO)
        pulls = _github_json("/pulls?state=all&per_page=12&sort=updated",
                             token, limit=250_000, repository=INTELLIGENCE_REPO)
        actions = _github_json("/actions/runs?per_page=15", token,
                               limit=400_000, repository=INTELLIGENCE_REPO)
        if not isinstance(commit, dict) or not isinstance(pulls, list) or not isinstance(actions, dict):
            raise TypeError("intelligence_github_schema_invalid")
        sha = _sha(commit.get("sha"))
        if not sha or not isinstance(actions.get("workflow_runs"), list):
            raise ValueError("intelligence_public_evidence_incomplete")
        cleaned_prs = [
            p for x in pulls if isinstance(x, dict)
            if (p := safe_pr(x, repository=INTELLIGENCE_REPO)) is not None
        ][:12]
        cleaned_runs = [
            r for x in actions["workflow_runs"] if isinstance(x, dict)
            if (r := safe_run(x, repository=INTELLIGENCE_REPO)) is not None
        ][:15]
        return {
            "status": "verified", "repository": INTELLIGENCE_REPO,
            "fetched_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "head_sha": sha, "prs": cleaned_prs, "runs": cleaned_runs,
            "runtime_inference_verified": False,
        }
    except (OSError, ValueError, TypeError, KeyError):
        return unavailable
