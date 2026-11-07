"""Autonomous research lifecycle (batch 2 Phase 3, spec §3).

The experiment loop that keeps producing, testing, and retiring
challengers without letting the LLM decide anything:

    propose -> verify receipt -> offline ablation -> retrospective
    screen -> forward shadow -> promote or discard -> feedback

Decision rights (spec §3.3): the LLM proposes only. Every status
transition is made here, by deterministic code, from the numeric output
of the existing gates — ``verify_proposals()``,
``evaluate_hypothesis_ablations()``, ``verified_candidate()`` and
``FamilyPromotion.evaluate()`` / ``selected()``. No LLM output path in
this module can write a status transition, open a holdout, mark a
candidate passed, issue/settle/re-weight a forecast, or promote,
demote, or extend freshness. Any attempt to drive a transition with
actor="llm" (or any LLM-shaped payload) is refused with ValueError.

Append-only (spec §3.1, D6): there is no DELETE anywhere in this
module and no UPDATE that rewrites history. A status change appends a
new event to the experiment row's ``outcome_json`` event list (events
are never removed or rewritten); the ``status`` column is only the
latest-state pointer. Corrections are new rows that supersede, never
edits: ``supersede_experiment()`` links the old row to the new one.
Findings are pure INSERTs. Read windows: 7d hypotheses (existing
``recent_hypotheses``), 60d trials (``trial_summary``), 90d experiments
(``recent_experiments``); findings never expire.

D10 boundary: this module performs no Drive read. Offline ablation may
use verified Drive months via ``source_native_offline``; the live LLM
brief stays live-Postgres-only
(``analysis_evidence.py:312`` — ``cold_archive_readback:
"not_performed_by_live_research"``). The ablation/screen stages here
take already-computed inputs (trial dicts / artifact bytes) produced by
the existing offline machinery, so no Drive path is reachable from the
live research loop.

Receipts: promotion / first-serve receipts are NOT re-implemented
here. They are emitted by the Phase 1 ``promotion_receipts`` mechanism
(``FamilyPromotion.evaluate`` and ``recent_watches``) and surfaced
through the status card (``research_alerts``). Experiment visibility
uses the same status-card pattern: ``research_alerts`` reads recently
promoted experiments from this module's tables.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import uuid
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.research_hypotheses import (
    HYPOTHESES,
    trial_summary,
    valid_feature_indices,
)

_LOG = logging.getLogger(__name__)

# Read windows (spec §3.1, D6).
EXPERIMENT_READ_DAYS = 90
TRIAL_READ_DAYS = 60

STATUSES = (
    "proposed", "ablating", "screened", "shadow",
    "promoted", "discarded", "failed",
)
TERMINAL_STATUSES = ("promoted", "discarded", "failed")
# Every legal step of the loop, in order. No other transition exists.
# "proposed" -> "discarded" is the D15 expiry close: a proposal that
# aged past the freshness window is discarded with a finding naming the
# expiry, rather than failing verification as a corrupt receipt.
ALLOWED_TRANSITIONS = {
    "proposed": ("ablating", "discarded", "failed"),
    "ablating": ("screened", "discarded", "failed"),
    "screened": ("shadow", "discarded"),
    "shadow": ("promoted", "discarded"),
    "promoted": ("discarded",),  # demotion only; a promoted family is
    # never refreshed, it expires in 48h (spec §3.2 step 6).
    "discarded": (),
    "failed": (),
}
# Split capacity pools (spec D16, overrides D10): the offline pool is
# proposed/ablating/screened, the shadow pool is shadow. Each caps at 4,
# so four 21-day shadow trials cannot starve the fast offline stage.
OFFLINE_POOL_STATUSES = ("proposed", "ablating", "screened")
OFFLINE_POOL_MAX = 4
SHADOW_POOL_MAX = 4
PROPOSERS = ("llm", "human", "scheduled")
FINDING_KINDS = ("discovery", "mistake", "negative_result")
RECORDED_BY = ("llm", "human", "system")
# Actors allowed to drive a status transition. "llm" is deliberately
# absent: an LLM output can never be the actor of a transition (§3.3).
TRANSITION_ACTORS = ("system", "human")
MAX_FINDING_CHARS = 280

EXPERIMENTS_SCHEMA = """CREATE TABLE IF NOT EXISTS rival_research_experiments (
    experiment_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    proposer TEXT NOT NULL,
    proposal_sha256 TEXT,
    hypothesis TEXT NOT NULL,
    feature_indices_json TEXT,
    design_json TEXT NOT NULL,
    status TEXT NOT NULL,
    outcome_json TEXT NOT NULL,
    superseded_by TEXT,
    record_sha256 TEXT NOT NULL
)"""

FINDINGS_SCHEMA = """CREATE TABLE IF NOT EXISTS rival_research_findings (
    finding_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    recorded_by TEXT NOT NULL
)"""


def _stamp(now: datetime | None = None) -> datetime:
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    if stamp.utcoffset() is None:
        raise ValueError("UTC timestamp required")
    return stamp


def _row_dict(row) -> dict:
    # Iterate .keys() deliberately: row types differ by backend (a
    # sqlite Row iterates values, a dict row iterates keys).
    return {key: row[key] for key in row.keys()}  # noqa: SIM118


def _decode_experiment(row: dict) -> dict:
    out = dict(row)
    for key in ("feature_indices_json", "design_json", "outcome_json"):
        raw = out.get(key)
        out[key] = json.loads(raw) if isinstance(raw, str) else raw
    return out


def _record_sha256(*, experiment_id, created_at, proposer, proposal_sha256,
                   hypothesis, feature_indices, design) -> str:
    content = {
        "experiment_id": experiment_id,
        "created_at": created_at,
        "proposer": proposer,
        "proposal_sha256": proposal_sha256,
        "hypothesis": hypothesis,
        "feature_indices": feature_indices,
        "design": design,
    }
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":"),
                   default=str).encode()
    ).hexdigest()


def initialize(store) -> None:
    """Create both research-memory tables if they do not exist."""
    with store.connect() as conn:
        conn.execute(EXPERIMENTS_SCHEMA)
        conn.execute(FINDINGS_SCHEMA)


def create_experiment(store, *, proposer: str, hypothesis: str,
                      feature_indices: list[int] | None = None,
                      design: dict | None = None,
                      proposal_sha256: str | None = None,
                      experiment_id: str | None = None,
                      now: datetime | None = None) -> dict:
    """Open one experiment in status='proposed' (loop step 1).

    ``hypothesis`` is constrained to the fixed enum the LLM may propose
    from (spec §3.2 step 1); ``design`` carries the train/validation/
    holdout windows, model shape, and calibration method. The caller
    that stores the LLM proposal in ``rival_llm_feature_hypotheses``
    (``FeatureHypothesisLab``) passes its ``proposal_sha256``.
    """
    if proposer not in PROPOSERS:
        raise ValueError(f"unknown proposer: {proposer!r}")
    if hypothesis not in HYPOTHESES:
        raise ValueError(f"hypothesis outside the fixed enum: {hypothesis!r}")
    if feature_indices is not None and not valid_feature_indices(feature_indices):
        raise ValueError("feature_indices must be 2-8 sorted unique indices 0-9")
    if proposal_sha256 is not None and not (
        isinstance(proposal_sha256, str) and len(proposal_sha256) == 64
        and all(c in "0123456789abcdef" for c in proposal_sha256)
    ):
        raise ValueError("proposal_sha256 must be a SHA-256 hex digest")
    stamp = _stamp(now)
    design = dict(design or {})
    experiment_id = experiment_id or uuid.uuid4().hex
    created = stamp.isoformat()
    digest = _record_sha256(
        experiment_id=experiment_id, created_at=created, proposer=proposer,
        proposal_sha256=proposal_sha256, hypothesis=hypothesis,
        feature_indices=feature_indices, design=design,
    )
    initialize(store)
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO rival_research_experiments
               (experiment_id, created_at, proposer, proposal_sha256,
                hypothesis, feature_indices_json, design_json, status,
                outcome_json, superseded_by, record_sha256)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (experiment_id, created, proposer, proposal_sha256, hypothesis,
             json.dumps(feature_indices, separators=(",", ":"))
             if feature_indices is not None else None,
             json.dumps(design, sort_keys=True, separators=(",", ":")),
             "proposed",
             json.dumps([{"stage": "proposed", "at": created,
                          "actor": "system"}],
                        separators=(",", ":")),
             None, digest),
        )
    return get_experiment(store, experiment_id)


def get_experiment(store, experiment_id: str) -> dict | None:
    initialize(store)
    with store.connect() as conn:
        row = conn.execute(
            "SELECT * FROM rival_research_experiments WHERE experiment_id=?",
            (str(experiment_id),),
        ).fetchone()
    return None if row is None else _decode_experiment(_row_dict(row))


def list_findings(store, experiment_id: str) -> list[dict]:
    initialize(store)
    with store.connect() as conn:
        rows = conn.execute(
            """SELECT * FROM rival_research_findings
               WHERE experiment_id=? ORDER BY recorded_at""",
            (str(experiment_id),),
        ).fetchall()
    return [_row_dict(row) for row in rows]


def record_finding(store, *, experiment_id: str, kind: str, summary: str,
                   evidence_ref: str, recorded_by: str,
                   now: datetime | None = None) -> dict:
    """Append one finding (the mistakes-and-discoveries memory, §3.1).

    Pure INSERT. ``summary`` is human-readable, at most 280 chars;
    ``evidence_ref`` names the table + row id or receipt id backing it.
    """
    if kind not in FINDING_KINDS:
        raise ValueError(f"unknown finding kind: {kind!r}")
    if recorded_by not in RECORDED_BY:
        raise ValueError(f"unknown recorded_by: {recorded_by!r}")
    if not isinstance(summary, str) or not 1 <= len(summary) <= MAX_FINDING_CHARS:
        raise ValueError("finding summary must be 1-280 chars")
    if not isinstance(evidence_ref, str) or not evidence_ref.strip():
        raise ValueError("finding evidence_ref is required")
    if get_experiment(store, experiment_id) is None:
        raise ValueError(f"unknown experiment: {experiment_id!r}")
    stamp = _stamp(now).isoformat()
    finding_id = uuid.uuid4().hex
    initialize(store)
    with store.connect() as conn:
        conn.execute(
            """INSERT INTO rival_research_findings
               (finding_id, experiment_id, recorded_at, kind, summary,
                evidence_ref, recorded_by) VALUES (?,?,?,?,?,?,?)""",
            (finding_id, experiment_id, stamp, kind, summary,
             evidence_ref.strip(), recorded_by),
        )
    return {"finding_id": finding_id, "experiment_id": experiment_id,
            "recorded_at": stamp, "kind": kind, "summary": summary,
            "evidence_ref": evidence_ref.strip(), "recorded_by": recorded_by}


def _require_finding(store, experiment_id: str, finding: dict | None,
                     *, to_status: str, now: datetime) -> None:
    """Every closed experiment leaves a finding (spec §3.1).

    A transition into a terminal status is refused unless a finding is
    supplied with it or one already exists for the experiment. The
    refusal is an error, logged, never silently allowed.
    """
    if to_status not in TERMINAL_STATUSES:
        return
    if finding is not None:
        return
    if list_findings(store, experiment_id):
        return
    _LOG.error("RivaL research loop refused to close experiment=%s "
               "status=%s without a finding", experiment_id, to_status)
    raise ValueError(
        f"closing experiment {experiment_id} as {to_status} requires a finding"
    )


def transition(store, experiment_id: str, to_status: str, *,
               actor: str, outcome: dict | None = None,
               finding: dict | None = None,
               idempotency_key: str | None = None,
               now: datetime | None = None) -> dict:
    """Move an experiment one legal step, appending the event.

    ``actor`` must be "system" or "human". An LLM is never an actor:
    passing ``actor="llm"`` — or any LLM-shaped payload as the actor —
    raises ValueError and changes nothing. Transitions into a terminal
    status require a finding (see ``_require_finding``). The event is
    appended to ``outcome_json``; prior events are never removed or
    rewritten, so the row's history is append-only.

    ``idempotency_key`` (spec D17): when given, the key is stored with
    the appended event, and a retry with the same key returns the
    experiment as it already stands instead of appending a second event
    — a restart between the commit and the caller's bookkeeping is a
    confirmed no-op, not an error. The key is checked after actor and
    status validation, before the legality check, because a retried
    transition has already moved the status past the step it repeats.
    """
    if actor not in TRANSITION_ACTORS:
        raise ValueError(
            "LLM output cannot drive a status transition: "
            f"actor must be 'system' or 'human', not {actor!r}"
        )
    if to_status not in STATUSES:
        raise ValueError(f"unknown status: {to_status!r}")
    stamp = _stamp(now)
    experiment = get_experiment(store, experiment_id)
    if experiment is None:
        raise ValueError(f"unknown experiment: {experiment_id!r}")
    if idempotency_key is not None:
        for event in experiment["outcome_json"] or []:
            if (isinstance(event, dict)
                    and event.get("idempotency_key") == idempotency_key):
                return experiment
    current = experiment["status"]
    if to_status not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(
            f"illegal experiment transition {current} -> {to_status} "
            f"for experiment {experiment_id}"
        )
    _require_finding(store, experiment_id, finding, to_status=to_status, now=stamp)
    events = list(experiment["outcome_json"] or [])
    event = {"stage": to_status, "from": current, "at": stamp.isoformat(),
             "actor": actor, "outcome": outcome or {}}
    if idempotency_key is not None:
        event["idempotency_key"] = idempotency_key
    events.append(event)
    with store.connect() as conn:
        if finding is not None:
            conn.execute(
                """INSERT INTO rival_research_findings
                   (finding_id, experiment_id, recorded_at, kind, summary,
                    evidence_ref, recorded_by) VALUES (?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, experiment_id, stamp.isoformat(),
                 finding["kind"], finding["summary"], finding["evidence_ref"],
                 finding["recorded_by"]),
            )
        conn.execute(
            """UPDATE rival_research_experiments
               SET status=?, outcome_json=? WHERE experiment_id=?""",
            (to_status,
             json.dumps(events, sort_keys=True, separators=(",", ":")),
             experiment_id),
        )
    return get_experiment(store, experiment_id)


