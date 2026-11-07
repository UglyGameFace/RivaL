"""Autonomous-loop Phase 1 driver: run the research loop without a human.

The research loop (`research_loop.py`) already exists as a complete, tested
state machine, and the hypothesis lab already proposes, and the offline GHA
job already ablates and screens — but nothing ever advanced an experiment
from one stage to the next. This driver is that missing piece: an in-bot
task (`_research_loop_driver` in `bot.py`) that ticks every 30 minutes and
advances at most one experiment one stage per tick.

What the driver does, per tick:
    proposed  -> verify the stored receipt (in-bot, no LLM call)
    ablating  -> consume the GHA trial report from Drive, joined on
                 proposal_sha256 (a report that names no open experiment
                 is ignored; a missing report means "wait", never a guess)
    screened  -> re-run the retrospective screen on the published artifact
                 bytes (models folder = eligible, reports folder = rejected)
    shadow    -> observe the stored FamilyPromotion evaluation, read-only;
                 promote only when selected() already returned the family,
                 discard when the 30-day lookback expires

What the driver never does: spend an LLM call (it holds no AI client; the
only LLM call in the loop is the lab's existing proposal call), change a
gate/threshold/settlement/promotion rule, write any table other than the
experiment/finding tables, or let an LLM output be a transition actor
(`research_loop.transition` refuses actor="llm" and the driver only ever
acts as "system"). Every closed experiment leaves a finding — the loop
enforces that, not the driver.

Cadence and safety (spec D2): 30-minute tick, one experiment one stage,
skip the tick on memory pressure. A tick that finds nothing to advance
does nothing. The master switch is the existing
`rival_llm_background_opt_in`: off stops the task at creation, at the
loop head, and in the watchdog; experiments freeze mid-state and resume
when re-enabled.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.markets import asset_for_contract
from parlay_bot.perpetuals.research_hypotheses import PROPOSAL_FRESHNESS_DAYS
from parlay_bot.perpetuals.research_loop import (
    SHADOW_POOL_MAX,
    TERMINAL_STATUSES,
    ResearchLoop,
    pool_counts,
    record_stage_outcome,
)
from parlay_bot.research.scheduled_variants import (
    design_sha256,
    lookup_variant,
    open_scheduled_variant,
    scheduled_shadow_identity,
    screen_scheduled_artifact,
)

_LOG = logging.getLogger(__name__)

# Spec D2: 30-minute tick, one experiment advanced one stage per tick.
DRIVER_TICK_SECONDS = 1800
# The forward-shadow lookback mirrors family_governance.LOOKBACK (30 days):
# an experiment in shadow with no promotion after 30 days is discarded.
SHADOW_LOOKBACK = timedelta(days=30)
# Bounded Drive scans per tick. The trial report for a proposal comes from
# the most recent daily research run, so the newest pages are the ones
# that matter; older pages are re-checked on later ticks.
TRIAL_REPORT_SCAN_PAGES = 3
CANDIDATE_SCAN_PAGES = 2
TRIAL_REPORT_SCHEMA = "rival-native-source-research-v1"
_SHA256_HEX = set("0123456789abcdef")


def _stamp(now: datetime | None = None) -> datetime:
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    if stamp.utcoffset() is None:
        raise ValueError("UTC timestamp required")
    return stamp


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else parsed.replace(tzinfo=UTC)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(ch in _SHA256_HEX for ch in value)
    )


def list_open_experiments(store, *, now: datetime | None = None) -> list[dict]:
    """Non-terminal experiments with a lab proposal, oldest first.

    Only experiments carrying a ``proposal_sha256`` are drivable: those
    are the ones the hypothesis lab opened, plus scheduled variants
    (whose proposal SHA is their design hash). Human-driven experiments
    (no proposal SHA, e.g. the archive driver's) are driven by their own
    caller and are left alone. The 90-day read window is the loop's own
    (``research_loop.EXPERIMENT_READ_DAYS``); an experiment stalled
    longer than that drops out of the driver's view by design.
    """
    loop = ResearchLoop(store)
    experiments = [
        experiment
        for experiment in loop.recent(now=now)
        if experiment["status"] not in TERMINAL_STATUSES
        and experiment.get("proposal_sha256")
    ]
    experiments.sort(key=lambda e: (e["created_at"], e["experiment_id"]))
    return experiments


def _verify_receipt(store, experiment: dict, *, now: datetime) -> dict | None:
    """Build this experiment's verify receipt from its stored proposal row.

    Reads the exact hypotheses-table row by record SHA (not the
    latest-per-asset receipt: a newer proposal for the same asset must
    not make this experiment's genuine proposal unverifiable). The row
    is rebuilt into a proposal dict and its digest is recomputed the
    same way ``recent_hypotheses`` does it — a digest mismatch means a
    corrupt row, not a verified proposal. Returns None when the row is
    absent, not proposed, corrupt, or outside the freshness window the
    verify gate enforces; the verify step then fails the experiment
    with a finding, which is the honest outcome for a stale proposal.
    An expired proposal is instead closed as discarded by
    ``_advance_verify`` (spec D15) — this helper only reports the row.
    """
    from parlay_bot.perpetuals.research_hypotheses import (
        MODEL,
        valid_feature_indices,
    )

    proposal_sha = experiment.get("proposal_sha256")
    if not _is_sha256(proposal_sha):
        return None
    try:
        with store.connect() as conn:
            row = conn.execute(
                """SELECT asset, slot_utc, evidence_sha256, model, hypothesis,
                          feature_indices_json, source_venues_json,
                          record_sha256, status
                   FROM rival_llm_feature_hypotheses
                   WHERE record_sha256=?""",
                (proposal_sha,),
            ).fetchone()
    except Exception:  # noqa: BLE001 - no hypotheses table yet is not an error
        return None
    if row is None:
        return None
    try:
        record = dict(row)
    except (TypeError, ValueError):
        return None
    if record.get("status") != "proposed" or record.get("model") != MODEL:
        return None
    design = experiment.get("design_json") or {}
    venues = design.get("venues") or []
    if not venues:
        return None
    try:
        row_venues = json.loads(record.get("source_venues_json") or "[]")
    except (ValueError, TypeError):
        return None
    if not isinstance(row_venues, list) or venues[0] not in row_venues:
        return None
    proposal = {
        "asset": record.get("asset"),
        "slot_utc": record.get("slot_utc"),
        "model": MODEL,
        "evidence_sha256": record.get("evidence_sha256"),
        "hypothesis": record.get("hypothesis"),
        "venues": row_venues,
    }
    if proposal["hypothesis"] == "feature_subset_v2":
        try:
            indices = json.loads(record.get("feature_indices_json") or "null")
        except (ValueError, TypeError):
            return None
        if not valid_feature_indices(indices):
            return None
        proposal["feature_indices"] = indices
    elif record.get("feature_indices_json") is not None:
        return None
    digest = hashlib.sha256(
        json.dumps(proposal, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if digest != record.get("record_sha256"):
        return None
    return {
        "status": "verified",
        "source": "live_postgresql_llm_hypotheses",
        "proposals": [{**proposal, "record_sha256": digest}],
    }


def _proposal_expired(store, experiment: dict, *, now: datetime) -> bool:
    """True when this experiment's proposal aged past the freshness window.

    Spec D15: a proposal that expires untested is a ledger row, not a
    silent absence. Reads the hypotheses-table row by record SHA and
    compares its slot_utc against PROPOSAL_FRESHNESS_DAYS. Any read or
    parse failure returns False — the normal verify path then fails the
    experiment closed, which is the safe outcome for a row that cannot
    be read at all.
    """
    proposal_sha = experiment.get("proposal_sha256")
    if not _is_sha256(proposal_sha):
        return False
    try:
        with store.connect() as conn:
            row = conn.execute(
                """SELECT slot_utc FROM rival_llm_feature_hypotheses
                   WHERE record_sha256=?""",
                (proposal_sha,),
            ).fetchone()
    except Exception:  # noqa: BLE001 - unreadable row is not an expiry
        return False
    if row is None:
        return False
    slot = _parse_time(row["slot_utc"])
    if slot is None:
        return False
    return (now - slot) > timedelta(days=PROPOSAL_FRESHNESS_DAYS)


def _advance_verify_scheduled(loop: ResearchLoop, experiment: dict, *,
                              now: datetime) -> dict:
    """Verify a scheduled variant against the registered grid (spec D9).

    A scheduled design is a deterministic proposal: the driver checks
    the experiment's design hash is a registered grid entry and the
    stored design hashes to it, then moves the experiment to ablating.
    An unregistered design — or a stored design that no longer hashes
    to its proposal SHA — fails closed to failed with a finding. No LLM
    receipt is involved: the grid, not a model, is the proposal.
    """
    experiment_id = experiment["experiment_id"]
    proposal_sha = experiment.get("proposal_sha256") or ""
    design = experiment.get("design_json") or {}
    entry = lookup_variant(proposal_sha)
    if entry is None or design_sha256(design) != proposal_sha:
        return loop.transition(
            experiment_id, "failed", actor="system",
            outcome={"stage": "verification",
                     "reason": "scheduled design is not a registered "
                               "grid entry",
                     "proposal_sha256": proposal_sha},
            finding={"kind": "negative_result",
                     "summary": ("Scheduled variant failed verification: "
                                 "its design is not a registered grid entry."),
                     "evidence_ref": f"rival_research_experiments:{experiment_id}",
                     "recorded_by": "system"},
            now=now)
    return loop.transition(
        experiment_id, "ablating", actor="system",
        outcome={"stage": "verification",
                 "scheduled_variant": entry["variant_id"],
                 "design_sha256": proposal_sha},
        now=now)


def _advance_verify(loop: ResearchLoop, experiment: dict, *,
                    now: datetime) -> dict:
    """Stage proposed -> ablating (or discarded/failed, with a finding).

    An expired proposal is closed as discarded with a finding naming
    the expiry (spec D15); any other unverifiable receipt fails closed
    through run_verify, as before.
    """
    if experiment.get("proposer") == "scheduled":
        return _advance_verify_scheduled(loop, experiment, now=now)
    if _proposal_expired(loop.store, experiment, now=now):
        return loop.transition(
            experiment["experiment_id"], "discarded", actor="system",
            outcome={"stage": "verify", "expired": True,
                     "freshness_days": PROPOSAL_FRESHNESS_DAYS,
                     "proposal_sha256": experiment.get("proposal_sha256")},
            finding={"kind": "negative_result",
                     "summary": (
                         "Proposal expired untested after "
                         f"{PROPOSAL_FRESHNESS_DAYS} days; the loop never "
                         "ablated it."),
                     "evidence_ref": ("rival_research_experiments:"
                                      f"{experiment['experiment_id']}"),
                     "recorded_by": "system"},
            now=now)
    receipt = _verify_receipt(loop.store, experiment, now=now)
    design = experiment.get("design_json") or {}
    venues = design.get("venues") or []
    if receipt is None or not venues:
        # No verifiable receipt for this proposal: fail it closed rather
        # than advancing on another proposal's receipt. run_verify with
        # an empty verified receipt does exactly that.
        receipt = {
            "status": "verified",
            "source": "live_postgresql_llm_hypotheses",
            "proposals": [],
        }
        venue = venues[0] if venues else "hyperliquid"
    else:
        venue = venues[0]
    return loop.verify(experiment["experiment_id"], receipt, venue=venue, now=now)


def _advance_blocked(loop: ResearchLoop, experiment: dict, *,
                     now: datetime) -> dict | None:
    """Close an experiment whose every venue reported incomplete months.

    Spec D17: a blocked run writes a receipt (venue, asset, requested
    months, missing months, report_sha256); the driver consumes it by
    closing the affected experiment with a finding naming the missing
    months. "Affected" means every venue in the experiment's design has
    a blocked receipt recorded since the experiment was created — a
    single blocked venue never closes the experiment, because another
    venue's run may still ablate it. Returns None when the experiment
    is not fully blocked: it waits, it is not failed on absence.
    """
    from parlay_bot.perpetuals.research_hypotheses import (
        list_blocked_receipts,
    )

    design = experiment.get("design_json") or {}
    venues = [v for v in (design.get("venues") or []) if isinstance(v, str)]
    if not venues:
        return None
    created = _parse_time(experiment.get("created_at"))
    covering: dict[str, dict] = {}
    for receipt in list_blocked_receipts(loop.store):
        venue = receipt.get("venue")
        if venue not in venues or venue in covering:
            continue
        recorded = _parse_time(receipt.get("recorded_at"))
        if created is not None and (recorded is None or recorded < created):
            continue
        covering[venue] = receipt
    if len(covering) != len(venues):
        return None
    missing = sorted({m for receipt in covering.values()
                      for m in receipt.get("missing_months") or []})
    shown = ", ".join(missing[:4])
    if len(missing) > 4:
        shown += ", …"
    return loop.transition(
        experiment["experiment_id"], "discarded", actor="system",
        outcome={"stage": "ablation", "blocked": True,
                 "missing_months": missing, "venues": venues},
        finding={"kind": "negative_result",
                 "summary": ("Research blocked: "
                             f"{shown or 'months'} missing; proposal never "
                             "ablated."),
                 "evidence_ref": ("rival_research_experiments:"
                                  f"{experiment['experiment_id']}"),
                 "recorded_by": "system"},
        now=now)


def _advance_ablation(loop: ResearchLoop, experiment: dict, *,
                      trial_reports: dict, now: datetime) -> dict | None:
    """Stage ablating -> screened (or discarded, with a finding).

    Returns None when no trial report for this proposal has been
    published yet: the experiment waits, it is not failed on absence —
    unless every venue in its design reported incomplete months, in
    which case it is closed as blocked (see ``_advance_blocked``).
    A report naming a different proposal SHA is ignored by the caller
    (trial_reports is keyed by proposal SHA). A re-delivered report
    (spec D17) is skipped before the ablation runs: its consumed key
    is already recorded, so re-applying would only duplicate the event.
    """
    proposal_sha = experiment.get("proposal_sha256") or ""
    entry = trial_reports.get(proposal_sha)
    if entry is None:
        return _advance_blocked(loop, experiment, now=now)
    report_sha = entry["report_sha256"]
    if is_report_consumed(loop.store, proposal_sha, report_sha):
        return None
    result = loop.ablate(
        experiment["experiment_id"],
        trials=entry["trials"],
        report_sha256=report_sha,
        idempotency_key=f"ablation:{proposal_sha}:{report_sha}",
        now=now,
    )
    record_consumed_report(loop.store, proposal_sha, report_sha, now=now)
    return result


def shadow_family_for_candidate(payload: bytes) -> tuple[str, dict]:
    """Derive the shadow family + scope for a screened candidate artifact.

    The family name is the same formula the issuance path uses
    (``model_competition.issue``): logistic candidates issue as
    ``historical_<model_sha256[:48]>``; portable boosted candidates as
    ``boosted_<family>_<model_sha256[:12]>``. The scope is the
    artifact's own identity: one venue/contract/quote/horizon.
    """
    source = json.loads(payload)
    spec = source["model_spec"]
    identity = spec["identity"]
    model_sha256 = source["model_sha256"]
    if spec.get("model") == "portable-boosted-binary-v1":
        family = f"boosted_{spec['family']}_{model_sha256[:12]}"
    else:
        family = f"historical_{model_sha256[:48]}"
    asset = asset_for_contract(str(identity["contract"]))
    return family, {
        "asset": asset,
        "venue": str(identity["venue"]),
        "contract": str(identity["contract"]),
        "quote": str(identity["quote"]),
        "horizon_minutes": int(spec["horizon_minutes"]),
    }


def _shadow_pool_full(loop: ResearchLoop, experiment: dict, *,
                      now: datetime) -> bool:
    """True when the shadow pool is full (spec D16).

    A screened candidate waits at screened while the shadow pool is
    full — the wait is a stage outcome noting capacity, not a finding.
    Recorded once per distinct shadow count so the event log is not
    spammed every tick.
    """
    counts = pool_counts(loop.store, now=now)
    if counts["shadow"] < SHADOW_POOL_MAX:
        return False
    events = experiment.get("outcome_json") or []
    last = events[-1] if events else {}
    last_outcome = last.get("outcome") or {}
    if not (last.get("stage") == "shadow-capacity"
            and last_outcome.get("shadow_open") == counts["shadow"]):
        record_stage_outcome(
            loop.store, experiment["experiment_id"],
            stage="shadow-capacity",
            outcome={"waiting": "shadow_pool_full",
                     "shadow_open": counts["shadow"],
                     "shadow_max": SHADOW_POOL_MAX},
            now=now)
    return True


def _hold_new_family(loop: ResearchLoop, experiment: dict, design: dict, *,
                     now: datetime) -> None:
    """Hold a new-family scheduled variant at screened (spec D10).

    A new family name is a human decision: the driver never invents
    one. The hold is recorded once as a stage outcome plus a single
    finding (kind ``negative_result`` — the kinds enum has no
    held/needs-human value, and the summary states the hold
    explicitly). The experiment stays open at screened, so its design
    is not re-proposed, and the driver moves on to the next experiment.
    A held variant occupies one offline pool slot until a human acts.
    """
    experiment_id = experiment["experiment_id"]
    current = loop.get(experiment_id) or experiment
    for event in current.get("outcome_json") or []:
        if (event.get("outcome") or {}).get("new_family_hold"):
            return
    variant_id = design.get("variant_id")
    record_stage_outcome(
        loop.store, experiment_id, stage="screened",
        outcome={"stage": "screen", "new_family_hold": True,
                 "variant_id": variant_id,
                 "model_shape": design.get("model_shape")},
        now=now)
    loop.finding(
        experiment_id=experiment_id, kind="negative_result",
        summary=(f"Scheduled variant {variant_id} held at screened: its "
                 "shadow mode is new_family and a new family name is a "
                 "human decision."),
        evidence_ref=f"rival_research_experiments:{experiment_id}",
        recorded_by="system", now=now)


def _advance_screen_scheduled(loop: ResearchLoop, experiment: dict, *,
                              artifact: bytes, eligible: bool,
                              now: datetime) -> dict | None:
    """Screen a scheduled variant's artifact (spec D9/D10/D13).

    A new-family design never enters shadow: a passing artifact holds
    at screened (see ``_hold_new_family``); a failing artifact is
    discarded by the shared screen. An artifact-mode design shadows
    as its own family (archive_v1 / gbm_v1), so the shadow pool
    capacity gate applies exactly as it does to LLM variants, and the
    family + scope + weights SHA are recorded for the shadow step to
    match against the pinned fit (D14).
    """
    design = experiment.get("design_json") or {}
    experiment_id = experiment["experiment_id"]
    if design.get("shadow_mode") == "new_family":
        check = screen_scheduled_artifact(artifact)
        if check["retrospective_screen"] != "passed":
            return loop.screen(
                experiment_id, artifact=artifact, eligible=eligible,
                screen=screen_scheduled_artifact, now=now,
            )
        _hold_new_family(loop, experiment, design, now=now)
        return None
    if _shadow_pool_full(loop, experiment, now=now):
        return None
    result = loop.screen(
        experiment_id, artifact=artifact, eligible=eligible,
        screen=screen_scheduled_artifact, now=now,
    )
    if result["status"] == "shadow":
        identity = scheduled_shadow_identity(design, artifact)
        record_stage_outcome(
            loop.store, experiment_id, stage="shadow-family",
            outcome={"family": identity["family"],
                     "asset": identity["asset"],
                     "venue": identity["venue"],
                     "contract": identity["contract"],
                     "quote": identity["quote"],
                     "horizon_minutes": identity["horizon_minutes"],
                     "weights_sha256": identity["weights_sha256"]},
            now=now,
        )
    return result


def _advance_screen(loop: ResearchLoop, experiment: dict, *,
                    candidates: dict, now: datetime) -> dict | None:
    """Stage screened -> shadow (or discarded, with a finding).

    Returns None when the published artifact for this proposal has not
    been found yet. On a pass to shadow, the family + scope are
    recorded as a stage outcome so the shadow step knows what to
    observe without re-reading Drive.
    """
    found = candidates.get(experiment.get("proposal_sha256") or "")
    if found is None:
        return None
    if experiment.get("proposer") == "scheduled":
        return _advance_screen_scheduled(
            loop, experiment, artifact=found[0], eligible=found[1], now=now,
        )
    # Spec D16: a screened candidate waits at screened while the shadow
    # pool is full — the wait is a stage outcome noting capacity, not a
    # finding. Recorded once per distinct shadow count so the event log
    # is not spammed every tick.
    if _shadow_pool_full(loop, experiment, now=now):
        return None
    payload, eligible = found
    result = loop.screen(
        experiment["experiment_id"], artifact=payload, eligible=eligible, now=now,
    )
    if result["status"] == "shadow":
        family, scope = shadow_family_for_candidate(payload)
        record_stage_outcome(
            loop.store, experiment["experiment_id"], stage="shadow-family",
            outcome={"family": family, **scope}, now=now,
        )
    return result


def read_promotion_evaluation(store, *, asset: str, venue: str, contract: str,
                              quote: str, horizon: int) -> dict | None:
    """Latest stored FamilyPromotion.evaluate() row for one scope.

    Read-only observation: the driver never re-runs evaluate() itself
    (that would double-write promotion rows). Returns None when the
    hourly job has not evaluated this scope yet, or the table does not
    exist. The extra ``evaluated_at`` key lets the driver tell a new
    evaluation from one it already observed.
    """
    try:
        with store.connect() as conn:
            row = conn.execute(
                """SELECT evaluated_at, selected_family, status, evidence_json
                   FROM rival_family_promotion
                   WHERE asset=? AND venue=? AND contract=? AND quote=?
                     AND horizon_minutes=?
                   ORDER BY evaluated_at DESC LIMIT 1""",
                (asset, venue, contract, quote, horizon),
            ).fetchone()
    except Exception:  # noqa: BLE001 - no row yet is not an error
        return None
    if row is None:
        return None
    try:
        families = json.loads(row["evidence_json"] or "{}")
    except (ValueError, TypeError):
        return None
    if not isinstance(families, dict):
        return None
    return {
        "status": str(row["status"]),
        "selected_family": row["selected_family"],
        "families": families,
        "evaluated_at": str(row["evaluated_at"]),
    }


def _shadow_scope(experiment: dict) -> dict | None:
    """Family + scope recorded when the experiment entered shadow."""
    for event in reversed(experiment.get("outcome_json") or []):
        if event.get("stage") == "shadow-family":
            outcome = event.get("outcome") or {}
            if outcome.get("family") and outcome.get("asset"):
                return dict(outcome)
    return None


def _shadow_entry_at(experiment: dict) -> datetime | None:
    """When the experiment entered shadow (its screen transition event)."""
    for event in experiment.get("outcome_json") or []:
        if event.get("stage") == "shadow" and event.get("from") == "screened":
            return _parse_time(event.get("at"))
    return None


def _last_shadow_observation_at(experiment: dict) -> datetime | None:
    """When the driver last observed a shadow evaluation for this experiment."""
    latest = None
    for event in experiment.get("outcome_json") or []:
        if event.get("stage") == "shadow" and event.get("from") == "shadow":
            at = _parse_time(event.get("at"))
            if at is not None and (latest is None or at > latest):
                latest = at
    return latest


def _advance_shadow(loop: ResearchLoop, experiment: dict, *,
                    promoter=None, now: datetime) -> dict | None:
    """Stage shadow -> promoted / discarded, or wait for new evidence.

    Returns None when there is nothing new to observe: no stored
    evaluation yet, or the stored evaluation is not newer than the
    last observation and the lookback has not expired. A lookback
    expiry is observed even with a stale evaluation — the gates'
    verdict is "discarded", not "wait forever".
    """
    from parlay_bot.perpetuals.family_governance import FamilyPromotion

    scope = _shadow_scope(experiment)
    if scope is None:
        return None
    family = scope["family"]
    evaluation = read_promotion_evaluation(
        loop.store, asset=scope["asset"], venue=scope["venue"],
        contract=scope["contract"], quote=scope["quote"],
        horizon=int(scope["horizon_minutes"]),
    )
    entry_at = _shadow_entry_at(experiment)
    anchor = entry_at or _parse_time(experiment.get("created_at"))
    lookback_expired = anchor is not None and (now - anchor) > SHADOW_LOOKBACK
    if evaluation is None:
        if not lookback_expired:
            return None
        evaluation = {}
    else:
        evaluated_at = _parse_time(evaluation.get("evaluated_at"))
        last_observed = _last_shadow_observation_at(experiment)
        if (
            not lookback_expired
            and last_observed is not None
            and evaluated_at is not None
            and evaluated_at <= last_observed
        ):
            return None
    if experiment.get("proposer") == "scheduled" and not lookback_expired:
        # Spec D14: the variant shadows as its family, but a new fit
        # queues behind the pinned fit. The verdict belongs to the
        # variant's own artifact — the pinned fit's weights SHA must
        # match the variant's, or the variant waits (a stage outcome,
        # not a finding) until a newer evaluation pins its fit. The
        # wait is observed once per evaluation: the stage outcome
        # updates the last-observed time, so the same evaluation is
        # not re-read every tick.
        family_evaluation = (evaluation.get("families") or {}).get(family) or {}
        artifact = family_evaluation.get("artifact")
        pinned_sha = (artifact.get("weights_sha256")
                      if isinstance(artifact, dict) else None)
        variant_sha = scope.get("weights_sha256")
        if pinned_sha != variant_sha:
            record_stage_outcome(
                loop.store, experiment["experiment_id"], stage="shadow",
                outcome={"queued": True, "family": family,
                         "variant_weights_sha256": variant_sha,
                         "pinned_weights_sha256": pinned_sha},
                now=now)
            return None
    if promoter is None:
        promoter = FamilyPromotion(loop.store)
    return loop.shadow(
        experiment["experiment_id"], family=family, evaluation=evaluation,
        promoter=promoter, asset=scope["asset"], venue=scope["venue"],
        contract=scope["contract"], quote=scope["quote"],
        horizon=int(scope["horizon_minutes"]),
        lookback_expired=lookback_expired, now=now,
    )


def _advance_experiment(store, experiment: dict, *, trial_reports: dict,
                        candidates: dict, promoter=None,
                        now: datetime) -> dict | None:
    """Advance one experiment one stage, or None if its input is missing."""
    loop = ResearchLoop(store)
    status = experiment["status"]
    if status == "proposed":
        result = _advance_verify(loop, experiment, now=now)
    elif status == "ablating":
        result = _advance_ablation(
            loop, experiment, trial_reports=trial_reports, now=now)
        if result is None:
            return None
    elif status == "screened":
        result = _advance_screen(
            loop, experiment, candidates=candidates, now=now)
        if result is None:
            return None
    elif status == "shadow":
        result = _advance_shadow(loop, experiment, promoter=promoter, now=now)
        if result is None:
            return None
    else:
        return None
    return {
        "status": "advanced",
        "advanced": experiment["experiment_id"],
        "from": status,
        "to": result["status"],
    }


def advance_one(store, *, trial_reports: dict | None = None,
                candidates: dict | None = None, promoter=None,
                now: datetime | None = None,
                memory_pressure: bool = False) -> dict:
    """Advance the oldest open experiment whose stage input is available.

    Sync core, used by the bot task (via ``drive_research_loop_tick``)
    and directly by tests. ``trial_reports`` maps proposal SHA to
    ``{"trials": [...], "report_sha256": str}``; ``candidates`` maps
    proposal SHA to ``(artifact_bytes, eligible_bool)``. A tick that
    finds nothing to advance returns ``nothing_to_advance`` and changes
    nothing.
    """
    stamp = _stamp(now)
    if memory_pressure:
        return {"status": "skipped_memory_pressure", "advanced": None}
    for experiment in list_open_experiments(store, now=stamp):
        result = _advance_experiment(
            store, experiment,
            trial_reports=trial_reports or {},
            candidates=candidates or {},
            promoter=promoter, now=stamp,
        )
        if result is not None:
            return result
    return {"status": "nothing_to_advance", "advanced": None}


# --- Consumed trial reports (spec D17) ---------------------------------------
#
# Every handoff outcome is recorded once. A trial report re-delivered
# after a restart (the Drive scan returns the same file on later ticks)
# must be a provable no-op, not a re-apply. The key is
# (proposal_sha256, report_sha256) with ON CONFLICT DO NOTHING; the
# driver records it only after the ablation transition commits.

CONSUMED_REPORTS_SCHEMA = """CREATE TABLE IF NOT EXISTS rival_research_consumed_reports (
    proposal_sha256 TEXT NOT NULL,
    report_sha256 TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (proposal_sha256, report_sha256))"""


def is_report_consumed(store, proposal_sha256: str, report_sha256: str) -> bool:
    """True when this trial report was already applied to its experiment."""
    try:
        with store.connect() as conn:
            conn.execute(CONSUMED_REPORTS_SCHEMA)
            row = conn.execute(
                """SELECT 1 FROM rival_research_consumed_reports
                   WHERE proposal_sha256=? AND report_sha256=?""",
                (proposal_sha256, report_sha256),
            ).fetchone()
    except Exception:  # noqa: BLE001 - no ledger yet means not consumed
        return False
    return row is not None


def record_consumed_report(store, proposal_sha256: str, report_sha256: str, *,
                           now: datetime | None = None) -> bool:
    """Record one applied trial report. True when newly recorded."""
    stamp = _stamp(now).isoformat()
    with store.connect() as conn:
        conn.execute(CONSUMED_REPORTS_SCHEMA)
        row = conn.execute(
            """INSERT INTO rival_research_consumed_reports
               (proposal_sha256, report_sha256, recorded_at)
               VALUES (?,?,?) ON CONFLICT DO NOTHING""",
            (proposal_sha256, report_sha256, stamp),
        )
        return bool(row.rowcount == 1)


# --- Drive handoff (spec §2.4, D3) -------------------------------------------
#
# The GHA offline job already publishes trial reports (kind="report", in
# the reports folder) and screened candidates (kind="model" in the models
# folder, or kind="report" in the reports folder when the screen failed).
# The driver reads both folders through the same Drive client the
# candidate-import loop uses — no new client, no new credentials. A file
# that parses as neither a trial report nor a candidate is ignored; a
# trial naming a proposal SHA the driver does not know is ignored, never
# attached to the wrong experiment.


def _trials_from_report(payload: bytes) -> list[dict]:
    """Trial dicts from one published trial-report file, or []."""
    try:
        report = json.loads(payload)
    except (ValueError, TypeError, UnicodeDecodeError):
        return []
    if not isinstance(report, dict):
        return []
    if report.get("schema_version") != TRIAL_REPORT_SCHEMA:
        return []
    experiments = (report.get("llm_hypothesis_research") or {}).get("experiments")
    if not isinstance(experiments, list):
        return []
    return [trial for trial in experiments if isinstance(trial, dict)]


def _candidate_from_payload(payload: bytes) -> str | None:
    """Proposal SHA of a published candidate artifact, or None.

    LLM candidates carry ``lfm3_proposal_sha256`` in their model spec.
    Scheduled-variant artifacts carry ``scheduled_design_sha256`` —
    top-level, or in the model spec — naming the grid design they were
    trained for (spec D9). The training job records it when it trains
    a variant; an artifact without either key is not a loop candidate.
    """
    try:
        source = json.loads(payload)
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(source, dict):
        return None
    spec = source.get("model_spec")
    if isinstance(spec, dict):
        sha = (spec.get("lfm3_proposal_sha256")
               or spec.get("scheduled_design_sha256"))
        if _is_sha256(sha):
            return sha
    sha = source.get("scheduled_design_sha256")
    return sha if _is_sha256(sha) else None


async def fetch_loop_inputs(drive, *, report_pages: int = TRIAL_REPORT_SCAN_PAGES,
                            candidate_pages: int = CANDIDATE_SCAN_PAGES,
                            now: datetime | None = None) -> tuple[dict, dict]:
    """One bounded scan of the models + reports folders for the driver.

    Returns ``(trial_reports, candidates)``:
    - trial_reports: {proposal_sha256: {"trials": [...], "report_sha256": str}}
    - candidates: {proposal_sha256: (artifact_bytes, eligible_bool)}
    The models folder is scanned first, so an eligible (kind="model")
    artifact wins over a rejected copy of the same proposal in the
    reports folder. A Drive failure raises; the caller skips the tick.
    """
    _ = now  # reserved: a future staleness bound on scanned reports.
    trial_reports: dict[str, dict] = {}
    candidates: dict[str, tuple[bytes, bool]] = {}

    async def _scan_reports() -> None:
        cursor: str | None = None
        for _ in range(max(1, report_pages)):
            items, cursor = await drive.read_verified_research_reports(
                limit=20, page_token=cursor, return_next_page=True,
            )
            for _file_id, payload in items:
                report_sha = hashlib.sha256(payload).hexdigest()
                for trial in _trials_from_report(payload):
                    sha = trial.get("proposal_sha256")
                    if not _is_sha256(sha):
                        continue
                    entry = trial_reports.setdefault(
                        sha, {"trials": [], "report_sha256": report_sha})
                    entry["trials"].append(trial)
                sha = _candidate_from_payload(payload)
                if sha is not None and sha not in candidates:
                    candidates[sha] = (payload, False)
            if not cursor:
                break

    async def _scan_models() -> None:
        cursor: str | None = None
        for _ in range(max(1, candidate_pages)):
            items, cursor = await drive.read_verified_research_candidates(
                limit=20, page_token=cursor, return_next_page=True,
            )
            for _file_id, payload in items:
                sha = _candidate_from_payload(payload)
                if sha is not None and sha not in candidates:
                    candidates[sha] = (payload, True)
            if not cursor:
                break

    # Models first: an eligible artifact wins over a rejected copy.
    await _scan_models()
    await _scan_reports()
    return trial_reports, candidates


async def drive_research_loop_tick(store, *, drive=None,
                                   now: datetime | None = None,
                                   memory_pressure: bool = False) -> dict:
    """One driver tick: advance at most one experiment one stage, or open one.

    Async orchestrator for the bot task. Drive reads happen here (the
    Drive client is async) and only when the experiment being advanced
    needs them — verify and shadow need no Drive, so a Drive failure
    never blocks a verify; all store work runs on a worker thread so a
    slow pool checkout never freezes the Discord gateway heartbeat. A
    Drive failure skips the Drive-dependent stages for this tick — the
    experiments wait, nothing is invented. When nothing advances, the
    tick tries opening the next scheduled variant (spec D9/D10/D16):
    one action per tick, an advance OR an open.
    """
    stamp = _stamp(now)
    if memory_pressure:
        return {"status": "skipped_memory_pressure", "advanced": None}
    experiments = await asyncio.to_thread(list_open_experiments, store, now=stamp)
    trial_reports: dict = {}
    candidates: dict = {}
    inputs_fetched = False
    drive_failed = False
    for experiment in experiments:
        if (
            experiment["status"] in ("ablating", "screened")
            and drive is not None
            and not inputs_fetched
            and not drive_failed
        ):
            inputs_fetched = True
            try:
                trial_reports, candidates = await fetch_loop_inputs(drive)
            except Exception:  # noqa: BLE001 - Drive failure skips the tick
                _LOG.warning(
                    "RivaL research loop driver Drive read failed; "
                    "Drive-dependent stages wait this tick",
                    exc_info=True,
                )
                drive_failed = True
        result = await asyncio.to_thread(
            _advance_experiment, store, experiment,
            trial_reports=trial_reports, candidates=candidates, now=stamp,
        )
        if result is not None:
            return result
    if drive_failed:
        return {"status": "drive_unavailable", "advanced": None}
    # Nothing advanced this tick: try opening the next scheduled variant
    # (spec D9/D10/D16). An exhausted grid, a full offline pool, or a
    # spent weekly budget is a normal nothing-to-do, not an error.
    opened = await asyncio.to_thread(open_scheduled_variant, store, now=stamp)
    if opened is not None:
        return {"status": "opened", "opened": opened["experiment_id"],
                "variant_id": (opened.get("design_json") or {}).get("variant_id"),
                "proposal_sha256": opened.get("proposal_sha256")}
    return {"status": "nothing_to_advance", "advanced": None}
