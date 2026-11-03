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
from collections import Counter
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from parlay_bot.config import Settings
from parlay_bot.research.audit import audit_catalog
from parlay_bot.research.batch import complete_months
from parlay_bot.research.boosted_models import evaluate_directory as evaluate_boosters
from parlay_bot.research.candidate_registry import verified_candidate
from parlay_bot.research.free_history import HistoryImportError
from parlay_bot.research.llm_feature_ablation import (
    evaluate_hypothesis_ablations,
    verify_proposals,
)
from parlay_bot.research.live_training import evaluate_directory as evaluate_logistic
from parlay_bot.research.production import _report
from parlay_bot.research.publish import _artifact, _drive
from parlay_bot.research.source_native import SOURCE, VENUES, restore_verified_source_months


def select_training_months(*, now: datetime, first_month: str | None) -> list[str]:
    """Select any exact six completed UTC calendar months from ONE source.

    A historical window can begin in 2021, 2023 or later, independently for
    each venue/asset. Six contiguous verified months remain a MODEL integrity
    requirement, not a global historical download cutoff.
    """
    if first_month is None:
        return complete_months(now=now, count=6)
    if now.utcoffset() is None:
        raise ValueError("training requires a timezone-aware clock")
    try:
        first = datetime.strptime(first_month, "%Y-%m").replace(tzinfo=UTC)
    except (TypeError, ValueError) as exc:
        raise ValueError("first_month must be YYYY-MM") from exc
    if first.strftime("%Y-%m") != first_month or first_month < "2021-01":
        raise ValueError("invalid historical training month")
    selected = []
    for n in range(6):
        idx = first.year * 12 + first.month - 1 + n
        year, month = divmod(idx, 12)
        selected.append(f"{year:04d}-{month + 1:02d}")
    if selected[-1] >= now.astimezone(UTC).strftime("%Y-%m"):
        raise ValueError("model research cannot use an unfinished calendar month")
    return selected



def screening_receipt(logistic: list[dict], boosted: list[dict]) -> dict:
    """Preflight exact live importer contracts before publishing any models.

    A candidate can be an honest historical experiment but still fail the
    actual immutable registry's retrospective gate. Reuse that registry as the
    one screening authority, never guess qualification from trainer labels.
    """
    rejections: Counter[str] = Counter()
    source_scopes = []
    passed = 0
    candidates = []
    eligible_hashes = []
    for experiment in logistic:
        candidates.append((
            json.dumps({
                "research_only": True, "live_model_promoted": False,
                "model_sha256": experiment["model_sha256"],
                "model_spec": experiment["model_spec"],
                "validation": experiment["validation"],
                "out_of_sample_test": experiment["out_of_sample_test"],
            }, sort_keys=True, separators=(",", ":")).encode(),
            experiment["research_candidate_passed"],
        ))
    for batch in boosted:
        for portable in batch.get("portable_candidates", []):
            candidates.append((
                Path(portable["path"]).read_bytes(),
                portable["retrospective_passed"],
            ))
    for payload, trainer_passed in candidates:
        try:
            admission = verified_candidate(payload)
        except (ValueError, KeyError, TypeError) as exc:
            # Generated files that cannot be imported must never be published.
            raise HistoryImportError(
                "native model artifact failed the live importer contract"
            ) from exc
        imported_passed = admission["retrospective_screen"] == "passed"
        if imported_passed != trainer_passed:
            raise HistoryImportError(
                "native model training and registry screening disagree"
            )
        passed += int(imported_passed)
        if imported_passed:
            eligible_hashes.append(admission["model_sha256"])
        else:
            rejections[admission["admission_reason"]] += 1
        identity = admission["model_spec"]["identity"]
        evidence = json.loads(payload)
        validation = evidence["validation"]
        holdout = evidence["out_of_sample_test"]
        # Score values come from immutable chronological evaluation; they
        # describe performance, never justify retrospective promotions.
        source_scopes.append({
            "model_sha256": admission["model_sha256"],
            "venue": identity["venue"], "contract": identity["contract"],
            "quote": identity["quote"], "asset": admission["asset"],
            "horizon": admission["horizon"],
            "family": admission["model_spec"]["model"],
            "screen": admission["admission_reason"],
            "validation_brier_gain": validation.get("brier_gain"),
            "holdout_brier_gain": holdout.get("brier_gain"),
            "validation_ece_10": validation.get("ece_10"),
            "holdout_ece_10": holdout.get("ece_10"),
            "validation_days": validation.get("days"),
            "holdout_days": holdout.get("days"),
        })
    return {
        "evaluated": len(candidates),
        "retrospective_passed": passed,
        "retrospective_rejected": len(candidates) - passed,
        "eligible_model_sha256": sorted(eligible_hashes),
        "rejected_by_reason": [
            {"reason": reason, "count": count}
            for reason, count in sorted(rejections.items())
        ],
        "source_specific_outcomes": source_scopes[:80],
        "proves_prospective_edge": False,
    }