def record_stage_outcome(store, experiment_id: str, *, stage: str,
                         outcome: dict, now: datetime | None = None) -> dict:
    """Append a stage's numeric outcome without changing status.

    Used while an experiment accumulates forward-shadow evidence
    (spec §3.2 step 5): the shadow window and paired forward scores are
    recorded as they arrive; only the promotion gate moves the status.
    """
    stamp = _stamp(now)
    experiment = get_experiment(store, experiment_id)
    if experiment is None:
        raise ValueError(f"unknown experiment: {experiment_id!r}")
    if experiment["status"] in TERMINAL_STATUSES:
        raise ValueError(
            f"cannot record a stage outcome on closed experiment {experiment_id}"
        )
    events = list(experiment["outcome_json"] or [])
    events.append({"stage": stage, "from": experiment["status"],
                   "at": stamp.isoformat(), "actor": "system",
                   "outcome": outcome or {}})
    with store.connect() as conn:
        conn.execute(
            """UPDATE rival_research_experiments
               SET outcome_json=? WHERE experiment_id=?""",
            (json.dumps(events, sort_keys=True, separators=(",", ":")),
             experiment_id),
        )
    return get_experiment(store, experiment_id)


def supersede_experiment(store, old_experiment_id: str, *,
                         new_experiment: dict, now: datetime | None = None) -> dict:
    """Corrections are new rows that supersede, never edits (§3.1).

    ``new_experiment`` is the dict returned by ``create_experiment`` for
    the replacement row; the old row keeps its history and gains only the
    ``superseded_by`` link to the new row's id.
    """
    old = get_experiment(store, old_experiment_id)
    if old is None:
        raise ValueError(f"unknown experiment: {old_experiment_id!r}")
    if old["superseded_by"] is not None:
        raise ValueError(f"experiment {old_experiment_id} already superseded")
    new_id = str(new_experiment["experiment_id"])
    if get_experiment(store, new_id) is None:
        raise ValueError(f"superseding experiment {new_id} does not exist")
    with store.connect() as conn:
        conn.execute(
            """UPDATE rival_research_experiments SET superseded_by=?
               WHERE experiment_id=?""",
            (new_id, old_experiment_id),
        )
    return get_experiment(store, old_experiment_id)


