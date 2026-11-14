"""One-off training requires the exact finished recovery run and every true fold."""

from __future__ import annotations

import copy
import json

import pytest

from tools.verify_xmarket_training_gate import (
    ASSETS,
    PINNED_SOURCE_RUN,
    PINNED_SOURCE_SHA,
    verify,
    verify_asset,
    verify_metadata,
)


def _metadata() -> dict:
    return {
        "databaseId": PINNED_SOURCE_RUN,
        "headSha": PINNED_SOURCE_SHA,
        "status": "completed",
        "conclusion": "success",
        "event": "push",
    }


def _receipt(asset: str) -> dict:
    return {
        "asset": asset,
        "eligible_historical_splits_proven": True,
        "input_restoration": {"historical_split_status": "eligible"},
        "xmarket_admission": {
            "status": "exact_features_joined",
            "trained_through_utc": "2026-11-13T23:55:00+00:00",
            "train_end_utc": "2026-09-19T00:00:00+00:00",
            "test_start_utc": "2026-10-17T00:00:00+00:00",
            "verified_native_fidelity": {"polymarket": True, "kalshi": True},
            "training_or_model_published": False,
            "horizons": {
                h: {
                    "status": "admission_only",
                    "split_eligible": True,
                    "model_fitted": False,
                    "model_published": False,
                    "split_rows": {
                        "train": {"rows": 302, "shortfall": 0, "unique_bars": 151},
                        "validation": {"rows": 102, "shortfall": 0, "unique_bars": 51},
                        "test": {"rows": 105, "shortfall": 0, "unique_bars": 53},
                    },
                } for h in ("5", "15", "60")
            },
        },
    }


def test_exact_original_source_run_and_four_asset_receipts_are_accepted(tmp_path):
    for asset in ASSETS:
        folder = tmp_path / asset
        folder.mkdir()
        (folder / f"recorded-partial-proof-{asset}.json").write_text(
            json.dumps(_receipt(asset)), encoding="utf-8"
        )
    result = verify(tmp_path, _metadata())
    assert [row["asset"] for row in result] == list(ASSETS)
    assert all(row["verified"] and row["model_fitted"] is False for row in result)


@pytest.mark.parametrize(("field", "bad"), [
    ("databaseId", PINNED_SOURCE_RUN + 1),
    ("headSha", "f" * 40),
    ("status", "in_progress"),
    ("conclusion", "failure"),
    ("event", "workflow_dispatch"),
])
def test_only_exact_completed_successful_pushed_run_can_trigger_fit(field, bad):
    data = _metadata()
    data[field] = bad
    with pytest.raises(ValueError, match="pinned successful recovery"):
        verify_metadata(data)


@pytest.mark.parametrize("broken", [
    "fidelity",
    "native_status",
    "source_clock",
    "train_clock",
    "test_clock",
    "proof",
    "restore",
    "unproven_horizon",
    "already_fitted",
    "too_few_train",
    "too_few_validation",
    "too_few_test",
    "remaining_shortfall",
    "missing_horizon",
])
def test_ineligible_source_cannot_be_upgraded_to_a_training_authorization(broken):
    document = copy.deepcopy(_receipt("BTC"))
    evidence = document["xmarket_admission"]
    if broken == "fidelity":
        evidence["verified_native_fidelity"]["kalshi"] = False
    elif broken == "native_status":
        evidence["status"] = "no_complete_source_and_label_rows"
    elif broken == "source_clock":
        evidence["trained_through_utc"] = "2026-11-14T23:55:00+00:00"
    elif broken == "train_clock":
        evidence["train_end_utc"] = "2026-09-20T00:00:00+00:00"
    elif broken == "test_clock":
        evidence["test_start_utc"] = "2026-10-18T00:00:00+00:00"
    elif broken == "proof":
        document["eligible_historical_splits_proven"] = False
    elif broken == "restore":
        document["input_restoration"]["historical_split_status"] = "blocked"
    elif broken == "unproven_horizon":
        evidence["horizons"]["60"]["split_eligible"] = False
    elif broken == "already_fitted":
        evidence["horizons"]["5"]["model_fitted"] = True
    elif broken in ("too_few_train", "too_few_validation", "too_few_test"):
        split = broken.removeprefix("too_few_")
        evidence["horizons"]["15"]["split_rows"][split]["rows"] = 2
    elif broken == "remaining_shortfall":
        evidence["horizons"]["15"]["split_rows"]["validation"]["shortfall"] = 1
    elif broken == "missing_horizon":
        del evidence["horizons"]["5"]
    with pytest.raises(ValueError):
        verify_asset("BTC", document)


def test_missing_one_asset_receipt_blocks_the_whole_four_asset_fit(tmp_path):
    for asset in ASSETS[:-1]:
        folder = tmp_path / asset
        folder.mkdir()
        (folder / f"recorded-partial-proof-{asset}.json").write_text(
            json.dumps(_receipt(asset)), encoding="utf-8"
        )
    with pytest.raises(ValueError, match="XRP: missing"):
        verify(tmp_path, _metadata())


def test_mislabeled_asset_receipt_blocks_fit():
    with pytest.raises(ValueError, match="asset mismatch"):
        verify_asset("BTC", _receipt("ETH"))