async def run_native_research(
    *, drive, venue: str, months: list[str], root: Path,
    report_dir: Path, publish: bool, asset: str | None = None,
    llm_hypotheses: list[dict] | None = None,
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
    if venue == "bitget":
        # Original API months live in a distinct Drive format. Restore them
        # through their strict original-response SHA/UTC verifier, not the
        # PostgreSQL source-native snapshot reader. Never mix both formats.
        from parlay_bot.research.bitget_source_bridge import (
            restore_verified_bitget_market_months,
        )

        artifacts = await restore_verified_bitget_market_months(
            drive, months=months, root=root, asset=asset or "BTC",
        )
    else:
        if asset is not None:
            raise HistoryImportError("asset selection applies only to Bitget")
        artifacts = await restore_verified_source_months(
            drive, venue=venue, months=months, root=root,
        )
    quality = audit_catalog(root, months=months, sources=(SOURCE,))
    requested = [s for s in quality["streams"] if s["venue"] == venue]
    # The general history audit permits small research-grade gaps. This
    # acceptance milestone is stricter: every one of the six *source-native*
    # calendar partitions must be present and have zero missing 5m candles.
    full_partitions = {
        (p["venue"], p["contract"], p["quote"], p["interval_minutes"], p["month"])
        for p in quality["partitions"]
        if p["full_calendar_month_verified"]
    }
    qualified = {
        (s["venue"], s["contract"], s["quote"], s["interval_minutes"])
        for s in requested
        if s["months_present"] == len(months) and s["research_grade"]
        and not s["missing_months"] and s["interval_minutes"] == 5
        and all(
            (s["venue"], s["contract"], s["quote"], 5, month) in full_partitions
            for month in months
        )
    }
    if quality["validation_failures"]:
        # A single forged/malformed or re-imported source file blocks the
        # entire run. Never select the cleaner-looking part of corrupt inputs.
        raise HistoryImportError("native research has invalid source partitions")
    if not qualified:
        blocked = {
            "schema_version": "rival-native-source-research-v1",
            "status": "source_months_incomplete", "venue": venue,
            "asset": asset if venue == "bitget" else None,
            "requested_months": months, "restored_manifests": len(artifacts),
            "qualified_source_scopes": 0, "published_models": 0,
            "source_scopes": [
                {
                    "contract": s["contract"], "quote": s["quote"],
                    "interval": s["interval_minutes"],
                    "verified_full_months": sum(
                        (s["venue"], s["contract"], s["quote"],
                         s["interval_minutes"], month) in full_partitions
                        for month in months
                    ),
                    "missing_or_incomplete_months": [
                        month for month in months
                        if (s["venue"], s["contract"], s["quote"],
                            s["interval_minutes"], month) not in full_partitions
                    ],
                }
                for s in requested
            ],
            "live_approved": False, "promoted_live": False,
            "profitability_verified": False,
        }
        _report(report_dir / f"source-{venue}-research.json", blocked)
        return blocked
    logistic = evaluate_logistic(root, qualified_streams=qualified)
    boosted = evaluate_boosters(
        root, qualified_streams=qualified,
        artifact_dir=report_dir / "source-native-boosted",
    )
    admission = screening_receipt(logistic, boosted)
    llm_trials = evaluate_hypothesis_ablations(
        root, qualified_streams=qualified,
        proposals=llm_hypotheses or [],
    )
    report = {
        "schema_version": "rival-native-source-research-v1",
        "venue": venue, "months": months,
        "asset": asset if venue == "bitget" else None,
        "source_scopes": sorted(
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
        "actual_registry_screening": admission,
        "llm_hypothesis_research": {
            "status": "validation_experiments_only"
                      if llm_hypotheses else "no_verified_proposals",
            "received_proposals": len(llm_hypotheses or []),
            "evaluated": len(llm_trials),
            "experiments": llm_trials[:48],
            "sealed_holdout_examined": False,
            "changes_live_weights": False,
        },
        "live_approved": False, "trades_placed": False,
        "real_fills_verified": False, "after_cost_gain_verified": False,
    }
    published = []
    rejected_archives = []
    eligible_hashes = set(admission["eligible_model_sha256"])
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
            qualified = evaluation["model_sha256"] in eligible_hashes
            restored = await drive.ensure_research_output(
                artifact=item, batch_id="candidate-" + item.sha256[:24],
                kind="model" if qualified else "report",
            )
            (published if qualified else rejected_archives).append(restored.file_id)
        for group in boosted:
            for model in group.get("portable_candidates", []):
                item = _artifact(Path(model["path"]), 1)
                if item.size_bytes != model["size"]:
                    raise HistoryImportError("portable model bytes changed after training")
                qualified = model["model_sha256"] in eligible_hashes
                restored = await drive.ensure_research_output(
                    artifact=item, batch_id="candidate-" + item.sha256[:24],
                    kind="model" if qualified else "report",
                )
                (published if qualified else rejected_archives).append(restored.file_id)
    result = {
        **report,
        "status": (
            "verified_research_published" if publish and published
            else "verified_research_archived_no_qualified_models"
            if publish and rejected_archives
            else "source_models_not_published" if publish
            else "verified_research_local_only"
        ),
        # Eligible model imports and rejected immutable research are separate
        # destinations. Never classify a rejected artifact as a model.
        "published_models": len(published),
        "archived_rejected_models": len(rejected_archives),
        "published_research_artifacts": len(published) + len(rejected_archives),
        "promoted_live": False, "profitability_verified": False,
    }
    # Persist the actual final outcome; the previous report was written
    # before publication and could not report how many files reached Drive.
    file = _report(report_dir / f"source-{venue}-research.json", result)
    if publish:
        report_item = _artifact(file, 1)
        await drive.ensure_research_output(
            artifact=report_item, batch_id="report-" + report_item.sha256[:24],
            kind="report",
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Source-exact private offline RivaL model research")
    parser.add_argument("--venue", choices=sorted(VENUES), required=True)
    parser.add_argument("--months", type=int, choices=(6,), default=6)
    parser.add_argument("--first-month", default=None)
    parser.add_argument("--asset", choices=("BTC", "ETH", "SOL", "XRP"))
    parser.add_argument("--root", type=Path, default=Path("data/native-research"))
    parser.add_argument("--report-dir", type=Path, default=Path("data/native-reports"))
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--llm-hypotheses-file", type=Path, default=None)
    args = parser.parse_args(argv)
    llm_hypotheses = []
    if args.llm_hypotheses_file is not None:
        try:
            receipt = json.loads(
                args.llm_hypotheses_file.read_text(encoding="utf-8")
            )
            llm_hypotheses = verify_proposals(
                receipt, venue=args.venue, now=datetime.now(UTC),
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            parser.exit(
                2, f"Unverified LFM3 research input: {type(exc).__name__}\\n",
            )
    drive = _drive(Settings())

    async def worker():
        try:
            return await run_native_research(
                drive=drive, venue=args.venue,
                months=select_training_months(now=datetime.now(UTC), first_month=args.first_month),
                root=args.root, report_dir=args.report_dir,
                publish=args.publish, asset=args.asset,
                llm_hypotheses=llm_hypotheses,
            )
        finally:
            await drive.aclose()
    try:
        result = asyncio.run(worker())
    except (HistoryImportError, OSError, RuntimeError) as exc:
        parser.exit(1, f"RivaL native research blocked safely: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    # A green Actions job must mean verified publication, not merely a clean
    # Python exit after zero restored months or zero published candidates.
    status = result.get("status")
    if not args.publish:
        return 0 if status == "verified_research_local_only" else 2
    if status == "verified_research_published":
        return 0 if result.get("published_models", 0) > 0 else 2
    if status == "verified_research_archived_no_qualified_models":
        screening = result.get("actual_registry_screening") or {}
        return 0 if (
            result.get("archived_rejected_models", 0) > 0
            and screening.get("evaluated") == result["archived_rejected_models"]
            and screening.get("retrospective_passed") == 0
            and result.get("published_models") == 0
        ) else 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