def recent_experiments(store, *, now: datetime | None = None,
                       days: int = EXPERIMENT_READ_DAYS,
                       asset: str | None = None) -> list[dict]:
    """Experiments from the last 90 days, newest first (§3.1, D6)."""
    stamp = _stamp(now)
    initialize(store)
    with store.connect() as conn:
        rows = conn.execute(
            """SELECT * FROM rival_research_experiments
               WHERE created_at>=? ORDER BY created_at DESC""",
            ((stamp - timedelta(days=days)).isoformat(),),
        ).fetchall()
    out = []
    for row in rows:
        experiment = _decode_experiment(_row_dict(row))
        if asset is not None:
            design = experiment["design_json"] or {}
            if design.get("asset") != asset:
                continue
        out.append(experiment)
    return out


def pool_counts(store, *, now: datetime | None = None) -> dict:
    """Open experiments per capacity pool (spec D16).

    Counts non-terminal experiments in the same 90-day read window the
    driver sees: the offline pool (proposed/ablating/screened) and the
    shadow pool (shadow). An experiment stalled past the window drops
    out of the driver's view and out of the count by the same design.
    """
    counts = {"offline": 0, "shadow": 0}
    for experiment in recent_experiments(store, now=now):
        status = experiment["status"]
        if status in OFFLINE_POOL_STATUSES:
            counts["offline"] += 1
        elif status == "shadow":
            counts["shadow"] += 1
    return counts


