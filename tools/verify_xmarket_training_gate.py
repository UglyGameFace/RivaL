"""Fail-closed admission proof for the one-off November XMarket fit.

Consumes only the short, sanitized readbacks from the existing source
recovery workflow. Never fetches raw prices or promotes a candidate.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ASSETS = ("BTC", "ETH", "SOL", "XRP")
HORIZONS = ("5", "15", "60")
SPLIT_MINIMUMS = {"train": 300, "validation": 100, "test": 100}
PINNED_SOURCE_RUN = 43641543527
PINNED_SOURCE_SHA = "b76731e787fe1a7dad5f222b7a934bca82a20121"
PINNED_TRAINED_THROUGH = "2026-11-13T23:55:00+00:00"


def verify_metadata(metadata: dict) -> None:
    expected = {
        "databaseId": PINNED_SOURCE_RUN,
        "headSha": PINNED_SOURCE_SHA,
        "status": "completed",
        "conclusion": "success",
        "event": "push",
    }
    for name, value in expected.items():
        if metadata.get(name) != value:
            raise ValueError(f"source run {name} does not match pinned successful recovery")


def verify_asset(asset: str, receipt: dict) -> dict:
    if receipt.get("asset") != asset:
        raise ValueError(f"{asset}: source receipt asset mismatch")
    if receipt.get("eligible_historical_splits_proven") is not True:
        raise ValueError(f"{asset}: historical input admission not proven")
    restore = receipt.get("input_restoration") or {}
    if restore.get("historical_split_status") != "eligible":
        raise ValueError(f"{asset}: verified original restoration not eligible")
    evidence = receipt.get("xmarket_admission") or {}
    if evidence.get("status") != "exact_features_joined":
        raise ValueError(f"{asset}: source features were not exactly joined")
    if evidence.get("trained_through_utc") != PINNED_TRAINED_THROUGH:
        raise ValueError(f"{asset}: source-readback clock differs from training clock")
    if evidence.get("train_end_utc") != "2026-09-19T00:00:00+00:00":
        raise ValueError(f"{asset}: training split cutoff changed")
    if evidence.get("test_start_utc") != "2026-10-17T00:00:00+00:00":
        raise ValueError(f"{asset}: test split cutoff changed")
    fidelity = evidence.get("verified_native_fidelity") or {}
    if not all(fidelity.get(provider) is True for provider in ("polymarket", "kalshi")):
        raise ValueError(f"{asset}: independently verified native fidelity is missing")
    if evidence.get("training_or_model_published") is not False:
        raise ValueError(f"{asset}: admission receipt must not claim a fitted model")

    horizons = evidence.get("horizons") or {}
    counts = {}
    for horizon in HORIZONS:
        result = horizons.get(horizon) or {}
        if (result.get("status") != "admission_only"
                or result.get("split_eligible") is not True
                or result.get("model_fitted") is not False
                or result.get("model_published") is not False):
            raise ValueError(f"{asset}/{horizon}m: not an eligible read-only admission")
        actual = {}
        for split, floor in SPLIT_MINIMUMS.items():
            record = (result.get("split_rows") or {}).get(split) or {}
            rows = record.get("rows")
            if not isinstance(rows, int) or isinstance(rows, bool) or rows < floor:
                raise ValueError(f"{asset}/{horizon}m: {split} below unchanged row floor")
            if record.get("shortfall") != 0:
                raise ValueError(f"{asset}/{horizon}m: unresolved {split} shortfall")
            actual[split] = rows
        counts[horizon] = actual
    return {"asset": asset, "verified": True, "split_rows": counts,
            "model_fitted": False, "profitability_proven": False}


def verify(root: Path, metadata: dict) -> list[dict]:
    verify_metadata(metadata)
    results = []
    for asset in ASSETS:
        path = root / asset / f"recorded-partial-proof-{asset}.json"
        if not path.is_file():
            raise ValueError(f"{asset}: missing private-source admission receipt")
        results.append(verify_asset(asset, json.loads(path.read_text(encoding="utf-8"))))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--run-metadata", type=Path, required=True)
    args = parser.parse_args(argv)
    metadata = json.loads(args.run_metadata.read_text(encoding="utf-8"))
    results = verify(args.root, metadata)
    print("XMARKET_TRAINING_SOURCE_GATE=" + json.dumps(
        {"source_run": PINNED_SOURCE_RUN, "status": "admission_proven_no_fit",
         "assets": results}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
