"""Regression tests for conservative GitHub branch retirement."""
from tools.retire_merged_branches import retire, retirement_candidates


def branch(name, sha="abc", protected=False):
    return {"name": name, "commit": {"sha": sha}, "protected": protected}


def pr(name, sha="abc", *, merged=True, state="closed", source="UglyGameFace/RivaL"):
    return {
        "head": {"ref": name, "sha": sha, "repo": {"full_name": source}},
        "merged_at": "2026-10-31T06:00:00Z" if merged else None,
        "state": state,
    }


def test_exact_merged_head_only():
    branches = [
        branch("main"),
        branch("fix/merged"),
        branch("fix/changed", "advanced"),
        branch("fix/unmerged"),
        branch("fix/open"),
        branch("fix/from-fork"),
        branch("fix/protected", protected=True),
        branch("fix/untracked"),
    ]
    prs = [
        pr("fix/merged"),
        pr("fix/changed"),
        pr("fix/unmerged", merged=False),
        pr("fix/open"),
        pr("fix/open", merged=False, state="open"),
        pr("fix/from-fork", source="someone-else/RivaL"),
        pr("fix/protected"),
    ]
    candidates, retained = retirement_candidates(branches, prs)
    assert candidates == [("fix/merged", "abc")]
    assert len(retained) == 7


class FakeAPI:
    def __init__(self, versions):
        self.versions = versions
        self.calls = []

    def request(self, method, path):
        self.calls.append((method, path))
        if method == "DELETE":
            return None
        name = path.partition("/heads/")[2]
        values = self.versions[name]
        sha = values.pop(0) if len(values) > 1 else values[0]
        return {"ref": f"refs/heads/{name}", "object": {"sha": sha}}


def test_dry_run_does_not_delete():
    api = FakeAPI({"fix/merged": ["abc"]})
    deleted, skipped = retire(api, [("fix/merged", "abc")], apply=False)
    assert (deleted, skipped) == ([], [])
    assert all(method == "GET" for method, _ in api.calls)


def test_branch_advance_cancels_deletion():
    api = FakeAPI({"fix/merged": ["abc", "changed"]})
    deleted, skipped = retire(api, [("fix/merged", "abc")], apply=True)
    assert deleted == []
    assert skipped == [("fix/merged", "branch advanced before deletion")]
    assert all(method == "GET" for method, _ in api.calls)


def test_matching_tip_is_deleted_only_after_two_checks():
    api = FakeAPI({"fix/merged": ["abc"]})
    deleted, skipped = retire(api, [("fix/merged", "abc")], apply=True)
    assert deleted == ["fix/merged"]
    assert skipped == []
    assert [method for method, _ in api.calls] == ["GET", "GET", "DELETE"]
