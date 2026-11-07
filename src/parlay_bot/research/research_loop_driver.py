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
from parlay_bot.perpetuals.research_loop import (
    TERMINAL_STATUSES,
    ResearchLoop,
    record_stage_outcome,
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

    Only experiments carrying a ``proposal_sha256`` are drivable: those are
    the ones the hypothesis lab opened. Human-driven experiments (no
    proposal SHA, e.g. the archive driver's) are driven by their own
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
    absent, not proposed, corrupt, or outside the 7-day window the
    verify gate enforces; the verify step then fails the experiment
    with a finding, which is the honest outcome for a stale proposal.
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


def _advance_verify(loop: ResearchLoop, experiment: dict, *,
                    now: datetime) -> dict:
    """Stage proposed -> ablating (or failed, with a finding)."""
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


def _advance_ablation(loop: ResearchLoop, experiment: dict, *,
                      trial_reports: dict, now: datetime) -> dict | None:
    """Stage ablating -> screened (or discarded, with a finding).

    Returns None when no trial report for this proposal has been
    published yet: the experiment waits, it is not failed on absence.
    A report naming a different proposal SHA is ignored by the caller
    (trial_reports is keyed by proposal SHA).
    """
    entry = trial_reports.get(experiment.get("proposal_sha256") or "")
    if entry is None:
        return None
    return loop.ablate(
        experiment["experiment_id"],
        trials=entry["trials"],
        report_sha256=entry["report_sha256"],
        now=now,
    )


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
    """Proposal SHA of a published candidate artifact, or None."""
    try:
        source = json.loads(payload)
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(source, dict):
        return None
    spec = source.get("model_spec")
    if not isinstance(spec, dict):
        return None
    sha = spec.get("lfm3_proposal_sha256")
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
    """One driver tick: advance at most one experiment one stage.

    Async orchestrator for the bot task. Drive reads happen here (the
    Drive client is async) and only when the experiment being advanced
    needs them — verify and shadow need no Drive, so a Drive failure
    never blocks a verify; all store work runs on a worker thread so a
    slow pool checkout never freezes the Discord gateway heartbeat. A
    Drive failure skips the Drive-dependent stages for this tick — the
    experiments wait, nothing is invented.
    """
    stamp = _stamp(now)
    if memory_pressure:
        return {"status": "skipped_memory_pressure", "advanced": None}
    experiments = await asyncio.to_thread(list_open_experiments, store, now=stamp)
    if not experiments:
        return {"status": "nothing_to_advance", "advanced": None}
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
    return {"status": "nothing_to_advance", "advanced": None}