def _numeric_outcomes(experiment: dict) -> dict:
    """Only numeric stage outcomes — no status, no promoted signal.

    The feedback fed to the next proposal (spec §3.2 step 7) must carry
    recorded numeric summaries only. The LLM is never told whether a
    proposal promoted except through these numbers.
    """
    numeric: dict[str, object] = {}
    for event in experiment["outcome_json"] or []:
        outcome = event.get("outcome") or {}
        for key, value in outcome.items():
            # Never leak a promotion signal: keys naming promotion, and
            # booleans (a bool is an int in Python), carry no number.
            if "promot" in key.lower() or "status" in key.lower():
                continue
            if type(value) in (int, float) and math.isfinite(value):
                numeric[key] = value
            elif isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    if ("promot" in sub_key.lower()
                            or "status" in sub_key.lower()):
                        continue
                    if type(sub_value) in (int, float) and math.isfinite(sub_value):
                        numeric[f"{key}.{sub_key}"] = sub_value
    return numeric


def prior_trials_context(store, *, asset: str,
                         now: datetime | None = None) -> list[dict]:
    """Feedback for the next proposal: 60d trials + 90d experiments.

    Trial summaries (max 12 ideas) plus per-experiment numeric outcomes.
    Contains hypothesis names, feature indices, and numbers only — no
    ``status`` field and no promoted/discarded/failed wording, so the
    next proposal sees aggregate numeric outcomes, not a live "you won"
    signal (spec §3.2 step 7, §3.6).
    """
    stamp = _stamp(now)
    context = list(trial_summary(store, asset=asset, now=stamp))
    for experiment in recent_experiments(store, now=stamp, asset=asset):
        numeric = _numeric_outcomes(experiment)
        # An experiment with no recorded numbers yet (e.g. only just
        # proposed) has nothing to teach the next proposal; skip it so
        # the context stays the recorded numeric summaries.
        if not numeric:
            continue
        context.append({
            "hypothesis": experiment["hypothesis"],
            "feature_indices": experiment["feature_indices_json"],
            "tests": len(experiment["outcome_json"] or []),
            **numeric,
        })
    return context


