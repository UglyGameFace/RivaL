"""Nightly: train RivaL's pooled multi-year model and publish it to Drive.

Research runner only (GitHub Actions). Downloads checksum-verified Binance
originals (the same files RivaL archives), trains one model per horizon on
every coin together, and publishes each model JSON to the private Drive
models folder. The bot imports only models whose gates passed, runs them as
live challengers, and grades them on real future outcomes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import feature_signal_experiment as source

from parlay_bot.config import Settings
from parlay_bot.perpetuals.pooled_features import ASSETS
from parlay_bot.perpetuals.pooled_model import family, model_sha256
from parlay_bot.research.pooled_training import frame_features, train_horizon
from parlay_bot.research.publish import _artifact, _drive


async def load(cache: Path, last_month: str) -> dict:
    import httpx

    sem = asyncio.Semaphore(16)
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        return {asset: await source.load_asset(client, asset, last_month, cache, sem)
                for asset in ASSETS}


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
    parser.add_argument("--out", type=Path, default=Path("pooled-models"))
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    raw = asyncio.run(load(args.cache, args.last_month))
    frames = {}
    for asset in ASSETS:
        k = raw[asset][["open", "high", "low", "close", "volume"]].astype(float)
        btc = raw["BTC"][["close"]].astype(float)
        frames[asset] = (k, frame_features(k, btc, asset))
        print(f"features {asset}: {len(k):,} bars", flush=True)
    trained_through = max(k.index.max() for k, _ in frames.values())
    # Evaluate on the most recent ~10 months, validate on the year before.
    test_start = pd.Timestamp(trained_through).tz_convert("UTC").normalize() - pd.Timedelta(days=300)
    train_end = test_start - pd.Timedelta(days=365)
    args.out.mkdir(parents=True, exist_ok=True)
    summary, published = {}, []
    for horizon in (5, 15, 60):
        doc = train_horizon(frames, horizon, train_end=train_end, test_start=test_start,
                            trained_through=trained_through)
        path = args.out / f"pooled-{horizon}m-{datetime.now(UTC):%Y%m%d}-{model_sha256(doc)[:12]}.json"
        path.write_text(json.dumps(doc, sort_keys=True, separators=(",", ":")))
        size = path.stat().st_size
        summary[f"{horizon}m"] = {
            "family": family(doc), "gates_passed": doc["gates_passed"], "bytes": size,
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
    print("TRAINING_SUMMARY=" + json.dumps(summary, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
