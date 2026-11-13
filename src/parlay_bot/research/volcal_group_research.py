"""Same-source, same-outcome VolCal feature-group ablation evidence.

Research only. The four ZIP-derived candle groups already exist in
volcal_features; their scheduled variants were often on different assets.
A cross-asset comparison cannot estimate a group's incremental value.

Each weekly asset/horizon job fits one *pre-scheduled* group-off ablation
against its unmodified full VolCal candidate on the identical frozen
archive frames, label horizons, purged splits and baselines. Rotate the
groups by UTC week, so each asset/horizon gets all four over four
independent runs without four extra fits on every weekly training job.
Neither variant is promoted or requalified by this diagnostic.
"""
from __future__ import annotations

import math
from datetime import UTC, date, datetime

from parlay_bot.research.volcal_features import FEATURE_CONTRACT, GROUPS

SCHEMA = "rival-volcal-paired-group-ablation-v1"
GROUP_ORDER = ("vol", "compression", "volume", "dc")
ASSET_ORDER = ("BTC", "ETH", "SOL", "XRP")
FIRST_WEEK = date(2026, 11, 12)


def _utc_clock(value) -> datetime:
    clock = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if clock.utcoffset() is None:
        raise ValueError("paired ablation clock must be timezone-aware")
    return clock.astimezone(UTC)


def scheduled_group(*, asset: str, trained_through) -> str:
    """Fixed pre-registered rotation, never chosen using model outcomes."""
    if asset not in ASSET_ORDER:
        raise ValueError("unsupported VolCal ablation asset")
    week = (_utc_clock(trained_through).date() - FIRST_WEEK).days // 7
    group = GROUP_ORDER[(ASSET_ORDER.index(asset) + week) % len(GROUP_ORDER)]
    assert group in GROUPS
    return group


def _split_metrics(report: dict) -> tuple[dict, dict, dict]:
    evaluation = report.get("evaluation")
    if not isinstance(evaluation, dict):
        raise TypeError("missing VolCal held-out evaluation")
    validation, test = evaluation.get("validation"), evaluation.get("test")
    if not isinstance(validation, dict) or not isinstance(test, dict):
        raise TypeError("VolCal test or validation evidence missing")
    for stage in (validation, test):
        if (type(stage.get("rows")) is not int or stage["rows"] <= 0
                or type(stage.get("days")) is not int or stage["days"] <= 0
                or not isinstance(stage.get("brier_gain"), (int, float))
                or not math.isfinite(stage["brier_gain"])):
            raise ValueError("invalid paired VolCal evaluation metrics")
    return evaluation, validation, test


def compare(full: dict, group_off: dict, *, asset: str, horizon: int,
            trained_through) -> dict:
    """A descriptive, co-issued Brier difference, never a promotion test.

    The existing VolCal train_run() constructs both candidates from the
    *same frames* and drops all incomplete features *before* masking a
    group. Therefore their label rows are identical, including the
    horizon purge and train-only baseline. Check the resulting split
    counts/dates before interpreting their Brier-gain difference.
    """
    group = scheduled_group(asset=asset, trained_through=trained_through)
    if group_off.get("feature_groups_off") != [group]:
        raise ValueError("wrong VolCal research ablation")
    for doc in (full, group_off):
        if (doc.get("family") != "volcal_v1"
                or doc.get("feature_contract") != FEATURE_CONTRACT
                or doc.get("asset") != asset
                or doc.get("horizon_minutes") != horizon):
            raise ValueError("cross-scope VolCal feature research comparison")
    full_eval, full_val, full_test = _split_metrics(full)
    alt_eval, alt_val, alt_test = _split_metrics(group_off)
    for field in ("train_rows", "train_before", "test_from"):
        if full_eval.get(field) != alt_eval.get(field):
            raise ValueError("VolCal ablation evaluation did not use the same train/test windows")
    for left, right in ((full_val, alt_val), (full_test, alt_test)):
        if (left["rows"], left["days"]) != (right["rows"], right["days"]):
            raise ValueError("VolCal ablation evaluation is not paired on source rows")
    return {
        "schema": SCHEMA,
        "status": "paired_descriptive_research_only",
        "asset": asset, "horizon_minutes": horizon, "group": group,
        "removed_features": len(GROUPS[group]),
        "frozen_source_clock": _utc_clock(trained_through).isoformat(),
        "train_rows": full_eval["train_rows"],
        "validation_rows": full_val["rows"], "validation_days": full_val["days"],
        "test_rows": full_test["rows"], "test_days": full_test["days"],
        "validation_brier_gain_full": full_val["brier_gain"],
        "validation_brier_gain_without_group": alt_val["brier_gain"],
        "test_brier_gain_full": full_test["brier_gain"],
        "test_brier_gain_without_group": alt_test["brier_gain"],
        "test_gain_from_including_group": round(
            full_test["brier_gain"] - alt_test["brier_gain"], 6),
        "full_offline_gates_passed": full.get("gates_passed") is True,
        "ablated_offline_gates_passed": group_off.get("gates_passed") is True,
        "group_selected_before_outcomes": True,
        "same_asset_horizon_source_and_test_rows": True,
        "multiple_comparisons_adjusted": False,
        "statistically_significant_superiority_proven": False,
        "prospective_forward_edge_proven": False,
        "after_cost_edge_proven": False,
        "model_artifact_published_by_research": False,
        "promotable_from_this_result": False,
    }


def evaluate(frames: dict, training: dict, full_report: dict) -> dict:
    """Run one bounded source-paired refit; never affect the full model."""
    from parlay_bot.research import volcal_training

    asset, horizon = training["asset"], training["horizon"]
    clock = training["trained_through"]
    group = scheduled_group(asset=asset, trained_through=clock)
    if not isinstance(full_report.get("evaluation"), dict):
        return {"schema": SCHEMA, "status": "full_model_not_evaluable",
                "group": group, "promotable_from_this_result": False}
    alternate = volcal_training.train_run(
        frames, **training, groups_off=(group,),
        include_research_diagnostics=False,
    )
    return compare(full_report, alternate["report"], asset=asset,
                   horizon=horizon, trained_through=clock)
