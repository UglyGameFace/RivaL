"""Read-only replay workbench (design 2026-11-20, phase 1).

Grades registered candidates from the Model Lab and XMarket factories
against RivaL's settled record, paired against the market's own
issuance-time price after fees, following
``parlay_bot.perpetuals.replay_workbench``. SELECT-only -- it never
writes to the store, never issues a forecast, flips no switch, and is
not wired into any bot loop or schedule. A PASS licenses only a
proposal for a registered prospective test (design section 8).

Screens:
    recalibration     R1: Platt / isotonic / shrinkage variants of the
                      live model's settled Polymarket calls.
    xmarket           X1-X5: the five pinned XMarket fits on their
                      frozen prospective shadow record.
    coinbase-premium  R3: premium conditioning on the live model's
                      settled BTC calls (needs --premium-file from
                      tools/fetch_coinbase_premium.py, or
                      --fetch-premium where the archives are reachable;
                      --record-file grades an exported record instead
                      of the store).
    liquidation       R2: registered DATA-BLOCKED (design 5.2); prints
                      the block, runs nothing.
    generation        G-A/G-B (phase-2 amendment): mask logistic
                      refits + book-state conditioning on the settled
                      forward record.
    estate            G-C: the threshold estate, graded under the
                      owner-approved floors (2026-11-25) against the
                      settled threshold outcomes; NOT_MEASURABLE with
                      floor progress below the floors.
    llm-intake        G-D: the frozen proposer's stored feature masks,
                      validated, deduplicated, registered, and graded
                      against the settled lineage record (the recorded
                      exchange feature vectors in
                      rival_forward_model_lineage).

Phase-2 screens record their per-candidate results into the
workbench's own results table (the Models board reads it). Phase-1
verdicts enter the same table via --import-report on their report
files -- import records the verdicts as computed once; it recomputes
nothing.

Usage (on the host, from the deployed source tree):
    python tools/replay_workbench.py --screen recalibration [--output r.json]
    python tools/replay_workbench.py --screen xmarket [--scope BTC:15]
    python tools/replay_workbench.py --screen coinbase-premium \
        --premium-file premium.jsonl [--record-file btc_record.jsonl]
    python tools/replay_workbench.py --export-record btc.jsonl --asset BTC
    python tools/replay_workbench.py --screen generation [--output g.json]
    python tools/replay_workbench.py --screen estate
    python tools/replay_workbench.py --screen llm-intake
    python tools/replay_workbench.py --import-report wb_r1.json \
        --import-report wb_x.json --import-report wb_r3.json \
        --import-report wb_r2.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from parlay_bot.config import Settings
from parlay_bot.perpetuals import replay_generation as gen
from parlay_bot.perpetuals import replay_workbench as wb
from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore

MARKER = "RIVAL_REPLAY_WORKBENCH="


def _store():
    settings = Settings()
    if settings.rival_storage_backend == "postgres":
        if settings.rival_database_url is None:
            return None
        return PostgresHotStore(settings.rival_database_url.get_secret_value())
    return SQLiteHotStore(settings.rival_db_path)


def _row_to_json(row: dict) -> dict:
    out = dict(row)
    out["issued_at"] = row["issued_at"].isoformat()
    out["outcome_observed_at"] = row["outcome_observed_at"].isoformat()
    return out


def _row_from_json(data: dict) -> dict:
    out = dict(data)
    out["issued_at"] = datetime.fromisoformat(data["issued_at"])
    out["outcome_observed_at"] = datetime.fromisoformat(
        data["outcome_observed_at"])
    return out


def _load_record_file(path: Path) -> list[dict]:
    return [_row_from_json(json.loads(line))
            for line in path.read_text().splitlines() if line.strip()]


def _generation_entries(masks: dict, conds: dict, *,
                        evaluated_at: str) -> list[dict]:
    meta = {c["id"]: c for c in
            gen.MASK_CANDIDATES + gen.CONDITIONING_CANDIDATES}
    entries = []
    for result in (masks, conds):
        for cid, cand in result["candidates"].items():
            m = meta[cid]
            if result["screen"] == "G-A":
                score = cand.get("score") or {}
                rows = score.get("rows") or 0
                calls = (score.get("calls") or {}).get("n") or 0
                days = score.get("days") or 0
            else:
                subset = cand.get("subset") or {}
                rows = (cand.get("coverage") or {}).get("subset_rows") or 0
                calls = subset.get("calls") or 0
                days = subset.get("call_days") or 0
            entries.append({
                "candidate_id": cid, "screen": m["screen"],
                "factory": m["factory"], "display": m["display"],
                "status": cand.get("status"), "verdict": cand.get("verdict"),
                "graded_rows": rows, "calls": calls, "days": days,
                "evaluated_at": evaluated_at,
                "detail": {"legs": cand.get("legs"),
                           "floors": cand.get("floors")},
            })
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only replay workbench (frozen criteria)")
    parser.add_argument("--screen", choices=(
        "recalibration", "xmarket", "coinbase-premium", "liquidation",
        "generation", "estate", "llm-intake"))
    parser.add_argument("--import-report", type=Path, action="append",
                        default=[], help="Phase-1 report JSON to record "
                        "into the results table (repeatable)")
    parser.add_argument("--scope", action="append", default=[],
                        help="XMarket scope ASSET:HORIZON (repeatable)")
    parser.add_argument("--asset", default=None,
                        help="Restrict the live record to one asset")
    parser.add_argument("--premium-file", type=Path, default=None)
    parser.add_argument("--fetch-premium", action="store_true",
                        help="Fetch the premium series in-process")
    parser.add_argument("--record-file", type=Path, default=None,
                        help="Grade an exported record JSONL (premium)")
    parser.add_argument("--export-record", type=Path, default=None,
                        help="Dump the live record as JSONL and exit")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    now = datetime.now(UTC)

    if args.screen == "liquidation":
        report = wb.base_report("R2", now=now)
        report.update({
            "status": "DATA-BLOCKED",
            "verdict": None,
            "reason": "No free public archive serves timestamped "
                      "liquidation events for the record window: "
                      "Binance Vision's futures catalog (daily and "
                      "monthly listings verified 2026-11-20) has no "
                      "liquidation dataset, and Bybit's public archive "
                      "exposes no liquidation directory (/liquidation/ "
                      "returns 404). Remaining sources are paid or "
                      "API-keyed. No substitutes, no synthetic data. "
                      "Criteria stay frozen in design 5.2.",
            "registry": wb.registry_report(),
        })
        return _emit(report, args.output)

    store = None
    if args.export_record is not None or args.record_file is None \
            or args.import_report:
        store = _store()
        if store is None:
            print(json.dumps({
                "status": "UNVERIFIED",
                "reason": "host database not configured"}))
            return 2

    if args.import_report:
        imported: list[dict] = []
        for path in args.import_report:
            imported.extend(gen.import_phase1_reports(
                [json.loads(path.read_text())]))
        recorded = gen.record_results(store, imported)
        if args.screen is None and args.export_record is None:
            report = gen.base_report("IMPORT", now=now)
            report.update({"imported_entries": len(imported),
                           "recorded_results": recorded})
            return _emit(report, args.output)

    if args.export_record is not None:
        rows, coverage = wb.load_live_record(store, asset=args.asset)
        args.export_record.write_text("\n".join(
            json.dumps(_row_to_json(r), sort_keys=True) for r in rows) + "\n")
        print(MARKER + json.dumps(
            {"exported_rows": len(rows), "coverage": coverage},
            sort_keys=True))
        return 0

    if args.screen == "recalibration":
        rows, coverage = wb.load_live_record(store, asset=args.asset)
        result = wb.screen_recalibration(rows)
        report = wb.base_report("R1", now=now)
        report.update(result)
        report["loader_coverage"] = coverage
        return _emit(report, args.output)

    if args.screen == "xmarket":
        wanted = set(args.scope) or None
        scopes = {}
        for spec in wb.XMARKET_SCOPES:
            key = f"{spec['asset']}:{spec['horizon']}"
            if wanted is not None and key not in wanted:
                continue
            rows, sha = wb.load_shadow_record(
                store, family=wb.XMARKET_FAMILY, asset=spec["asset"],
                horizon=spec["horizon"], sha_prefix=spec["sha_prefix"])
            if sha is None:
                scopes[key] = {
                    "screen": spec["screen"], "status": "NOT_MEASURABLE",
                    "verdict": None,
                    "reason": "pinned SHA prefix did not resolve to "
                              "exactly one ledger artifact"}
                continue
            block = wb.screen_xmarket(
                rows, asset=spec["asset"], horizon=spec["horizon"],
                family_size=len(wb.XMARKET_SCOPES))
            block["screen"] = spec["screen"]
            block["artifact_sha256"] = sha
            scopes[key] = block
        report = wb.base_report("X1-X5", now=now)
        report["scopes"] = scopes
        return _emit(report, args.output)

    if args.screen == "coinbase-premium":
        if args.record_file is not None:
            rows = _load_record_file(args.record_file)
            coverage = {"source": "record_file", "rows": len(rows)}
        else:
            rows, coverage = wb.load_live_record(store, asset="BTC")
        if args.premium_file is not None:
            points = []
            for line in args.premium_file.read_text().splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                points.append((datetime.fromisoformat(item["at"]),
                               float(item["premium"])))
        elif args.fetch_premium:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import fetch_coinbase_premium
            start = min(r["issued_at"] for r in rows)
            end = max(r["issued_at"] for r in rows)
            points, fetch_meta = \
                fetch_coinbase_premium.fetch_premium_series(start, end)
        else:
            print(json.dumps({
                "status": "UNVERIFIED",
                "reason": "--premium-file or --fetch-premium required"}))
            return 2
        result = wb.screen_premium(rows, points)
        report = wb.base_report("R3", now=now)
        report.update(result)
        report["loader_coverage"] = coverage
        report["premium_points"] = len(points)
        if args.fetch_premium:
            report["premium_fetch"] = fetch_meta
        return _emit(report, args.output)

    if args.screen == "generation":
        rows, coverage = gen.load_generation_record(store)
        masks = gen.screen_generation_masks(rows)
        conds = gen.screen_conditioning(rows)
        entries = _generation_entries(
            masks, conds, evaluated_at=now.isoformat())
        recorded = gen.record_results(store, entries)
        report = gen.base_report("G-A/G-B", now=now)
        report.update({"masks": masks, "conditioning": conds,
                       "loader_coverage": coverage,
                       "recorded_results": recorded})
        return _emit(report, args.output)

    if args.screen == "estate":
        result = gen.screen_estate(store)
        entries = []
        for cand in gen.ESTATE_CANDIDATES:
            graded = result["candidates"][cand["id"]]
            score = graded.get("score") or {}
            detail = {"legs": graded.get("legs"),
                      "floors": graded.get("floors"),
                      "grading_version": graded["grading_version"],
                      "coverage": graded.get("coverage"),
                      "estate_coverage": result["coverage"]}
            if graded.get("cause"):
                detail["cause"] = graded["cause"]
            if graded.get("reason"):
                detail["reason"] = graded["reason"]
            entries.append({
                "candidate_id": cand["id"], "screen": cand["screen"],
                "factory": cand["factory"], "display": cand["display"],
                "status": graded.get("status"),
                "verdict": graded.get("verdict"),
                "graded_rows": score.get("rows") or 0,
                "calls": (score.get("calls") or {}).get("n") or 0,
                "days": score.get("days") or 0,
                "evaluated_at": now.isoformat(),
                "detail": detail,
            })
        recorded = gen.record_results(store, entries)
        report = gen.base_report("G-C", now=now)
        report.update(result)
        report["recorded_results"] = recorded
        return _emit(report, args.output)

    if args.screen == "llm-intake":
        proposals, rejected = gen.load_llm_proposals(store)
        registrations = gen.llm_registrations(proposals)
        gd_rows, gd_coverage = gen.load_gd_record(store)
        graded = gen.screen_llm_masks(gd_rows, registrations)
        entries = []
        for reg in registrations:
            cand = graded["candidates"][reg["id"]]
            score = cand.get("score") or {}
            detail = {"legs": cand.get("legs"),
                      "floors": cand.get("floors"),
                      "grading_version": cand["grading_version"],
                      "asset": reg["asset"], "mask": reg["mask"],
                      "proposed_at": reg["proposed_at"],
                      "coverage": cand.get("coverage")}
            if cand.get("cause"):
                detail["cause"] = cand["cause"]
            if cand.get("reason"):
                detail["reason"] = cand["reason"]
            entries.append({
                "candidate_id": reg["id"], "screen": reg["screen"],
                "factory": reg["factory"], "display": reg["display"],
                "status": cand.get("status"),
                "verdict": cand.get("verdict"),
                "graded_rows": score.get("rows") or 0,
                "calls": (score.get("calls") or {}).get("n") or 0,
                "days": score.get("days") or 0,
                "evaluated_at": now.isoformat(),
                "detail": detail,
            })
        recorded = gen.record_results(store, entries)
        report = gen.base_report("G-D", now=now)
        report.update({
            "status": "GRADED",
            "grading_version": graded["grading_version"],
            "verified_proposals": len(proposals),
            "rejected": rejected,
            "registrations": registrations,
            "candidates": graded["candidates"],
            "loader_coverage": gd_coverage,
            "recorded_results": recorded,
            "registry": gen.generation_registry(),
        })
        return _emit(report, args.output)

    print(json.dumps({"status": "UNVERIFIED",
                      "reason": "--screen or --export-record required"}))
    return 2


def _emit(report: dict, output: Path | None) -> int:
    if output is not None:
        output.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(MARKER + json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
