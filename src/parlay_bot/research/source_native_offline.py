"""Private source-native offline ML research, not trading or live prediction.

Reads only fully verified completed venue/month snapshots from the existing
RivaL Drive archive, trains the existing exact feature/target model families,
publishes native-parity research-only candidates, and leaves all promotion
and NO CALL gates unchanged. Skips incomplete six-month sources.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from parlay_bot.config import Settings
from parlay_bot.research.audit import audit_catalog
from parlay_bot.research.batch import complete_months
from parlay_bot.research.boosted_models import evaluate_directory as evaluate_boosters
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.live_training import evaluate_directory as evaluate_logistic
from parlay_bot.research.production import _report
from parlay_bot.research.publish import _artifact, _drive
from parlay_bot.research.source_native import SOURCE, VENUES, restore_verified_source_months


async def run_native_research(
    *, drive, venue: str, months: list[str], root: Path,
    report_dir: Path, publish: bool,
) -> dict:
    """Research-only. A successful run is NOT forward accuracy or profitability."""
    if venue not in VENUES or len(months) != 6:
        raise HistoryImportError("six exact consecutive months and active venue required")
    # Contiguous calendar months and one venue only; never stitch distant
    # periods into a fake six-month learning window.
    from parlay_bot.research.audit import _next_month
    if sorted(months) != months or any(
        _next_month(a) != b for a, b in pairwise(months)
    ):
        raise HistoryImportError("nonconsecutive source-native research months")
    artifacts = await restore_verified_source_months(
        drive, venue=venue, months=months, root=root,
    )
    quality = audit_catalog(root, months=months, sources=(SOURCE,))
    requested = [s for s in quality["streams"] if s["venue"] == venue]
    qualified = {
        (s["venue"], s["contract"], s["quote"], s["interval_minutes"])
        for s in requested
        if s["months_present"] == len(months) and s["research_grade"]
        and not s["missing_months"] and s["interval_minutes"] == 5
    }
    if quality["validation_failures"]:
        # A single forged/malformed or re-imported source file blocks the
        # entire run. Never select the cleaner-looking part of corrupt inputs.
        raise HistoryImportError("native research has invalid source partitions")
    if not qualified:
        return {
            "status": "source_months_incomplete", "venue": venue,
            "requested_months": months, "restored_manifests": len(artifacts),
            "qualified_source_scopes": 0, "published_models": 0,
            "promoted_live": False, "profitability_verified": False,
        }
    logistic = evaluate_logistic(root, qualified_streams=qualified)
    boosted = evaluate_boosters(
        root, qualified_streams=qualified,
        artifact_dir=report_dir / "source-native-boosted",
    )
    report = {
        "schema_version": "rival-native-source-research-v1",
        "venue": venue, "months": months, "source_scopes": sorted(
            [{"venue": s[0], "contract": s[1], "quote": s[2], "interval": s[3]}
             for s in qualified], key=lambda x: (x["contract"], x["quote"])),
        "restored_manifests": len(artifacts),
        "compatible_candidate_count": len(logistic),
        "compatible_screened": sum(x["research_candidate_passed"] for x in logistic),
        "boosted_scope_count": len(boosted),
        "portable_artifact_count": sum(
            len(x.get("portable_candidates", [])) for x in boosted
        ),
        "portable_retrospective_passed": sum(
            x["retrospective_passed"]
            for group in boosted for x in group.get("portable_candidates", [])
        ),
        "live_approved": False, "trades_placed": False,
        "real_fills_verified": False, "after_cost_gain_verified": False,
    }
    file = _report(report_dir / f"source-{venue}-research.json", report)
    published = []
    if publish:
        for evaluation in logistic:
            model = {
                "research_only": True, "live_model_promoted": False,
                "model_sha256": evaluation["model_sha256"],
                "model_spec": evaluation["model_spec"],
                "validation": evaluation["validation"],
                "out_of_sample_test": evaluation["out_of_sample_test"],
            }
            candidate_file = _report(
                report_dir / f"candidate-{evaluation['model_sha256'][:20]}.json",
                model,
            )
            item = _artifact(candidate_file, 1)
            restored = await drive.ensure_research_output(
                artifact=item, batch_id="candidate-" + item.sha256[:24],
                kind="model",
            )
            published.append(restored.file_id)
        for group in boosted:
            for model in group.get("portable_candidates", []):
                item = _artifact(Path(model["path"]), 1)
                if item.size_bytes != model["size"]:
                    raise HistoryImportError("portable model bytes changed after training")
                restored = await drive.ensure_research_output(
                    artifact=item, batch_id="candidate-" + item.sha256[:24],
                    kind="model",
                )
                published.append(restored.file_id)
        report_item = _artifact(file, 1)
        await drive.ensure_research_output(
            artifact=report_item, batch_id="report-" + report_item.sha256[:24],
            kind="report",
        )
    return {
        **report, "status": "verified_research_published" if publish
        else "verified_research_local_only",
        "published_models": len(published),
        "promoted_live": False, "profitability_verified": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Source-exact private offline RivaL model research")
    parser.add_argument("--venue", choices=sorted(VENUES), required=True)
    parser.add_argument("--months", type=int, choices=(6,), default=6)
    parser.add_argument("--root", type=Path, default=Path("data/native-research"))
    parser.add_argument("--report-dir", type=Path, default=Path("data/native-reports"))
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args(argv)
    drive = _drive(Settings())

    async def worker():
        try:
            return await run_native_research(
                drive=drive, venue=args.venue,
                months=complete_months(now=datetime.now(UTC), count=args.months),
                root=args.root, report_dir=args.report_dir,
                publish=args.publish,
            )
        finally:
            await drive.aclose()
    try:
        result = asyncio.run(worker())
    except (HistoryImportError, OSError, RuntimeError) as exc:
        parser.exit(1, f"RivaL native research blocked safely: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    # Missing provider coverage is recorded, never a false model promotion.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
