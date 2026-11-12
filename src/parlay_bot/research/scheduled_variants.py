"""Autonomous loop Phase 3 — the deterministic scheduled variant grid (spec §6, D9).

The loop proposes new model variants from two sources (spec D9):

(a) LLM feature masks, via the existing hypothesis lab — the LLM proposes
    only a fixed feature mask, never a probability, label, weight, or
    trade; verification and ablation of those proposals are unchanged.
(b) This module: a fixed, pre-registered grid of variant designs,
    enumerated in code and never invented at runtime by the LLM. The
    driver opens one grid entry at a time and shepherds it through the
    same experiment stages (proposed -> ablating -> screened -> shadow ->
    verdict), with the same caps and the same finding on every closure.

Safety (spec §6, D10/D16): no gate/threshold/settlement/promotion
changes; shadow-only until Route B; no auto-promotion; no new family
names without a human (the driver stops at screened for new-family
designs); every closed experiment leaves a finding; the driver holds no
AI client and spends no LLM calls. The grid is the only place a
scheduled variant is defined — a design the grid does not contain
cannot be proposed, verified, or shadowed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from parlay_bot.perpetuals.research_loop import (
    OFFLINE_POOL_MAX,
    create_experiment,
    initialize,
    pool_counts,
    recent_experiments,
)
from parlay_bot.research import archive_training, gbm_training, volcal_training
from parlay_bot.research.archive_features import GROUPS
from parlay_bot.research.volcal_features import GROUPS as VOLCAL_GROUPS

# The hypothesis the deterministic grid opens under. It is deliberately
# NOT in the LLM's HYPOTHESES enum: validate_proposal() refuses it, so
# the LLM can never propose a scheduled variant (spec D9).
SCHEDULED_HYPOTHESIS = "scheduled_variant"

# Spec D10/D16: at most 2 new scheduled variants per rolling week.
SCHEDULED_WEEKLY_CAP = 2
# A design already tried in the last 90 days is not re-proposed.
SCHEDULED_RETRY_DAYS = 90
# A design that failed ablation twice, ever, is permanently skipped.
SCHEDULED_ABLATION_FAILURE_LIMIT = 2
REQUEST_SCHEMA = "rival-scheduled-research-request-v1"
REQUEST_PREFIX = "scheduled-request-"
RESULT_PREFIX = "scheduled-result-"

# The four training venues (spec D6). A scheduled variant trains on the
# same archive frames as archive_v1 / gbm_v1.
TRAIN_VENUES = ("binance", "bitget", "okx", "gate")
# The observation scope every variant is evaluated on: the primary
# stream (hyperliquid, asset, USDC, 5m).
SCOPE_VENUE = "hyperliquid"
SCOPE_QUOTE = "USDC"
SCOPE_HORIZON_MINUTES = 5
CALIBRATION_METHOD = "platt-scaling-ece<=0.10"

# The boosted-research LightGBM factory set (spec §4.2(a),
# boosted_models.py:31-44): the only GBM parameter keys a scheduled
# variant may override. The pooled PARAMS set (pooled_training.py) is
# the other reference; the grid records the factory set exactly.
ALLOWED_GBM_PARAMS = frozenset({
    "n_estimators", "num_leaves", "max_depth", "learning_rate",
    "min_child_samples", "reg_lambda", "subsample", "colsample_bytree",
    "n_jobs", "random_state", "verbosity",
})


def _scope(asset: str) -> dict:
    return {
        "venue": SCOPE_VENUE,
        "contract": asset,
        "quote": SCOPE_QUOTE,
        "horizon_minutes": SCOPE_HORIZON_MINUTES,
    }


# The registered grid, in deterministic opening order. Each entry is a
# complete variant design: model shape, what differs from the base
# family, where it trains, how it is calibrated, and how it shadows.
# A design's proposal_sha256 IS its design hash (see design_sha256),
# so the existing trial-report / candidate handoff, keyed by proposal
# SHA, works unchanged.
SCHEDULED_VARIANTS: tuple[dict, ...] = (
    {
        # Feature-group ablation: the archive logistic without the
        # premium group (features 23-26). Shadows as its family
        # (archive_v1), a queued fit under D14 pinning.
        "variant_id": "archive-premium-off",
        "variant_kind": "feature_group_off",
        "asset": "BTC",
        "venues": list(TRAIN_VENUES),
        "model_shape": "archive-offline-v1",
        "feature_groups_off": ["premium"],
        "train_window": "common-2021",
        "gbm_params": None,
        "calibration_method": CALIBRATION_METHOD,
        "shadow_mode": "artifact",
        "scope": _scope("BTC"),
    },
    {
        # GBM hyperparameter variant: the boosted-factory LightGBM set
        # with 150 trees. Shadows as a NEW family, so the driver stops
        # at screened — a new family name is a human decision (D10).
        "variant_id": "gbm-trees-150",
        "variant_kind": "gbm_hyperparams",
        "asset": "ETH",
        "venues": list(TRAIN_VENUES),
        "model_shape": "gbm-offline-v1",
        "feature_groups_off": [],
        "train_window": "common-2021",
        "gbm_params": {
            "n_estimators": 150, "num_leaves": 12, "max_depth": 5,
            "learning_rate": 0.04, "min_child_samples": 100,
            "reg_lambda": 10.0, "subsample": 1.0, "colsample_bytree": 1.0,
            "n_jobs": 1, "random_state": 420, "verbosity": -1,
        },
        "calibration_method": CALIBRATION_METHOD,
        "shadow_mode": "new_family",
        "scope": _scope("ETH"),
    },
    {
        # Feature-group ablation: the archive logistic without the
        # Binance tape group (features 28-31). Shadows as archive_v1.
        "variant_id": "archive-tape-off",
        "variant_kind": "feature_group_off",
        "asset": "SOL",
        "venues": list(TRAIN_VENUES),
        "model_shape": "archive-offline-v1",
        "feature_groups_off": ["binance_tape"],
        "train_window": "common-2021",
        "gbm_params": None,
        "calibration_method": CALIBRATION_METHOD,
        "shadow_mode": "artifact",
        "scope": _scope("SOL"),
    },
    {
        # Window variant: the archive logistic with per-venue training
        # windows (Binance from 2019-09, per the archive spec option F).
        # Shadows as archive_v1.
        "variant_id": "archive-window-per-venue",
        "variant_kind": "window",
        "asset": "XRP",
        "venues": list(TRAIN_VENUES),
        "model_shape": "archive-offline-v1",
        "feature_groups_off": [],
        "train_window": "per-venue",
        "gbm_params": None,
        "calibration_method": CALIBRATION_METHOD,
        "shadow_mode": "artifact",
        "scope": _scope("XRP"),
    },
    {
        # Volcal full design: the 22-feature volcal_v1 logistic (the
        # incumbent 10 + vol/compression/volume/DC-leg groups), fitted
        # offline by volcal_training. Shadows as volcal_v1 — an existing
        # family, so shadow_mode is "artifact", not "new_family".
        "variant_id": "volcal-full",
        "variant_kind": "feature_group_off",
        "asset": "BTC",
        "venues": list(TRAIN_VENUES),
        "model_shape": "volcal-offline-v1",
        "feature_groups_off": [],
        "train_window": "common-2021",
        "gbm_params": None,
        "calibration_method": CALIBRATION_METHOD,
        "shadow_mode": "artifact",
        "scope": _scope("BTC"),
    },
    {
        # Volcal ablation: the full design without the adaptive-vol
        # group (features 10-13). Shadows as volcal_v1.
        "variant_id": "volcal-no-vol",
        "variant_kind": "feature_group_off",
        "asset": "ETH",
        "venues": list(TRAIN_VENUES),
        "model_shape": "volcal-offline-v1",
        "feature_groups_off": ["vol"],
        "train_window": "common-2021",
        "gbm_params": None,
        "calibration_method": CALIBRATION_METHOD,
        "shadow_mode": "artifact",
        "scope": _scope("ETH"),
    },
    {
        # Volcal ablation: the full design without the compression
        # group (features 14-15). Shadows as volcal_v1.
        "variant_id": "volcal-no-compression",
        "variant_kind": "feature_group_off",
        "asset": "SOL",
        "venues": list(TRAIN_VENUES),
        "model_shape": "volcal-offline-v1",
        "feature_groups_off": ["compression"],
        "train_window": "common-2021",
        "gbm_params": None,
        "calibration_method": CALIBRATION_METHOD,
        "shadow_mode": "artifact",
        "scope": _scope("SOL"),
    },
    {
        # Volcal ablation: the full design without the volume-surprise
        # group (features 16-18). Shadows as volcal_v1.
        "variant_id": "volcal-no-volume",
        "variant_kind": "feature_group_off",
        "asset": "XRP",
        "venues": list(TRAIN_VENUES),
        "model_shape": "volcal-offline-v1",
        "feature_groups_off": ["volume"],
        "train_window": "common-2021",
        "gbm_params": None,
        "calibration_method": CALIBRATION_METHOD,
        "shadow_mode": "artifact",
        "scope": _scope("XRP"),
    },
    {
        # Volcal ablation: the full design without the DC-leg group
        # (features 19-21). Same asset as the full design, so the
        # DC-leg contribution reads off a same-asset pair. Shadows as
        # volcal_v1.
        "variant_id": "volcal-no-dc",
        "variant_kind": "feature_group_off",
        "asset": "BTC",
        "venues": list(TRAIN_VENUES),
        "model_shape": "volcal-offline-v1",
        "feature_groups_off": ["dc"],
        "train_window": "common-2021",
        "gbm_params": None,
        "calibration_method": CALIBRATION_METHOD,
        "shadow_mode": "artifact",
        "scope": _scope("BTC"),
    },
)


def design_sha256(design: dict) -> str:
    """Canonical hash of a scheduled design: the experiment's proposal SHA.

    The same canonical-JSON digest the lab uses for LLM proposals, so a
    scheduled experiment's trial reports and candidate artifacts join on
    it exactly as an LLM experiment's do.
    """
    return hashlib.sha256(
        json.dumps(design, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def lookup_variant(sha: str) -> dict | None:
    """The registered grid entry with this design hash, or None."""
    for design in SCHEDULED_VARIANTS:
        if design_sha256(design) == sha:
            return design
    return None


def _validate_grid() -> None:
    """Fail fast at import when the registered grid is malformed.

    A grid entry that cannot train, cannot shadow, or cannot be told
    apart from another entry must break the build, never the loop.
    """
    seen_ids: set[str] = set()
    seen_hashes: set[str] = set()
    for design in SCHEDULED_VARIANTS:
        variant_id = design.get("variant_id")
        if not isinstance(variant_id, str) or not variant_id:
            raise ValueError("scheduled variant without a variant_id")
        if variant_id in seen_ids:
            raise ValueError(f"duplicate scheduled variant_id {variant_id!r}")
        seen_ids.add(variant_id)
        sha = design_sha256(design)
        if sha in seen_hashes:
            raise ValueError(f"duplicate scheduled design hash for {variant_id!r}")
        seen_hashes.add(sha)
        if design.get("variant_kind") not in (
                "feature_group_off", "gbm_hyperparams", "window"):
            raise ValueError(f"unknown variant_kind for {variant_id!r}")
        if design.get("model_shape") not in (
                "archive-offline-v1", "gbm-offline-v1", "volcal-offline-v1"):
            raise ValueError(f"unknown model_shape for {variant_id!r}")
        if design.get("shadow_mode") not in ("artifact", "new_family"):
            raise ValueError(f"unknown shadow_mode for {variant_id!r}")
        if design.get("asset") not in ("BTC", "ETH", "SOL", "XRP"):
            raise ValueError(f"unknown asset for {variant_id!r}")
        if list(design.get("venues") or []) != list(TRAIN_VENUES):
            raise ValueError(f"scheduled variant {variant_id!r} must train "
                             f"on {list(TRAIN_VENUES)!r}")
        if design.get("model_shape") == "volcal-offline-v1":
            # The per-venue window is archive-specific; a volcal entry
            # naming it would be fitted as archive in _train_variant.
            if design.get("train_window") != "common-2021":
                raise ValueError(
                    f"scheduled variant {variant_id!r} is volcal: only the "
                    "common-2021 window is supported")
            for group in design.get("feature_groups_off") or []:
                if group not in VOLCAL_GROUPS or group == "candle":
                    raise ValueError(
                        f"scheduled variant {variant_id!r} turns off unknown "
                        f"or non-ablatable volcal feature group {group!r}")
        else:
            for group in design.get("feature_groups_off") or []:
                if group not in GROUPS:
                    raise ValueError(
                        f"scheduled variant {variant_id!r} turns off unknown "
                        f"feature group {group!r}")
        if design.get("train_window") not in ("common-2021", "per-venue"):
            raise ValueError(f"unknown train_window for {variant_id!r}")
        params = design.get("gbm_params")
        if design.get("variant_kind") == "gbm_hyperparams":
            if not isinstance(params, dict) or not params:
                raise ValueError(
                    f"scheduled variant {variant_id!r} needs gbm_params")
            unknown = set(params) - ALLOWED_GBM_PARAMS
            if unknown:
                raise ValueError(
                    f"scheduled variant {variant_id!r} has unknown GBM "
                    f"params {sorted(unknown)!r}")
        elif params is not None:
            raise ValueError(
                f"scheduled variant {variant_id!r} carries gbm_params "
                "without being a gbm_hyperparams variant")
        scope = design.get("scope") or {}
        if (scope.get("venue") != SCOPE_VENUE
                or scope.get("contract") != design.get("asset")
                or scope.get("quote") != SCOPE_QUOTE
                or scope.get("horizon_minutes") != SCOPE_HORIZON_MINUTES):
            raise ValueError(f"scheduled variant {variant_id!r} has a scope "
                             "outside the primary stream")


_validate_grid()


def screen_scheduled_artifact(payload: bytes | str) -> dict:
    """Retrospective screen for a scheduled variant's artifact doc (D9/D13).

    The drop-in ``screen=`` checker for the shared ``run_screen``: the
    artifact document must validate AND be usable under its own
    training module (archive logistic or GBM). Anything else returns
    ``insufficient`` and the loop discards the experiment with a
    finding — never an exception, never a pass on a malformed doc.
    """
    try:
        doc = json.loads(payload)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"retrospective_screen": "insufficient",
                "reason": "artifact payload is not JSON"}
    if not isinstance(doc, dict):
        return {"retrospective_screen": "insufficient",
                "reason": "artifact payload is not a document"}
    schema = doc.get("schema")
    if schema == archive_training.SCHEMA:
        module = archive_training
    elif schema == gbm_training.SCHEMA:
        module = gbm_training
    elif schema == volcal_training.SCHEMA:
        module = volcal_training
    else:
        return {"retrospective_screen": "insufficient",
                "reason": f"unknown artifact schema {schema!r}"}
    try:
        module.validate(doc)
    except (ValueError, TypeError) as exc:
        return {"retrospective_screen": "insufficient", "reason": str(exc)}
    if not module.usable(doc):
        return {"retrospective_screen": "insufficient",
                "reason": "artifact is not usable (stale or backtest-only)"}
    return {"retrospective_screen": "passed"}


def scheduled_shadow_identity(design: dict, payload: bytes | str) -> dict:
    """Family + scope + weights SHA for a screened scheduled artifact.

    The variant shadows as its own family (archive_v1 / gbm_v1), so the
    identity is the family's own artifact identity — recomputed from
    the stored weights/trees, never trusted from a stored column — plus
    the design's scope. The driver records this when the experiment
    enters shadow and reads it back to match the pinned fit (D14).
    """
    doc = json.loads(payload)
    shape = design.get("model_shape")
    if shape == "archive-offline-v1":
        identity = archive_training.artifact_identity(doc)
        family = archive_training.FAMILY
    elif shape == "gbm-offline-v1":
        identity = gbm_training.artifact_identity(doc)
        family = gbm_training.FAMILY
    elif shape == "volcal-offline-v1":
        identity = volcal_training.artifact_identity(doc)
        family = volcal_training.FAMILY
    else:
        raise ValueError(f"unknown model shape {shape!r}")
    scope = design.get("scope") or {}
    scope_identity = {
        "family": family,
        "asset": design.get("asset"),
        "venue": scope.get("venue"),
        "contract": scope.get("contract"),
        "quote": scope.get("quote"),
        "horizon_minutes": int(scope.get("horizon_minutes",
                                         SCOPE_HORIZON_MINUTES)),
        "weights_sha256": identity["weights_sha256"],
    }
    if "scheduled_trial" in doc:
        from parlay_bot.perpetuals.pooled_model import model_sha256

        scope_identity["model_sha256"] = model_sha256(doc)
    return scope_identity


def _parse_time(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return parsed if parsed.utcoffset() else parsed.replace(tzinfo=UTC)


def _design_tried_recently(recent: list[dict], sha: str) -> bool:
    """True when a scheduled experiment with this design hash is open
    or closed within the 90-day window (spec D10)."""
    return any(
        e.get("proposer") == "scheduled"
        and e.get("proposal_sha256") == sha
        for e in recent
    )


def _design_failed_ablation_twice(store, sha: str) -> bool:
    """True when this design already failed ablation twice, ever (D10).

    The full history is scanned, not the 90-day window: two ablation
    failures permanently retire a design. An ablation failure is a
    discarded/failed event whose outcome stage is "ablation".
    """
    try:
        with store.connect() as conn:
            rows = conn.execute(
                """SELECT outcome_json FROM rival_research_experiments
                   WHERE proposer = 'scheduled' AND proposal_sha256 = ?""",
                (sha,),
            ).fetchall()
    except Exception:  # noqa: BLE001 - no ledger yet means no failures
        return False
    failures = 0
    for (outcome_json,) in rows:
        try:
            events = json.loads(outcome_json or "[]")
        except (ValueError, TypeError):
            continue
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get("stage") in ("discarded", "failed"):
                outcome = event.get("outcome") or {}
                if outcome.get("stage") == "ablation" and not outcome.get("blocked"):
                    failures += 1
                    if failures >= SCHEDULED_ABLATION_FAILURE_LIMIT:
                        return True
    return False


def _scheduled_opened_this_week(recent: list[dict], *, now: datetime) -> int:
    """Scheduled variants opened in the last 7 days (spec D10/D16)."""
    count = 0
    for experiment in recent:
        if experiment.get("proposer") != "scheduled":
            continue
        created = _parse_time(experiment.get("created_at"))
        if created is not None and now - created <= timedelta(days=7):
            count += 1
    return count


def open_scheduled_variant(store, *, now: datetime | None = None) -> dict | None:
    """Open the next registered scheduled variant, or None (spec D9/D10/D16).

    Grid order, first untried entry wins. Returns None when the offline
    pool is full, the weekly budget (≤2 new scheduled variants/week)
    is spent, every entry was already tried in the 90-day window, or
    every untried entry failed ablation twice. An exhausted grid is a
    normal state, not an error — the driver simply opens nothing.
    """
    stamp = now or datetime.now(UTC)
    initialize(store)
    counts = pool_counts(store, now=stamp)
    if counts["offline"] >= OFFLINE_POOL_MAX:
        return None
    recent = recent_experiments(store, now=stamp, days=SCHEDULED_RETRY_DAYS)
    if _scheduled_opened_this_week(recent, now=stamp) >= SCHEDULED_WEEKLY_CAP:
        return None
    for design in SCHEDULED_VARIANTS:
        sha = design_sha256(design)
        if _design_tried_recently(recent, sha):
            continue
        if _design_failed_ablation_twice(store, sha):
            continue
        return create_experiment(
            store, proposer="scheduled", hypothesis=SCHEDULED_HYPOTHESIS,
            design=copy.deepcopy(design), proposal_sha256=sha, now=stamp,
        )
    return None


def scheduled_request(experiment: dict) -> dict:
    return {
        "schema_version": REQUEST_SCHEMA,
        "experiment_id": experiment["experiment_id"],
        "design_sha256": experiment["proposal_sha256"],
        "created_at": experiment["created_at"],
    }


def _read_document(payload: bytes) -> dict:
    try:
        doc = json.loads(payload)
    except (ValueError, TypeError):
        return {}
    return doc if isinstance(doc, dict) else {}


async def publish_scheduled_request(drive, experiment: dict) -> None:
    from parlay_bot.research.publish import _artifact

    request = scheduled_request(experiment)
    design = lookup_variant(request["design_sha256"])
    experiment_id = request["experiment_id"]
    if design is None or not re.fullmatch(r"[0-9a-f]{32}", experiment_id):
        raise ValueError("unregistered scheduled research request")
    with TemporaryDirectory() as folder:
        path = Path(folder) / f"{REQUEST_PREFIX}{design['asset']}-{experiment_id}.json"
        archive_training._write_json(path, request)
        await drive.ensure_research_output(
            artifact=_artifact(path, 1), batch_id=f"scheduled-request-{experiment_id}",
            kind="report",
        )


async def read_scheduled_result(drive, request: dict) -> tuple[dict, bytes, bool] | None:
    """Find the immutable outcome for this attempt, including a passing artifact."""
    name = f"{RESULT_PREFIX}{request['experiment_id']}.json"
    design = lookup_variant(request["design_sha256"])
    if design is None:
        return None
    for eligible, reader in (
        (True, drive.read_verified_research_candidates),
        (False, drive.read_verified_research_reports),
    ):
        for _, payload in await reader(name_prefix=name, limit=2):
            doc = _read_document(payload)
            trial = doc.get("scheduled_trial")
            if not isinstance(trial, dict) or trial.get("request") != request:
                continue
            if doc.get("scheduled_design_sha256") != request["design_sha256"]:
                continue
            if doc.get("asset") != design["asset"] or doc.get("horizon_minutes") != 5:
                continue
            status = trial.get("status")
            expects_model = status == "passed" and design["shadow_mode"] == "artifact"
            if status in ("passed", "rejected", "blocked") and eligible == expects_model:
                return doc, payload, eligible
    return None


async def next_scheduled_request(drive, asset: str) -> dict | None:
    """Read the oldest outstanding request from the same private Drive boundary."""
    pending = []
    cursor = None
    for _ in range(3):
        items, cursor = await drive.read_verified_research_reports(
            name_prefix=f"{REQUEST_PREFIX}{asset}-", limit=20,
            page_token=cursor, return_next_page=True,
        )
        for _, payload in items:
            doc = _read_document(payload)
            design = lookup_variant(doc.get("design_sha256"))
            if (doc.get("schema_version") == REQUEST_SCHEMA and design is not None
                    and design["asset"] == asset
                    and re.fullmatch(r"[0-9a-f]{32}", str(doc.get("experiment_id", "")))
                    and _parse_time(doc.get("created_at")) is not None):
                pending.append(doc)
        if cursor is None:
            break
    for request in sorted(pending, key=lambda doc: doc["created_at"]):
        if await read_scheduled_result(drive, request) is None:
            return request
    return None
