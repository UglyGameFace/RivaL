"""One bounded acquisition -> audit -> offline evaluation -> Drive restore workflow.

Never writes to live prediction tables or executes orders. A report is eligible
as *research evidence*, never automatically a promoted live trading model.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from parlay_bot.archive.drive import GoogleDriveArchiveClient
from parlay_bot.config import Settings
from parlay_bot.research.audit import audit_catalog
from parlay_bot.research.batch import _already_present, collect, complete_months, targets
from parlay_bot.research.boosted_models import evaluate_directory as evaluate_boosted_models
from parlay_bot.research.free_history import ASSETS, INTERVALS, LEAGUES, HistoryImportError
from parlay_bot.research.learning import evaluate_directory
from parlay_bot.research.live_training import evaluate_directory as evaluate_live_contracts
from parlay_bot.research.path_research import evaluate_directory as evaluate_first_passage
from parlay_bot.research.publish import _artifact, _drive, prepare, publish

DEFAULT_INTERVALS = ("1m", "5m", "15m", "1h", "4h", "1d")


def _report(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    sha = hashlib.sha256(encoded).hexdigest()
    filename = path.parent / f"{path.stem}-{sha[:20]}.json"
    if filename.exists() and filename.read_bytes() != encoded:
        raise HistoryImportError("existing research report changed")
    if not filename.exists():
        filename.write_bytes(encoded)
    return filename


def _manifest_paths(root: Path) -> list[Path]:
    return sorted(
        list((root / "binance_um_klines").glob("*.manifest.json"))
        + list((root / "football_data").glob("*.manifest.json"))
    )


async def _archive(
    *, root: Path, drive: GoogleDriveArchiveClient, report_path: Path,
    research: dict, max_publications: int = 200,
) -> dict:
    """Every result requires a verified, downloaded exact-byte restore."""
    files = _manifest_paths(root)
    if len(files) > max_publications:
        raise HistoryImportError("too many historical partitions for one archive run")
    uploaded = []
    for path in files:
        item = prepare(path)
        uploaded.append(await publish(drive, item))
    report_artifact = _artifact(report_path, 1)
    report_obj = await drive.ensure_research_output(
        artifact=report_artifact,
        batch_id="report-" + report_artifact.sha256[:24], kind="report",
    )
    models = []
    # Publish compatible artifacts last so bounded startup reads find the
    # nine current serving scopes before legacy experimental formats.
    for evaluation in research.get("evaluations", []) + research.get("compatible_evaluations", []):
        spec = evaluation["model_spec"]
        data = {
            "research_only": True, "live_model_promoted": False,
            "model_sha256": evaluation["model_sha256"], "model_spec": spec,
            "validation": evaluation["validation"],
            "out_of_sample_test": evaluation["out_of_sample_test"],
        }
        path = _report(
            report_path.parent / f"candidate-{evaluation['model_sha256'][:20]}.json",
            data,
        )
        artifact = _artifact(path, 1)
        obj = await drive.ensure_research_output(
            artifact=artifact, batch_id="candidate-" + artifact.sha256[:24],
            kind="model",
        )
        models.append({"model_sha256": evaluation["model_sha256"],
                       "drive_id": obj.file_id, "readback_verified": True})
    return {
        "archived_partitions": len(uploaded), "verified_history_ids": uploaded,
        "report_drive_id": report_obj.file_id,
        "model_candidates_stored": models,
        "real_drive_restore_verified": True,
    }


async def run_pipeline(
    *, root: Path, months: list[str], assets: list[str],
    intervals: list[str], seasons: list[str], leagues: list[str],
    collect_enabled: bool, publish_enabled: bool, max_new: int,
    report_dir: Path, client: httpx.Client | None = None,
    drive: GoogleDriveArchiveClient | None = None,
) -> dict:
    if not assets or not intervals or any(x not in ASSETS for x in assets):
        raise HistoryImportError("invalid or empty requested market coverage")
    if any(v not in INTERVALS for v in intervals):
        raise HistoryImportError("invalid research interval")
    if len(assets) != len(set(assets)) or len(intervals) != len(set(intervals)):
        raise HistoryImportError("duplicate market coverage requested")
    jobs = targets(
        months=months, assets=assets, intervals=intervals,
        seasons=seasons, leagues=leagues,
    )
    acquisition = {"planned": len(jobs), "downloaded": 0, "rejected": 0,
                   "remaining": sum(not _already_present(root, j) for j in jobs)}
    if collect_enabled:
        owner = client is None
        http = client or httpx.Client(timeout=35)
        try:
            # Run at most three bounded passes, allowing 100+ distinct
            # partitions without turning provider outages into an infinite loop.
            for _ in range(3):
                part = collect(http, out=root, jobs=jobs, max_new=max_new)
                acquisition["downloaded"] += part["downloaded"]
                acquisition["rejected"] += part["rejected"]
                acquisition["failed"] = part["failed"]
                acquisition["remaining"] = part["remaining"]
                acquisition["already_verified"] = part["already_verified"]
                if part["rejected"] or part["remaining"] == 0:
                    break
        finally:
            if owner:
                http.close()
    quality = audit_catalog(root, months=months)
    expected_streams = {(a + "USDT", INTERVALS[i]) for a in assets for i in intervals}
    valid = {
        (s["contract"], s["interval_minutes"])
        for s in quality["streams"] if s["research_grade"]
    }
    missing_streams = sorted(
        [{"contract": c, "interval_minutes": i}
         for c, i in expected_streams - valid],
        key=lambda e: (e["contract"], e["interval_minutes"]),
    )
    quality["all_requested_streams_qualified"] = (
        not missing_streams and not quality["validation_failures"]
    )
    quality["missing_or_unqualified_streams"] = missing_streams
    expected_sports = {(league, season) for season in seasons for league in leagues}
    saved_sports = {
        (league, season)
        for league, season in expected_sports
        if _already_present(root, ("football", league, season, ""))
    }
    quality["missing_sports_archives"] = [
        {"league": league, "season": season}
        for league, season in sorted(expected_sports - saved_sports)
    ]
    research = evaluate_directory(
        root, intervals=tuple(sorted({INTERVALS[i] for i in intervals})),
        qualified_streams=valid & expected_streams,
    )
    research["compatible_evaluations"] = evaluate_live_contracts(
        root, qualified_streams=valid & expected_streams,
    )
    # Tree models are trained on the same verified, purged source splits.
    # Their reports are research-only; weights cannot be imported into the
    # logistic checkpoint registry and never serve signals.
    research["boosted_evaluations"] = evaluate_boosted_models(
        root, qualified_streams=valid & expected_streams,
    )
    # Evaluate target-before-stop path distributions on the SAME audited
    # five-minute futures manifests. These are separate research-only
    # scenario models, never interchangeable with live binary labels or
    # Polymarket/Kalshi contracts.
    research["path_research"] = evaluate_first_passage(
        root, qualified_streams=valid & expected_streams,
    )
    # Report crypto completeness independently. Optional football history
    # must never make verified six-month crypto learning look unavailable.
    crypto_complete = (
        quality["all_requested_streams_qualified"]
        and not any(
            job[0] == "binance" and not _already_present(root, job)
            for job in jobs
        )
    )
    data_complete = (
        crypto_complete
        and not quality["missing_sports_archives"]
        and not acquisition["remaining"]
    )
    report = {
        "schema_version": "rival-full-history-pipeline-v1",
        "source_months": months, "assets": assets, "intervals": intervals,
        "acquisition": acquisition, "quality": quality, "research": research,
        "crypto_sources_complete": crypto_complete,
        "compatible_candidate_count": len(research["compatible_evaluations"]),
        "boosted_model_evaluations": len(research["boosted_evaluations"]),
        "boosted_models_evaluated": sum(
            item.get("status") == "evaluated"
            for item in research["boosted_evaluations"]
        ),
        "boosted_candidates_retrospectively_passed": sum(
            item.get("research_candidate_passed") is True
            for item in research["boosted_evaluations"]
        ),
        "path_research_evaluated": len(research["path_research"]["evaluations"]),
        "path_research_candidates_passed": sum(
            bool(item["research_candidate_passed"])
            for item in research["path_research"]["evaluations"]
        ),
        "compatible_candidate_passed": sum(
            bool(item["research_candidate_passed"])
            for item in research["compatible_evaluations"]
        ),
        "all_requested_data_complete": data_complete,
        "live_model_promoted": False, "live_accuracy_improved_verified": False,
        "auto_betting_enabled": False,
        "production_ready": False,
    }
    report_file = _report(report_dir / "history-learning-report.json", report)
    report["local_report_path"] = str(report_file)
    if publish_enabled:
        if not quality["all_requested_streams_qualified"]:
            report["warehouse_status"] = "not_published_incomplete_crypto_source_coverage"
            return report
        # A blocked football-odds endpoint must not destroy successfully
        # verified crypto history. Archive complete crypto separately and
        # mark unresolved sports as explicit partial production coverage.
        owner = drive is None
        connection = drive or _drive(Settings())
        try:
            uploaded = await _archive(
                root=root, drive=connection, report_path=report_file, research=research,
            )
            report["archive"] = uploaded
            report["warehouse_status"] = (
                "verified_byte_restores" if data_complete
                else "verified_crypto_only_sports_pending"
            )
        finally:
            if owner:
                await connection.aclose()
    else:
        report["warehouse_status"] = "not_published_no_authorization"
    # A complete archive is still research-only and not proof of profitable
    # predictions or successful private Discloud token permissions.
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RivaL complete historical research pipeline")
    parser.add_argument("--root", type=Path, default=Path("data/research-history"))
    parser.add_argument("--report-dir", type=Path, default=Path("data/research-reports"))
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--assets", nargs="+", choices=sorted(ASSETS),
                        default=["BTC", "ETH", "SOL"])
    parser.add_argument("--intervals", nargs="+", choices=sorted(INTERVALS),
                        default=list(DEFAULT_INTERVALS))
    # Sports backfill is a separate opt-in, not a prerequisite for a
    # monthly cryptocurrency model. Preserve explicit --seasons behavior.
    parser.add_argument("--seasons", nargs="*", default=[])
    parser.add_argument("--leagues", nargs="+", choices=sorted(LEAGUES),
                        default=["E0", "D1", "I1", "SP1", "F1"])
    parser.add_argument("--max-new", type=int, default=100)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--publish", action="store_true",
                        help="Requires authorized private Drive OAuth and models/reports folders")
    parser.add_argument("--plan", action="store_true", help="No network or file mutation")
    args = parser.parse_args(argv)
    if not 1 <= args.max_new <= 100:
        parser.error("max-new must be within 1..100")
    months = complete_months(now=datetime.now(UTC), count=args.months)
    if args.plan:
        print(json.dumps({"months": months, "jobs": targets(
            months=months, assets=args.assets, intervals=args.intervals,
            seasons=args.seasons, leagues=args.leagues,
        )}, indent=2))
        return 0
    try:
        report = asyncio.run(run_pipeline(
            root=args.root, months=months, assets=args.assets,
            intervals=args.intervals, seasons=args.seasons, leagues=args.leagues,
            collect_enabled=not args.skip_download, publish_enabled=args.publish,
            max_new=args.max_new, report_dir=args.report_dir,
        ))
    except (HistoryImportError, OSError, RuntimeError) as exc:
        parser.exit(1, f"Historical research pipeline blocked: {exc}\n")
    print(json.dumps({
        "acquisition": report["acquisition"],
        "qualified": report["all_requested_data_complete"],
        "evaluation_count": len(report["research"]["evaluations"]),
        "compatible_candidate_count": report["compatible_candidate_count"],
        "path_research_evaluated": report["path_research_evaluated"],
        "path_research_candidates_passed": report["path_research_candidates_passed"],
        "compatible_candidate_passed": report["compatible_candidate_passed"],
        "crypto_sources_complete": report["crypto_sources_complete"],
        "warehouse": report["warehouse_status"],
        "report": report["local_report_path"],
    }, indent=2, sort_keys=True))
    return 0 if report["all_requested_data_complete"] and (
        not args.publish or report["warehouse_status"] == "verified_byte_restores"
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
