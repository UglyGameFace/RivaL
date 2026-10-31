"""Conservatively retire GitHub branches whose exact tip was merged.

No unmerged or changed branch is eligible. Requires an explicit --apply flag.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

REPOSITORY = "UglyGameFace/RivaL"
MAX_RETIREMENTS = 200


class GitHubAPI:
    def __init__(self, token: str):
        self.token = token

    def request(self, method: str, path: str):
        request = Request(
            f"https://api.github.com{path}",
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "rival-merged-branch-retirement",
            },
        )
        with urlopen(request, timeout=25) as response:
            payload = response.read()
        return json.loads(payload) if payload else None

    def pages(self, path: str):
        records = []
        page = 1
        while True:
            separator = "&" if "?" in path else "?"
            chunk = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            if not isinstance(chunk, list):
                raise ValueError(f"Expected a list from GitHub: {path}")
            records.extend(chunk)
            if len(chunk) < 100:
                return records
            page += 1


def retirement_candidates(branches: list[dict], pull_requests: list[dict]):
    """Require an exact merged PR head SHA from this repository, not merely a similar diff."""
    merged = set()
    active = set()
    for pr in pull_requests:
        head = pr.get("head") or {}
        source = head.get("repo") or {}
        if source.get("full_name", "").casefold() != REPOSITORY.casefold():
            continue
        name, sha = head.get("ref"), head.get("sha")
        if pr.get("state") == "open" and name:
            active.add(name)
        if pr.get("merged_at") and name and sha:
            merged.add((name, sha))

    eligible = []
    retained = []
    for branch in branches:
        name = branch["name"]
        sha = (branch.get("commit") or {}).get("sha")
        if name == "main" or branch.get("protected") or name in active:
            retained.append((name, "default, protected, or open PR"))
        elif sha and (name, sha) in merged:
            eligible.append((name, sha))
        else:
            retained.append((name, "unmatched PR SHA or unmerged work"))
    return eligible, retained


def ref_path(name: str) -> str:
    return f"/repos/{REPOSITORY}/git/refs/heads/{quote(name, safe='/')}"


def retire(api: GitHubAPI, candidates: list[tuple[str, str]], apply: bool):
    if len(candidates) > MAX_RETIREMENTS:
        raise RuntimeError("Retirement limit exceeded; inspect repository before proceeding")
    deleted = []
    skipped = []
    for name, expected_sha in candidates:
        path = ref_path(name)
        try:
            current = api.request("GET", path)
        except HTTPError as error:
            if error.code == 404:
                skipped.append((name, "already absent"))
                continue
            raise

        actual_sha = (current.get("object") or {}).get("sha")
        if current.get("ref") != f"refs/heads/{name}" or actual_sha != expected_sha:
            skipped.append((name, "branch changed since discovery"))
            continue

        if not apply:
            print(f"DRY RUN: eligible {name} @ {expected_sha}")
            continue

        # Recheck immediately before the delete, so stale discovery cannot authorize it.
        verified = api.request("GET", path)
        if (verified.get("object") or {}).get("sha") != expected_sha:
            skipped.append((name, "branch advanced before deletion"))
            continue

        try:
            api.request("DELETE", path)
        except HTTPError as error:
            if error.code == 404:
                skipped.append((name, "already absent"))
                continue
            raise
        deleted.append(name)
        print(f"RETIRED: {name}")
    return deleted, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Actually delete eligible refs")
    args = parser.parse_args()

    if os.getenv("GITHUB_REPOSITORY") != REPOSITORY:
        raise RuntimeError("Refusing to operate outside the RivaL repository")
    if os.getenv("GITHUB_REF") != "refs/heads/main":
        raise RuntimeError("Retirement is only permitted from main")
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required")

    api = GitHubAPI(token)
    branches = api.pages(f"/repos/{REPOSITORY}/branches")
    prs = api.pages(f"/repos/{REPOSITORY}/pulls?state=all")
    candidates, retained = retirement_candidates(branches, prs)
    print(
        f"Branches={len(branches)} merged-exact={len(candidates)} "
        f"retained={len(retained)} apply={args.apply}"
    )

    deleted, skipped = retire(api, candidates, args.apply)
    print(f"Retired={len(deleted)} skipped={len(skipped)}")
    for name, reason in retained + skipped:
        print(f"RETAINED: {name} ({reason})")

    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as output:
            output.write(
                "## RivaL verified branch retirement\n\n"
                f"- Mode: {'apply' if args.apply else 'dry-run'}\n"
                f"- Branches examined: {len(branches)}\n"
                f"- Exact merged-head candidates: {len(candidates)}\n"
                f"- Deleted: {len(deleted)}\n"
                f"- Preserved: {len(retained) + len(skipped)}\n\n"
                "Only exact merged PR branch heads are eligible.\n"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
