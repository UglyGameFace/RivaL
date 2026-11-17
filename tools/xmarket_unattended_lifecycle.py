"""Authenticated native-source to XMarket offline candidates; no live orders.

Only provider-hashed market candles and independent native PM/Kalshi originals.
An untouched outer holdout is never scored unless the inner train-only fold
is measured. A published candidate still requires on-host shadow/promotion.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from parlay_bot.config import Settings
from parlay_bot.research.archive_training import _git_sha, load_frames_manifests
from parlay_bot.research.publish import _drive
from parlay_bot.research.xmarket_history import read_verified_inputs_only
from parlay_bot.research.xmarket_training import (
    HORIZONS,
    _publish,
    _write_json,
    train_run,
    validate,
)

OKX_SOURCE = "okx-v5-public-history-5m-v1"
INNER_MIN_DAYS = 7
INNER_MIN_TARGETS = 100
INNER_MIN_FIT_ROWS = 300


async def _read_originals(asset: str) -> dict:
    """Use the established authenticated Drive reader, with no tool-path imports."""
    drive = _drive(Settings())
    try:
        return await read_verified_inputs_only(drive, asset=asset)
    finally:
        await drive.aclose()


def source_clock(as_of: date) -> tuple[datetime, datetime, datetime]:
    """Freeze canonical 28-day test/validation windows before source selection."""
    if type(as_of) is not date:
        raise ValueError("as_of must be a UTC date")
    midnight = datetime.combine(as_of, time.min, tzinfo=UTC)
    return (midnight - timedelta(days=56),
            midnight - timedelta(days=28),
            midnight - timedelta(minutes=5))


def _verified_fidelity(native: dict) -> dict[str, bool]:
    checks = native.get("verification") or {}
    return {source: checks.get(source, {}).get("status") == "verified"
            for source in ("polymarket", "kalshi")}


def _source_commitment(native: dict) -> str:
    """Nonreversible source-set commitment; never print private IDs or quotes."""
    values = sorted(str(r["sha256"]) for r in native.get("receipts", ())
                    if isinstance(r, dict) and isinstance(r.get("sha256"), str))
    return hashlib.sha256("\n".join(values).encode()).hexdigest()


def evaluate(*, asset: str, manifests: Path, as_of: date,
             publish_passing: bool = False) -> dict:
    """Produce only a whitelisted sanitized report from three exact scopes."""
    if asset not in ("BTC", "ETH", "SOL", "XRP"):
        raise ValueError("unsupported XMarket asset")
    train_end, test_start, through = source_clock(as_of)
    frames, provenance = load_frames_manifests(
        manifests, target_asset=asset, okx_source_integrity=OKX_SOURCE,
    )
    if not provenance.get("provider_sha256_by_month"):
        raise ValueError("no provider-hashed original manifests")
    native = asyncio.run(_read_originals(asset))
    fidelity = _verified_fidelity(native)
    result = {
        "schema": "rival-xmarket-source-to-model-v1",
        "asset": asset, "as_of_utc": as_of.isoformat(),
        "train_end_utc": train_end.isoformat(),
        "test_start_utc": test_start.isoformat(),
        "trained_through_utc": through.isoformat(),
        "verified_native_fidelity": fidelity,
        "verified_source_set_sha256": _source_commitment(native),
        "okx_source_integrity": OKX_SOURCE,
        "outer_holdouts_used_for_feature_selection": False,
        "real_money_orders": 0, "models_published": 0, "scopes": [],
    }
    if not all(fidelity.values()):
        result["status"] = "awaiting_verified_native_sources"
        for horizon in HORIZONS:
            result["scopes"].append({
                "horizon_minutes": horizon, "status": "source_blocked",
                "inner_eval_days": None, "missing_inner_days": None,
                "offline_gate_passed": False, "published": False,
            })
        return result

    for horizon in HORIZONS:
        kwargs = {
            "frames": frames, "asset": asset, "horizon": horizon,
            "train_end": train_end, "test_start": test_start,
            "trained_through": through, "provenance": provenance,
            "code_version": _git_sha(), "market_data": native,
        }
        inner = train_run(**kwargs, research_only=True)
        if inner.get("publish_kind") != "report" or inner.get("artifact") is not None:
            raise ValueError("inner research attempted to create a model")
        report = inner.get("report") or {}
        if (report.get("training_status") == "research_only"
                and report.get("outer_validation_test_scored") is not False):
            raise ValueError("inner research touched sealed holdout")
        ablation = report.get("train_only_feature_ablation") or {}
        fold = ablation.get("fold") or {}
        days, targets, fit_rows = (fold.get(k) for k in (
            "eval_days", "eval_targets", "fit_rows",
        ))
        measured = bool(
            report.get("training_status") == "research_only"
            and ablation.get("status") == "measured_train_only"
            and ablation.get("can_publish_model") is False
            and ablation.get("uses_outer_validation_or_test") is False
            and type(days) is int and days >= INNER_MIN_DAYS
            and type(targets) is int and targets >= INNER_MIN_TARGETS
            and type(fit_rows) is int and fit_rows >= INNER_MIN_FIT_ROWS
        )
        scope = {
            "horizon_minutes": horizon,
            "status": "inner_train_only_ready" if measured else "awaiting_inner_fold",
            "inner_eval_days": days if type(days) is int else None,
            "missing_inner_days": max(0, INNER_MIN_DAYS - days)
                                  if type(days) is int else None,
            "inner_eval_targets": targets if type(targets) is int else None,
            "offline_gate_passed": False, "published": False,
        }
        result["scopes"].append(scope)
        if not measured:
            continue

        # The untouched outer holdouts may ONLY be opened after the
        # independently purged inner fold meets the existing minimum.
        candidate = train_run(**kwargs)
        if candidate.get("publish_kind") != "model":
            if candidate.get("artifact") is not None:
                raise ValueError("rejected candidate carried importable weights")
            scope["status"] = "offline_gates_failed"
            scope["gate_failure_count"] = len(
                (candidate.get("report") or {}).get("gate_failures", [])
            )
            continue
        artifact = validate(candidate["artifact"])
        scope["offline_gate_passed"] = True
        scope["status"] = "offline_passed_not_published"
        if publish_passing:
            with TemporaryDirectory(prefix="rival-xmarket-private-") as tmp:
                # No full model artifact ever enters public Actions artifacts.
                private_path = _write_json(
                    Path(tmp) / f"{asset}-{horizon}m-model.json", artifact,
                )
                receipt = asyncio.run(_publish("model", private_path))
            if receipt.get("kind") != "model":
                raise ValueError("private Drive publication not confirmed")
            scope["published"] = True
            scope["status"] = "offline_candidate_published_pending_promotion"
            result["models_published"] += 1

    result["status"] = (
        "offline_candidates_published" if result["models_published"]
        else "offline_candidates_eligible" if any(
            s["offline_gate_passed"] for s in result["scopes"])
        else "awaiting_inner_fold" if any(
            s["status"] == "awaiting_inner_fold" for s in result["scopes"])
        else "offline_candidates_rejected"
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guarded XMarket native-to-model lifecycle")
    parser.add_argument("--asset", choices=("BTC", "ETH", "SOL", "XRP"), required=True)
    parser.add_argument("--manifests-dir", type=Path, required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--publish-passing", action="store_true")
    args = parser.parse_args(argv)
    if args.as_of > datetime.now(UTC).date():
        parser.error("source-run date cannot be in the future")
    try:
        result = evaluate(
            asset=args.asset, manifests=args.manifests_dir,
            as_of=args.as_of, publish_passing=args.publish_passing,
        )
    except Exception as exc:  # noqa: BLE001 - class only, never private data
        result = {
            "schema": "rival-xmarket-source-to-model-v1",
            "asset": args.asset, "status": "unavailable",
            "error_class": type(exc).__name__,
            "models_published": 0, "real_money_orders": 0,
        }
        failed = True
    else:
        failed = False
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n",
                        encoding="utf-8")
    print("XMARKET_NATIVE_TO_MODEL=" + json.dumps(result, sort_keys=True))
    # Insufficient real source days are pending, not successful models.
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