def publish_kind(*, eligible: bool) -> str:
    """Drive publication kind (spec §3.2 step 4, source_native_offline).

    ``kind="model"`` only if the screened candidate is eligible, else
    ``kind="report"``. The report states live_approved: False,
    promoted_live: False, changes_live_weights: False — publication is
    never promotion.
    """
    return "model" if eligible else "report"


def run_verify(store, experiment_id: str, receipt: object, *,
               venue: str, now: datetime | None = None) -> dict:
    """Loop step 2: verify the proposal receipt, deterministically.

    Calls the existing ``verify_proposals()`` boundary
    (``llm_feature_ablation.py:41-100``). Success moves the experiment
    to ``ablating``; any verification failure moves it to ``failed``
    with a finding row, and the loop ends.
    """
    from parlay_bot.research.llm_feature_ablation import verify_proposals

    stamp = _stamp(now)
    experiment = get_experiment(store, experiment_id)
    if experiment is None:
        raise ValueError(f"unknown experiment: {experiment_id!r}")
    if experiment["status"] != "proposed":
        raise ValueError(
            f"verify requires status 'proposed', experiment {experiment_id} "
            f"is {experiment['status']!r}"
        )
    try:
        accepted = verify_proposals(receipt, venue=venue, now=stamp)
    except Exception as exc:  # noqa: BLE001 - any verification failure ends the loop
        _LOG.warning("RivaL research loop receipt verification failed "
                     "experiment=%s (%s)", experiment_id, type(exc).__name__)
        return transition(
            store, experiment_id, "failed", actor="system",
            outcome={"stage": "verify", "verified": False,
                     "error": type(exc).__name__},
            finding={"kind": "negative_result",
                     "summary": "Proposal receipt failed verification; "
                                "experiment closed without ablation.",
                     "evidence_ref": f"rival_research_experiments:{experiment_id}",
                     "recorded_by": "system"},
            now=stamp)
    if not accepted:
        return transition(
            store, experiment_id, "failed", actor="system",
            outcome={"stage": "verify", "verified": False, "accepted": 0},
            finding={"kind": "negative_result",
                     "summary": "Proposal receipt verified as empty; "
                                "experiment closed without ablation.",
                     "evidence_ref": f"rival_research_experiments:{experiment_id}",
                     "recorded_by": "system"},
            now=stamp)
    return transition(
        store, experiment_id, "ablating", actor="system",
        outcome={"stage": "verify", "verified": True,
                 "accepted": len(accepted),
                 "proposal_sha256": accepted[0].get("record_sha256")},
        now=stamp)


def _ablation_passed(trial: dict) -> bool:
    """The deterministic ablation gate, read off the trial record.

    The numeric gates live inside ``evaluate_hypothesis_ablations()``
    (validation Brier gain > 0.005, ECE <= 0.10, holdout delta > 0.005);
    this loop only reads the recorded outcome: the sealed holdout was
    examined, it was beaten, and the candidate was screened for future
    shadow. The loop never re-implements the gates.
    """
    return bool(
        trial.get("holdout_examined")
        and isinstance(trial.get("holdout_brier_delta"), (int, float))
        and trial["holdout_brier_delta"] > 0.005
        and trial.get("retrospective_candidate_status")
        == "screened_for_future_shadow"
    )


