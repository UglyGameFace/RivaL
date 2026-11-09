"""Weekly archive workflow entry point for the shared verified training input."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from parlay_bot.archive.drive import MAX_RESEARCH_DOCUMENT_BYTES
from parlay_bot.config import Settings
from parlay_bot.research import archive_training, gbm_training
from parlay_bot.research.publish import _artifact, _drive
from parlay_bot.research.scheduled_variants import (
    RESULT_PREFIX,
    lookup_variant,
    next_scheduled_request,
)


def _window(frames: dict, *, through, first: str | None) -> dict:
    from pandas import Timestamp, to_datetime

    bounded = {}
    end = to_datetime(through, utc=True)
    for key, (candles, features) in frames.items():
        first_date = first if first is not None else (
            "2019-09-01" if key.startswith("binance|") else "2021-01-01"
        )
        start = Timestamp(first_date, tz="UTC")
        bounded[key] = (candles.loc[start:end], features.loc[start:end])
    return bounded


def _fit(module, frames: dict, training: dict, **variant) -> tuple[dict, dict | None, str]:
    try:
        if not training["provenance"].get("provider_sha256_by_month"):
            raise archive_training.TrainingDataUnavailable("missing verified provider hashes")
        result = module.train_run(frames, **training, **variant)
    except archive_training.TrainingDataUnavailable as exc:
        # A floor failure still carries its census (regime_v2 attaches
        # it to the exception): the blocked report measures the
        # shortfall instead of merely naming it.
        report = {"gates_passed": False, "gate_failures": [str(exc)]}
        census = getattr(exc, "census", None)
        if census is not None:
            report["trending_census"] = census
        return report, None, "blocked"
    report = result["report"]
    if result["artifact"] is None:
        return report, None, "rejected"
    model = module.validate(result["artifact"])
    if len(_encode(model)) > MAX_RESEARCH_DOCUMENT_BYTES:
        return {**report, "gates_passed": False,
                "gate_failures": ["portable artifact exceeds the Drive import limit"]}, None, "rejected"
    return report, model, "passed"


def _encode(document: dict) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


async def _save(drive, path: Path, document: dict, *, kind: str) -> None:
    payload = _encode(document)
    if len(payload) > MAX_RESEARCH_DOCUMENT_BYTES:
        raise ValueError("research document exceeds the Drive import limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    if drive is not None:
        artifact = _artifact(path, 1)
        await drive.ensure_research_output(
            artifact=artifact, batch_id=f"archive-train-{artifact.sha256[:24]}", kind=kind,
        )


async def _train_variant(drive, request: dict, *, common: dict, full: dict,
                         training: dict) -> None:
    design = lookup_variant(request["design_sha256"])
    assert design is not None
    if design["train_window"] == "per-venue":
        extended = any(
            len(candles) > len(common[key][0])
            for key, (candles, _) in full.items() if key.startswith("binance|")
        )
        if extended:
            report, model, status = _fit(archive_training, full, training)
        else:
            report = {"gates_passed": False,
                      "gate_failures": ["no verified pre-2021 Binance bars for the window variant"]}
            model, status = None, "blocked"
    elif design["gbm_params"] is not None:
        report, model, status = _fit(gbm_training, common, training, params=design["gbm_params"])
    else:
        report, model, status = _fit(
            archive_training, common, training, groups_off=tuple(design["feature_groups_off"]),
        )
    doc = {
        **(model if model is not None else report),
        "asset": training["asset"],
        "horizon_minutes": training["horizon"],
        "scheduled_design_sha256": request["design_sha256"],
        "scheduled_trial": {"request": request, "status": status},
    }
    kind = "model" if status == "passed" and design["shadow_mode"] == "artifact" else "report"
    # Keep the Postgres attempt identity in private Drive, outside the Actions upload directory.
    with TemporaryDirectory() as folder:
        path = Path(folder) / f"{RESULT_PREFIX}{request['experiment_id']}.json"
        await _save(drive, path, doc, kind=kind)
    print(f"{design['variant_id']} {training['asset']}: {status}")


async def train_weekly_archive(frames: dict, *, training: dict, census: dict,
                               out: Path, publish: bool) -> int:
    """Train the two ordinary families and at most one requested variant per scope.

    regime_v2 is retired (spec rival-regime-live-learning-spec-20261108,
    D10): it no longer trains here; the live-learning families replace
    it and train themselves in the bot.
    """
    asset, horizon = training["asset"], training["horizon"]
    through = training["trained_through"]
    full = _window(frames, through=through, first=None)
    common = _window(full, through=through, first="2021-01-01")
    drive = _drive(Settings()) if publish else None
    try:
        if drive is not None and horizon == 5:
            request = await next_scheduled_request(drive, asset)
            if request is not None:
                await _train_variant(drive, request, common=common, full=full,
                                     training=training)
        for prefix, module in (("archive-v1", archive_training),
                               ("gbm-v1", gbm_training)):
            report, model, status = _fit(module, common, training)
            report = {**report, "census": census, "training_status": status,
                      "asset": asset, "horizon_minutes": horizon, "family": module.FAMILY}
            await _save(drive if model is None else None,
                        out / f"{prefix}-{asset}-{horizon}m-report.json", report, kind="report")
            if model is not None:
                await _save(drive, out / f"{prefix}-{asset}-{horizon}m-model.json", model,
                            kind="model")
            print(f"{prefix} {asset} {horizon}m: {status}")
    finally:
        if drive is not None:
            await drive.aclose()
    return 0
