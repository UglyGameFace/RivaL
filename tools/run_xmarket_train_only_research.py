"""Bounded one-off XMarket feature research from verified original archives.

Never triggers original market backfill, Drive publications, model imports,
outer holdout evaluation, live inference or trading.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from parlay_bot.config import Settings
from parlay_bot.research.archive_training import load_frames_manifests
from parlay_bot.research.publish import _drive
from parlay_bot.research.xmarket_history import read_verified_inputs_only
from parlay_bot.research.xmarket_training import train_run

SCOPE_HORIZONS = (5, 15, 60)


async def _read_originals(asset: str) -> dict:
    drive = _drive(Settings())
    try:
        return await read_verified_inputs_only(drive, asset=asset)
    finally:
        await drive.aclose()


def run(*, asset: str, manifests: Path, train_end: str,
        test_start: str, trained_through: str,
        okx_source_integrity: str | None = None) -> dict:
    # Never expose authenticated Drive identifiers or returned native quotes.
    result = {"schema": "rival-xmarket-train-only-evidence-v1",
              "asset": asset, "research_only": True,
              "source_archives_modified": False,
              "models_published": 0, "trades_placed": 0, "scopes": []}
    try:
        frame_options = (
            {"okx_source_integrity": okx_source_integrity}
            if okx_source_integrity is not None else {}
        )
        frames, provenance = load_frames_manifests(
            manifests, target_asset=asset, **frame_options,
        )
        result["okx_selected_source_integrity"] = provenance.get(
            "okx_selected_source_integrity"
        )
        result["okx_unselected_manifests"] = provenance.get(
            "okx_unselected_manifests", 0
        )
        if not provenance.get("provider_sha256_by_month"):
            raise ValueError("no verified provider-origin candle manifests")
        market_data = asyncio.run(_read_originals(asset))
        result["native_source_status"] = {
            s: market_data.get("verification", {}).get(s, {}).get("status", "missing")
            for s in ("polymarket", "kalshi")
        }
        for horizon in SCOPE_HORIZONS:
            training = train_run(
                frames, asset=asset, horizon=horizon,
                train_end=datetime.fromisoformat(train_end),
                test_start=datetime.fromisoformat(test_start),
                trained_through=trained_through, provenance=provenance,
                code_version="train-only-prospective-research",
                market_data=market_data, research_only=True,
            )
            if training["artifact"] is not None or training["publish_kind"] != "report":
                raise AssertionError("train-only code tried to create an importable model")
            report = training["report"]
            ablation = report.get("train_only_feature_ablation") or {
                "status": "source_admission_blocked", "profiles": [],
                "research_only": True, "can_publish_model": False,
            }
            result["scopes"].append({
                "horizon_minutes": horizon, "training_status": report.get("training_status"),
                "gate_failures": report.get("gate_failures", []),
                "split_rows": report.get("split_rows", {}),
                "original_overlap_eligible": report.get("overlap_evidence", {}).get("eligible", False),
                "ablation": ablation,
                "outer_validation_test_scored": False,
            })
        result["status"] = ("measured" if len(result["scopes"]) == 3 and
                            all(s["ablation"]["status"] == "measured_train_only"
                                for s in result["scopes"]) else "incomplete")
    except Exception as exc:  # noqa: BLE001 - fail closed and save class-only diagnostics
        result["status"] = "unavailable"
        result["error_class"] = type(exc).__name__
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only XMarket training-only feature research")
    parser.add_argument("--asset", required=True, choices=("BTC", "ETH", "SOL", "XRP"))
    parser.add_argument("--manifests-dir", type=Path, required=True)
    parser.add_argument(
        "--okx-source-integrity",
        choices=("okx-v5-public-history-5m-v1",
                 "okx-v5-official-1m-zip-derived-5m-v1"),
        default=None,
        help="Use only the explicitly restored SHA-verified OKX native provider family",
    )
    parser.add_argument("--train-end", required=True)
    parser.add_argument("--test-start", required=True)
    parser.add_argument("--trained-through", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    receipt = run(asset=args.asset, manifests=args.manifests_dir,
                  train_end=args.train_end, test_start=args.test_start,
                  trained_through=args.trained_through)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(receipt, sort_keys=True, indent=2), encoding="utf-8")
    print(json.dumps({"asset": args.asset, "status": receipt["status"],
                      "scopes": [{"horizon": s["horizon_minutes"],
                                  "status": s["ablation"]["status"]}
                                 for s in receipt.get("scopes", [])]}))
    return 0 if receipt["status"] == "measured" else 2


if __name__ == "__main__":
    raise SystemExit(main())