def run_ablation(store, experiment_id: str, *, trials: list[dict] | None = None,
                 ablate=None, ablate_kwargs: dict | None = None,
                 report_sha256: str | None = None,
                 idempotency_key: str | None = None,
                 now: datetime | None = None) -> dict:
    """Loop step 3: offline ablation, wired to the existing machinery.

    ``trials`` are the dicts returned by ``evaluate_hypothesis_ablations()``
    (``llm_feature_ablation.py:101-240``); when omitted, ``ablate`` (default
    that function) is called with ``ablate_kwargs``. Passing trials are
    recorded in ``rival_llm_hypothesis_trials`` via ``record_trials()``
    when ``report_sha256`` is given. A pass moves the experiment to
    ``screened``; any failure moves it to ``discarded`` with a finding.
    ``idempotency_key`` (spec D17) is passed through to the transition,
    so a re-delivered trial report re-applies as a no-op.
    """
    from parlay_bot.perpetuals.research_hypotheses import record_trials
    from parlay_bot.research.llm_feature_ablation import (
        evaluate_hypothesis_ablations,
    )

    stamp = _stamp(now)
    experiment = get_experiment(store, experiment_id)
    if experiment is None:
        raise ValueError(f"unknown experiment: {experiment_id!r}")
    if experiment["status"] != "ablating":
        raise ValueError(
            f"ablation requires status 'ablating', experiment {experiment_id} "
            f"is {experiment['status']!r}"
        )
    if trials is None:
        try:
            trials = (ablate or evaluate_hypothesis_ablations)(**(ablate_kwargs or {}))
        except Exception as exc:  # noqa: BLE001 - an ablation failure ends the loop
            _LOG.warning("RivaL research loop ablation failed experiment=%s (%s)",
                         experiment_id, type(exc).__name__)
            return transition(
                store, experiment_id, "discarded", actor="system",
                outcome={"stage": "ablation", "trials": 0,
                         "error": type(exc).__name__},
                finding={"kind": "negative_result",
                         "summary": "Offline ablation errored before producing "
                                    "a result; idea discarded.",
                         "evidence_ref": f"rival_research_experiments:{experiment_id}",
                         "recorded_by": "system"},
                idempotency_key=idempotency_key, now=stamp)
    trials = list(trials or [])
    if report_sha256 is not None:
        try:
            saved = record_trials(store, trials, report_sha256=report_sha256,
                                  now=stamp)
        except ValueError:
            saved = 0
    else:
        saved = 0
    passed = [trial for trial in trials if _ablation_passed(trial)]
    numeric = {
        "stage": "ablation",
        "trials": len(trials),
        "trials_recorded": saved,
        "passed_trials": len(passed),
        "validation_brier_delta": max(
            (float(t["validation_brier_delta"]) for t in trials
             if isinstance(t.get("validation_brier_delta"), (int, float))),
            default=None),
        "holdout_brier_delta": max(
            (float(t["holdout_brier_delta"]) for t in trials
             if isinstance(t.get("holdout_brier_delta"), (int, float))),
            default=None),
    }
    if not passed:
        return transition(
            store, experiment_id, "discarded", actor="system",
            outcome=numeric,
            finding={"kind": "negative_result",
                     "summary": "Offline ablation did not beat the baseline "
                                "on the sealed holdout; idea discarded.",
                     "evidence_ref": f"rival_research_experiments:{experiment_id}",
                     "recorded_by": "system"},
            idempotency_key=idempotency_key, now=stamp)
    return transition(store, experiment_id, "screened", actor="system",
                      outcome=numeric, idempotency_key=idempotency_key,
                      now=stamp)


