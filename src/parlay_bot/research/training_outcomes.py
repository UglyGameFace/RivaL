"""Separate successful research execution from accepted model fits."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

FAMILIES = ("archive_v1", "gbm_v1", "volcal_v1", "xmarket_v1")


def summarize(folder: Path, *, asset: str | None = None) -> dict:
    outcomes, errors = [], []
    for path in sorted(folder.glob("*-report.json")):
        try:
            report = json.loads(path.read_text())
            if report.get("family") not in FAMILIES:
                continue
            outcomes.append({"asset": report.get("asset"), "family": report['family'],
                             "horizon": report.get("horizon_minutes"),
                             "status": report.get("training_status", "unknown"),
                             "reasons": report.get("gate_failures", [])})
        except (OSError, ValueError, TypeError):
            errors.append(path.name)
    assets = (asset,) if asset else sorted({o['asset'] for o in outcomes if o['asset']})
    seen = {(o['asset'], o['family'], o['horizon']) for o in outcomes}
    missing = [f"{a}/{f}/{h}m" for a in assets for f in FAMILIES for h in (5, 15, 60)
               if (a, f, h) not in seen]
    return {"execution_complete": bool(outcomes) and not errors and not missing
            and all(o['status'] in ("passed", "rejected", "blocked") for o in outcomes),
            "accepted": sum(o['status'] == "passed" for o in outcomes),
            "rejected": sum(o['status'] == "rejected" for o in outcomes),
            "blocked": sum(o['status'] == "blocked" for o in outcomes),
            "outcomes": outcomes, "missing_reports": missing, "unreadable_reports": errors,
            "proves_forward_edge": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("--asset", choices=("BTC", "ETH", "SOL", "XRP"))
    args = parser.parse_args(argv)
    summary = summarize(args.folder, asset=args.asset)
    print(json.dumps(summary, sort_keys=True))
    lines = ["### Actual training outcomes", "",
             (f"Execution complete: {summary['execution_complete']}; "
             f"accepted: {summary['accepted']}; rejected: {summary['rejected']}; "
             f"blocked: {summary['blocked']}."),
             "Accepted offline fits still require independent forward and cost proof.", ""]
    for row in summary['outcomes']:
        lines.append(f"- {row['asset']} {row['horizon']}m {row['family']}: "
                     f"{row['status']} — {', '.join(map(str, row['reasons'])) or 'offline gates passed'}")
    lines += [f"- Missing report: {value}" for value in summary['missing_reports']]
    if target := os.getenv("GITHUB_STEP_SUMMARY"):
        with open(target, 'a', encoding='utf-8') as stream:
            stream.write('\n'.join(lines) + '\n')
    return 0 if summary['execution_complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
