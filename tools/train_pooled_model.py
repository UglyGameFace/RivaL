"""Nightly: train RivaL's pooled multi-year model and publish it to Drive.

Research runner only (GitHub Actions). Reads every source file from the
private Drive warehouse first; a file the warehouse lacks is downloaded once
from Binance, checksum-verified and saved into the warehouse. Two input
versions are trained per horizon (price-only v1; v2 adds Binance flow,
premium and funding); the better one on the VALIDATION year is published,
provided it also passes every test gate. The bot imports only models whose gates passed, runs them as
live challengers, and grades them on real future outcomes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import feature_signal_experiment as source

from parlay_bot.config import Settings
from parlay_bot.perpetuals.pooled_features import ASSETS, FEATURE_VERSION, FEATURE_VERSION_V2
from parlay_bot.perpetuals.pooled_model import family, model_sha256
from parlay_bot.research.pooled_training import frame_context, frame_features, train_horizon
from parlay_bot.research.publish import _artifact, _drive
from parlay_bot.research.training_warehouse import TrainingWarehouse


async def load(cache: Path, last_month: str, *, use_warehouse: bool,
               through_day=None) -> tuple[dict, dict]:
    import httpx

    sem = asyncio.Semaphore(16)
    drive = _drive(Settings()) if use_warehouse else None
    warehouse = None
    try:
        if drive is not None:
            warehouse = TrainingWarehouse(drive, cache)
            await warehouse.load_index()
        fetcher = warehouse.fetcher(source.fetch) if warehouse else None
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            raw = {asset: await source.load_asset(client, asset, last_month, cache, sem,
                                                  fetcher=fetcher, through_day=through_day)
                   for asset in ASSETS}
        return raw, (warehouse.stats if warehouse else {"warehouse": "not used"})
    finally:
        if drive is not None:
            await drive.aclose()


async def publish(paths: list[Path]) -> list[str]:
    drive = _drive(Settings())
    try:
        ids = []
        for path in paths:
            artifact = _artifact(path, 1)
            stored = await drive.ensure_research_output(
                artifact=artifact, batch_id="pooled-model-" + artifact.sha256[:24],
                kind="model",
            )
            ids.append(stored.file_id)
        return ids
    finally:
        await drive.aclose()


def main() -> int:
    import pandas as pd

    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=Path("binance-cache"))
    parser.add_argument("--last-month", required=True)
    parser.add_argument("--through-day", type=date.fromisoformat, default=None,
                        help="also train on daily files through this UTC day")
    parser.add_argument("--out", type=Path, default=Path("pooled-models"))
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--no-warehouse", action="store_true",
                        help="read Binance directly instead of the Drive warehouse")
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    raw, warehouse_stats = asyncio.run(load(args.cache, args.last_month,
                                            use_warehouse=not args.no_warehouse,
                                            through_day=args.through_day))
    print("WAREHOUSE=" + json.dumps(warehouse_stats), flush=True)
    frames = {}
    for asset in ASSETS:
        full = raw[asset]
        k = full[["open", "high", "low", "close", "volume"]].astype(float)
        btc = raw["BTC"][["close"]].astype(float)
        context = frame_context(full[["volume", "taker_buy_volume", "count", "prem",
                                      "fund"]].astype(float))
        frames[asset] = (k, pd.concat([frame_features(k, btc, asset), context], axis=1))
        print(f"features {asset}: {len(k):,} bars", flush=True)
    trained_through = max(k.index.max() for k, _ in frames.values())
    # Evaluate on the most recent ~10 months, validate on the year before.
    test_start = pd.Timestamp(trained_through).tz_convert("UTC").normalize() - pd.Timedelta(days=300)
    train_end = test_start - pd.Timedelta(days=365)
    args.out.mkdir(parents=True, exist_ok=True)
    summary, published = {}, []
    for horizon in (5, 15, 60):
        candidates = [train_horizon(frames, horizon, train_end=train_end,
                                    test_start=test_start, trained_through=trained_through,
                                    feature_version=version)
                      for version in (FEATURE_VERSION, FEATURE_VERSION_V2)]
        for c in candidates:
            print(json.dumps({f"{horizon}m/{c['feature_version']}": {
                "gates_passed": c["gates_passed"],
                "validation_gain": c["evaluation"]["validation"]["brier_gain"],
                "test_gain": c["evaluation"]["test"]["brier_gain"]}}), flush=True)
        # Choose on validation only; the test year stays an untouched check.
        passing = [c for c in candidates if c["gates_passed"]] or candidates
        doc = max(passing, key=lambda c: (c["evaluation"]["validation"]["brier_gain"],
                                          c["feature_version"] == FEATURE_VERSION))
        # The price-only model is ALWAYS published too (when it passes): the
        # bot serves it whenever live Binance context is unavailable.
        fallback = next(c for c in candidates if c["feature_version"] == FEATURE_VERSION)
        if fallback is not doc and fallback["gates_passed"]:
            extra = args.out / (f"pooled-{horizon}m-{datetime.now(UTC):%Y%m%d}-"
                                f"{model_sha256(fallback)[:12]}.json")
            extra.write_text(json.dumps(fallback, sort_keys=True, separators=(",", ":")))
            if extra.stat().st_size <= 2_200_000:
                published.append(extra)
        path = args.out / f"pooled-{horizon}m-{datetime.now(UTC):%Y%m%d}-{model_sha256(doc)[:12]}.json"
        path.write_text(json.dumps(doc, sort_keys=True, separators=(",", ":")))
        size = path.stat().st_size
        summary[f"{horizon}m"] = {
            "family": family(doc), "feature_version": doc["feature_version"],
            "gates_passed": doc["gates_passed"], "bytes": size,
            "validation": doc["evaluation"]["validation"], "test": doc["evaluation"]["test"],
            "portable_max_error": doc["portable_max_error"],
        }
        if doc["gates_passed"] and size <= 2_200_000:
            published.append(path)
        print(json.dumps({f"{horizon}m": summary[f"{horizon}m"]}), flush=True)
    if args.publish and published:
        ids = asyncio.run(publish(published))
        print(f"published {len(ids)} model(s) to Drive", flush=True)
    elif not published:
        print("no model passed its gates; nothing published", flush=True)
    summary["warehouse"] = warehouse_stats
    print("TRAINING_SUMMARY=" + json.dumps(summary, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