def run_screen(store, experiment_id: str, *, artifact: bytes | None = None,
               screen=None, eligible: bool = False,
               now: datetime | None = None) -> dict:
    """Loop step 4: retrospective screen, wired to the existing gate.

    ``screen`` defaults to ``verified_candidate()``
    (``candidate_registry.py:82-245``) called on ``artifact``; tests may
    substitute a stub at that boundary. ``eligible`` is the
    ``source_native_offline`` screening-receipt result that decides the
    Drive publication kind ("model" vs "report"). A passed screen moves
    the experiment to ``shadow``; anything else discards it with a
    finding. Publication is never promotion.
    """
    from parlay_bot.research.candidate_registry import verified_candidate

    stamp = _stamp(now)
    experiment = get_experiment(store, experiment_id)
    if experiment is None:
        raise ValueError(f"unknown experiment: {experiment_id!r}")
    if experiment["status"] != "screened":
        raise ValueError(
            f"screen requires status 'screened', experiment {experiment_id} "
            f"is {experiment['status']!r}"
        )
    checker = screen or verified_candidate
    try:
        result = checker(artifact) if artifact is not None else checker()
    except Exception as exc:  # noqa: BLE001 - a screen failure ends the loop
        _LOG.warning("RivaL research loop screen failed experiment=%s (%s)",
                     experiment_id, type(exc).__name__)
        return transition(
            store, experiment_id, "discarded", actor="system",
            outcome={"stage": "screen", "retrospective_screen": None,
                     "publish_kind": publish_kind(eligible=False),
                     "live_approved": False, "promoted_live": False,
                     "changes_live_weights": False,
                     "error": type(exc).__name__},
            finding={"kind": "negative_result",
                     "summary": "Retrospective screen errored before producing "
                                "a result; candidate not admitted to shadow.",
                     "evidence_ref": f"rival_research_experiments:{experiment_id}",
                     "recorded_by": "system"},
            now=stamp)
    screen_status = result.get("retrospective_screen") if isinstance(result, dict) else None
    outcome = {"stage": "screen", "retrospective_screen": screen_status,
               "publish_kind": publish_kind(eligible=eligible),
               "live_approved": False, "promoted_live": False,
               "changes_live_weights": False}
    if screen_status != "passed":
        return transition(
            store, experiment_id, "discarded", actor="system",
            outcome=outcome,
            finding={"kind": "negative_result",
                     "summary": "Retrospective screen did not pass; "
                                "candidate not admitted to forward shadow.",
                     "evidence_ref": f"rival_research_experiments:{experiment_id}",
                     "recorded_by": "system"},
            now=stamp)
    return transition(store, experiment_id, "shadow", actor="system",
                      outcome=outcome, now=stamp)


def run_shadow_promotion(store, experiment_id: str, *, family: str,
                         evaluation: dict | None = None,
                         promoter=None, asset: str | None = None,
                         venue: str | None = None, contract: str | None = None,
                         quote: str | None = None, horizon: int | None = None,
                         lookback_expired: bool = False,
                         now: datetime | None = None) -> dict:
    """Loop steps 5-6: forward shadow, then promote or discard.

    ``evaluation`` is the dict returned by ``FamilyPromotion.evaluate()``
    for the stream; when ``promoter`` (a ``FamilyPromotion``) and the
    stream scope are given, the ``selected()`` identity check is run
    live instead of trusting the evaluation dict. The shadow window and
    paired forward scores are recorded as they accumulate; the status
    moves only on the deterministic outcome:

    - ``selected()`` returns the family -> ``promoted`` + discovery finding.
    - the 30-day lookback expires with no promotion -> ``discarded`` + finding.
    - a previously promoted family fails re-evaluation -> ``discarded``
      with the failing evidence recorded (demotion; the old promotion
      row expires in 48h and is not refreshed).
    - otherwise the experiment stays in ``shadow`` and the evaluation
      numerics are appended as a stage outcome.
    """
    stamp = _stamp(now)
    experiment = get_experiment(store, experiment_id)
    if experiment is None:
        raise ValueError(f"unknown experiment: {experiment_id!r}")
    if experiment["status"] not in ("shadow", "promoted"):
        raise ValueError(
            f"shadow promotion requires status 'shadow' or 'promoted', "
            f"experiment {experiment_id} is {experiment['status']!r}"
        )
    evaluation = dict(evaluation or {})
    family_evidence = (evaluation.get("families") or {}).get(family) or {}
    numeric = {
        "stage": "shadow",
        "targets": family_evidence.get("targets"),
        "days": family_evidence.get("days"),
        "challenger_trades": family_evidence.get("challenger_trades"),
        "quote_coverage": family_evidence.get("quote_coverage"),
        "ece": family_evidence.get("ece"),
        "forward_gain": family_evidence.get("forward_gain"),
        "evaluation_status": evaluation.get("status"),
        "selected_family": evaluation.get("selected_family"),
    }
    selected = None
    if promoter is not None:
        selected = promoter.selected(
            asset=asset, venue=venue, contract=contract, quote=quote,
            horizon=horizon, now=stamp)
    else:
        selected = evaluation.get("selected_family")
    if selected == family:
        if experiment["status"] == "promoted":
            return record_stage_outcome(
                store, experiment_id, stage="shadow", outcome=numeric, now=stamp)
        return transition(
            store, experiment_id, "promoted", actor="system",
            outcome=numeric,
            finding={"kind": "discovery",
                     "summary": f"{family} earned paper promotion on the "
                                "forward shadow record; see promotion receipt.",
                     "evidence_ref": f"rival_research_experiments:{experiment_id}",
                     "recorded_by": "system"},
            now=stamp)
    if experiment["status"] == "promoted":
        # Demotion: not refreshed, expires in 48h; record the failing evidence.
        return transition(
            store, experiment_id, "discarded", actor="system",
            outcome={**numeric, "demoted": True},
            finding={"kind": "mistake",
                     "summary": f"{family} failed re-evaluation after promotion; "
                                "demoted, promotion row expires unrefreshed.",
                     "evidence_ref": f"rival_research_experiments:{experiment_id}",
                     "recorded_by": "system"},
            now=stamp)
    if lookback_expired:
        return transition(
            store, experiment_id, "discarded", actor="system",
            outcome=numeric,
            finding={"kind": "negative_result",
                     "summary": "Forward shadow did not meet the promotion "
                                "floors within the 30-day lookback; discarded.",
                     "evidence_ref": f"rival_research_experiments:{experiment_id}",
                     "recorded_by": "system"},
            now=stamp)
    return record_stage_outcome(store, experiment_id, stage="shadow",
                                outcome=numeric, now=stamp)


