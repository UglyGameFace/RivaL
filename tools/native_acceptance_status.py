"""Maintain one honest, durable source-native acceptance status on Issue #151.

This job only reports authenticated private-warehouse research observations.
It never imports models, alters live forecasts, or calls historical training
a future performance result. All observed source/month details are bounded.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

MARKER = "<!-- rival-source-native-acceptance-status-v1 -->"
ISSUE = 151
_REPO = "UglyGameFace/RivaL"


def latest_report(report_dir: Path) -> dict | None:
    """Read the run's final hash-suffixed native report, not a stale file."""
    paths = sorted(
        report_dir.glob("source-*-research-*.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if not paths:
        return None
    data = json.loads(paths[0].read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != "rival-native-source-research-v1":
        raise ValueError("source-native report has an invalid schema")
    return data


def _safe_label(value: object) -> str:
    label = str(value)[:70]
    return label if re.fullmatch(r"[A-Za-z0-9_.:/-]+", label) else "unavailable"


def render_status(report: dict | None, run_url: str) -> str:
    """No possible report can claim import, live improvement, or trading gains."""
    url = run_url if re.fullmatch(
        r"https://github\.com/UglyGameFace/RivaL/actions/runs/[0-9]+", run_url
    ) else "https://github.com/UglyGameFace/RivaL/actions"
    lines = [
        MARKER,
        "### Source-native production acceptance (automatically maintained)",
        "",
        f"Latest [authenticated research attempt]({url}):",
    ]
    if report is None:
        lines.extend([
            "**BLOCKED: No source-native research report was produced.**",
            ("Check the job for missing private Drive authorization, restore errors, "
             "or failures before research. This is not a successful training run."),
        ])
    else:
        status = report.get("status")
        count = report.get("published_models", 0)
        venue = _safe_label(report.get("venue", "unavailable"))
        if status == "verified_research_published" and type(count) is int and count > 0:
            lines.append(
                f"**RESEARCH PUBLISHED:** {count} checksum-restored research "
                f"model file(s) for source `{venue}`. Production import remains unverified."
            )
        else:
            lines.append(
                f"**BLOCKED:** source `{venue}`, reason "
                f"`{_safe_label(status)}`; no qualified native model publication proven."
            )
        lines.append(
            f"Restored native manifests in this attempt: "
            f"{report.get('restored_manifests', 'unknown')}."
        )
        scopes = report.get("source_scopes", [])
        if isinstance(scopes, list):
            for entry in scopes[:12]:
                if not isinstance(entry, dict) or "missing_or_incomplete_months" not in entry:
                    continue
                missing = entry["missing_or_incomplete_months"]
                if not isinstance(missing, list):
                    continue
                missing_label = ", ".join(_safe_label(m) for m in missing[:6]) or "none"
                lines.append(
                    f"- `{_safe_label(entry.get('contract'))}/"
                    f"{_safe_label(entry.get('quote'))}`: "
                    f"{entry.get('verified_full_months', 'unknown')}/6 complete months; "
                    f"missing or incomplete: {missing_label}"
                )
    lines.extend([
        "",
        ("**Still independently required, regardless of a green workflow:** "
        "proof of the exact published model's import into hosted PostgreSQL; "
        "unattended model-SHA-specific predictions issued before outcomes; "
        "150 unique resolved future targets spanning 21 UTC days; "
        "at least 80% matched exact-venue quote coverage; "
        "positive challenger-versus-incumbent severe and conservative "
        "after-cost *paper* comparisons and calibration checks."),
        "",
        ("Real fills, fees, funding, and slippage are not independently verified. "
        "**NO CALL and incumbent retention remain authoritative.** "
        "This status is private archive research evidence, **not** live-trading proof."),
    ])
    return "\n".join(lines)


def upsert_issue_status(repo: str, body: str) -> None:
    """Edit one comment instead of notifying everyone with daily duplicates."""
    if repo != _REPO or not os.environ.get("GH_TOKEN"):
        raise RuntimeError("production repository or GitHub Actions token missing")
    selected = subprocess.run(
        [
            "gh", "api", f"repos/{repo}/issues/{ISSUE}/comments?per_page=100",
            "--paginate", "--jq",
            f'.[] | select(.body | contains("{MARKER}")) | .id',
        ],
        text=True, capture_output=True, check=True,
    )
    ids = [line.strip() for line in selected.stdout.splitlines() if line.strip().isdigit()]
    if ids:
        args = ["gh", "api", f"repos/{repo}/issues/comments/{ids[-1]}",
                "--method", "PATCH", "-f", f"body={body}"]
    else:
        args = ["gh", "api", f"repos/{repo}/issues/{ISSUE}/comments",
                "--method", "POST", "-f", f"body={body}"]
    subprocess.run(args, text=True, capture_output=True, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", type=Path, default=Path("data/native-reports"))
    args = parser.parse_args()
    report = latest_report(args.report_dir)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    body = render_status(
        report, f"https://github.com/{repo}/actions/runs/{run_id}"
    )
    upsert_issue_status(repo, body)
    print("Updated the existing RivaL source-native evidence status on Issue #151.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