async def run_propose(store, *, asset: str, venues: tuple[str, ...], ai,
                      background: bool = False, background_opt_in: bool = False,
                      now: datetime | None = None) -> dict:
    """Loop step 1: propose via the LLM, gated by the background opt-in.

    Background (automatic) proposals run only when
    ``rival_llm_background_opt_in`` is True (spec §3.4; Phase 2
    ``llm_background_enabled``). With the opt-in off, no LLM call is
    made at all. User-initiated proposals (``background=False``) do not
    consult the flag; they still reserve quota inside
    ``propose_feature_hypothesis()``. The proposal is validated against
    the fixed enum and stored as a ``proposed`` experiment; storing the
    proposal row itself (6-hour slot claim) stays with
    ``FeatureHypothesisLab``.
    """
    from parlay_bot.perpetuals.research_hypotheses import validate_proposal

    stamp = _stamp(now)
    if background and not background_opt_in:
        _LOG.info("RivaL research loop background proposal skipped "
                  "asset=%s reason=opt_in_disabled", asset)
        return {"status": "opt_in_required", "asset": asset}
    if ai is None or not getattr(ai, "available", False):
        return {"status": "unavailable", "asset": asset}
    prior = prior_trials_context(store, asset=asset, now=stamp)
    result = await ai.propose_feature_hypothesis(
        asset=asset, venues=venues,
        **({"prior_trials": prior} if prior else {}),
    )
    if (not validate_proposal(result) or result.get("asset") != asset
            or not isinstance(result.get("venues"), list)
            or not result["venues"] or not all(v in venues for v in result["venues"])):
        raise ValueError("invalid LFM3 hypothesis proposal")
    # The proposal SHA is the hypotheses-table record digest, created by
    # FeatureHypothesisLab when it stores the proposal row; the experiment
    # links it there (see research_hypotheses.run_once).
    experiment = create_experiment(
        store, proposer="llm", hypothesis=result["hypothesis"],
        feature_indices=(result.get("feature_indices")
                         if result["hypothesis"] == "feature_subset_v2" else None),
        design={"asset": asset, "venues": list(result["venues"]),
                "model_shape": "offline-live-contract-logistic-v3",
                "calibration_method": "platt-scaling-ece<=0.10"},
        now=stamp)
    return {"status": "proposed", "asset": asset,
            "hypothesis": result["hypothesis"],
            "experiment_id": experiment["experiment_id"]}


class ResearchLoop:
    """Store-bound facade over the module-level loop functions."""

    def __init__(self, store):
        self.store = store

    def initialize(self) -> None:
        initialize(self.store)

    def create(self, **kwargs) -> dict:
        return create_experiment(self.store, **kwargs)

    def get(self, experiment_id: str) -> dict | None:
        return get_experiment(self.store, experiment_id)

    def transition(self, experiment_id: str, to_status: str, **kwargs) -> dict:
        return transition(self.store, experiment_id, to_status, **kwargs)

    def finding(self, **kwargs) -> dict:
        return record_finding(self.store, **kwargs)

    def findings(self, experiment_id: str) -> list[dict]:
        return list_findings(self.store, experiment_id)

    def verify(self, experiment_id: str, receipt: object, **kwargs) -> dict:
        return run_verify(self.store, experiment_id, receipt, **kwargs)

    def ablate(self, experiment_id: str, **kwargs) -> dict:
        return run_ablation(self.store, experiment_id, **kwargs)

    def screen(self, experiment_id: str, **kwargs) -> dict:
        return run_screen(self.store, experiment_id, **kwargs)

    def shadow(self, experiment_id: str, **kwargs) -> dict:
        return run_shadow_promotion(self.store, experiment_id, **kwargs)

    async def propose(self, **kwargs) -> dict:
        return await run_propose(self.store, **kwargs)

    def prior_trials(self, *, asset: str, now: datetime | None = None) -> list[dict]:
        return prior_trials_context(self.store, asset=asset, now=now)

    def recent(self, **kwargs) -> list[dict]:
        return recent_experiments(self.store, **kwargs)
