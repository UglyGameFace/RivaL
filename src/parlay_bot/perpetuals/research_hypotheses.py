"""Feature-hypothesis registry, proposal verification and trial records.

The proposal registry and the deterministic verification/trial machinery
(shared by the research loop and the offline drivers) live here. The
LFM3 proposer that created new proposals was retired with the
RivaL-Intelligence service; existing proposal and trial rows remain
readable and gradeable through the functions below.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from datetime import UTC, datetime, timedelta

_LOG = logging.getLogger(__name__)
ASSETS = ("BTC", "ETH", "SOL", "XRP")
HYPOTHESES = ("trend_only", "momentum_only", "volatility_only", "volume_only",
              "feature_subset_v2")
MODEL = "LiquidAI/LFM3-1.7B-GGUF:Q4_K_M"
# Proposal freshness window (spec D15, amended 2026-11-07): 9 days, not 7.
# The GHA venue rotation is 8 days, so a 7-day window strands a proposal
# made just after its venue's visit day. 9 exceeds the 8-day rotation (and
# the 4-day Bitget asset cycle) with a day to spare. This constant is the
# single source of truth: recent_hypotheses(), verify_proposals(), and the
# driver's expiry close must agree, or the job would pull proposals the
# verifier then rejects as stale.
PROPOSAL_FRESHNESS_DAYS = 9


def valid_feature_indices(value: object) -> bool:
    """Canonical subset with fixed live-feature positions, never LLM weights."""
    return (
        isinstance(value, list) and 2 <= len(value) <= 8
        and all(type(i) is int and 0 <= i < 10 for i in value)
        and value == sorted(set(value))
    )


def validate_proposal(data: object) -> bool:
    """Accept exactly a controlled research question, not an LLM prediction."""
    return (
        isinstance(data, dict)
        and data.get("model") == MODEL
        and data.get("hypothesis") in HYPOTHESES
        and data.get("research_only") is True
        and data.get("live_model_promoted") is False
        and data.get("retrospective_screen") == "not_tested"
        and data.get("prediction") is None
        and (valid_feature_indices(data.get("feature_indices"))
             if data.get("hypothesis") == "feature_subset_v2"
             else data.get("feature_indices") is None)
        and all(
            isinstance(data.get(key), str)
            and 5 <= len(data[key].strip()) <= 120
            for key in ("rationale", "falsification")
        )
    )


def recent_hypotheses(store, *, venue: str, now: datetime | None = None) -> list[dict]:
    """Read bounded, genuine prior proposals for one exact source venue.

    Oldest first (spec D15): the one-per-asset dedupe keeps the oldest
    eligible proposal per asset, so an older idea is never silently
    dropped in favour of a newer one for the same asset.
    """
    current = (now or datetime.now(UTC)).astimezone(UTC)
    with store.connect() as conn:
        # Legacy hosts can have hypothesis receipts without a research ledger.
        ledger = store.database.column_names(conn, "rival_research_experiments")
        pending = """AND NOT EXISTS (
            SELECT 1 FROM rival_research_experiments e
            WHERE e.proposal_sha256=h.record_sha256
              AND e.status IN ('screened', 'shadow', 'promoted', 'discarded', 'failed')
        )""" if "proposal_sha256" in ledger else ""
        rows = conn.execute(
            f"""SELECT h.* FROM rival_llm_feature_hypotheses h
               WHERE h.status='proposed' AND h.attempted_at>=? {pending}
               ORDER BY attempted_at ASC LIMIT 96""",
            ((current - timedelta(days=PROPOSAL_FRESHNESS_DAYS)).isoformat(),),
        ).fetchall()
    results = []
    seen = set()
    for row in rows:
        try:
            asset, mode = row["asset"], row["hypothesis"]
            if asset not in ASSETS or mode not in HYPOTHESES or asset in seen:
                continue
            if row["model"] != MODEL:
                continue
            venues = json.loads(row["source_venues_json"])
            if (not isinstance(venues, list) or venue not in venues
                    or not all(isinstance(v, str) and re.fullmatch(r"[a-z0-9_-]{2,24}", v)
                               for v in venues)):
                continue
            # The DB query bounds attempted_at for operational retries, but
            # the offline verifier binds freshness to the original UTC slot.
            # A recently retried OLD proposal must never be relayed as fresh.
            slot = datetime.fromisoformat(str(row["slot_utc"]))
            if (slot.utcoffset() is None
                    or not timedelta(0) <= current - slot.astimezone(UTC)
                    <= timedelta(days=PROPOSAL_FRESHNESS_DAYS)):
                continue
            record = {
                "asset": asset, "slot_utc": row["slot_utc"], "model": MODEL,
                "evidence_sha256": row["evidence_sha256"],
                "hypothesis": mode, "venues": venues,
            }
            # A relay may read a legacy host before the next scheduled
            # LFM3 attempt migrates its table. Do not mutate on a read.
            indices_json = dict(row).get("feature_indices_json")
            if mode == "feature_subset_v2":
                if not isinstance(indices_json, str):
                    continue
                indices = json.loads(indices_json)
                if not valid_feature_indices(indices):
                    continue
                record["feature_indices"] = indices
            elif indices_json is not None:
                continue
            digest = hashlib.sha256(
                json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if digest != row["record_sha256"]:
                continue
            results.append({**record, "record_sha256": digest})
            seen.add(asset)
        except (KeyError, ValueError, TypeError):
            continue
    return results


def hypothesis_attempt_status(store, *, now: datetime | None = None) -> dict:
    """Small read-only host diagnostic; never disclose prompts or credentials."""
    stamp = now or datetime.now(UTC)
    if stamp.utcoffset() is None:
        raise ValueError("UTC research status clock required")
    try:
        with store.connect() as conn:
            rows = conn.execute(
                """SELECT asset,slot_utc,attempted_at,status,attempt_count
                   FROM rival_llm_feature_hypotheses
                   WHERE attempted_at>=?
                   ORDER BY attempted_at DESC LIMIT 32""",
                ((stamp.astimezone(UTC)-timedelta(days=2)).isoformat(),),
            ).fetchall()
    except Exception:  # noqa: BLE001 - legacy hosts may not have the table
        return {"status": "unavailable", "latest_by_asset": []}
    seen, latest = set(), []
    for row in rows:
        if row["asset"] not in ASSETS or row["asset"] in seen:
            continue
        seen.add(row["asset"])
        latest.append({
            "asset": row["asset"], "slot_utc": str(row["slot_utc"]),
            "status": str(row["status"]),
            "attempt_count": int(row["attempt_count"]),
            "attempted_at": str(row["attempted_at"]),
        })
    return {
        "status": "recorded" if latest else "no_recent_attempts",
        "latest_by_asset": latest,
    }


HYPOTHESES_SCHEMA = """CREATE TABLE IF NOT EXISTS rival_llm_feature_hypotheses (
  asset TEXT NOT NULL,
  slot_utc TEXT NOT NULL,
  attempted_at TEXT NOT NULL,
  evidence_sha256 TEXT,
  model TEXT,
  hypothesis TEXT,
  feature_indices_json TEXT,
  rationale TEXT,
  falsification TEXT,
  source_venues_json TEXT,
  record_sha256 TEXT,
  status TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(asset,slot_utc)
)"""


TRIALS_SCHEMA = """CREATE TABLE IF NOT EXISTS rival_llm_hypothesis_trials (
    proposal_sha256 TEXT NOT NULL, venue TEXT NOT NULL, contract TEXT NOT NULL,
    horizon_minutes INTEGER NOT NULL, asset TEXT NOT NULL, hypothesis TEXT NOT NULL,
    feature_indices_json TEXT NOT NULL, baseline_brier DOUBLE PRECISION NOT NULL,
    llm_brier DOUBLE PRECISION NOT NULL, validation_count INTEGER NOT NULL,
    report_sha256 TEXT NOT NULL, recorded_at TEXT NOT NULL,
    outcome_status TEXT, outcome_reason TEXT,
    PRIMARY KEY (proposal_sha256, venue, contract, horizon_minutes))"""

# Rejection status -> short human-readable reason (spec D17). The status
# itself is the machine-readable code; the reason is what the card and
# the finding show. An admission reason the map does not know is stored
# verbatim (truncated), never dropped.
_TRIAL_OUTCOME_REASONS = {
    "validation_rejected": "purged validation gates not passed",
    "ordinary_holdout_not_beaten": "sealed holdout Brier delta <= 0.005",
    "screened_for_future_shadow": "passed validation and holdout; screened for shadow",
}

BLOCKED_REPORTS_SCHEMA = """CREATE TABLE IF NOT EXISTS rival_research_blocked_reports (
    venue TEXT NOT NULL,
    asset TEXT,
    requested_months INTEGER NOT NULL,
    missing_months_json TEXT NOT NULL,
    report_sha256 TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (venue, report_sha256))"""


def initialize_blocked_reports(store) -> None:
    """Create the blocked-run receipts table if it does not exist (D17)."""
    with store.connect() as conn:
        conn.execute(BLOCKED_REPORTS_SCHEMA)


def record_blocked_receipt(store, *, venue: str, asset: str | None,
                           requested_months: int, missing_months: list,
                           report_sha256: str,
                           now: datetime | None = None) -> int:
    """Record one blocked offline run, once (spec D17).

    A run that produces zero trials (incomplete months) previously
    recorded nothing anywhere; this receipt is the record. Keyed by
    (venue, report_sha256) with ON CONFLICT DO NOTHING, so a re-delivered
    report is a provable no-op. Returns 1 when newly recorded, 0 on a
    duplicate. ``missing_months`` is the sorted union of the report's
    missing_or_incomplete_months, as "YYYY-MM" strings.
    """
    if not re.fullmatch(r"[a-z0-9_-]{2,24}", str(venue)):
        raise ValueError("venue must be a lowercase slug")
    if not re.fullmatch(r"[0-9a-f]{64}", str(report_sha256)):
        raise ValueError("report_sha256 must be a SHA-256 hex digest")
    if asset is not None and asset not in ASSETS:
        raise ValueError(f"unknown asset: {asset!r}")
    if int(requested_months) < 0:
        raise ValueError("requested_months must be >= 0")
    missing = sorted({str(m) for m in missing_months})
    if not all(re.fullmatch(r"\d{4}-\d{2}", m) for m in missing):
        raise ValueError("missing_months must be YYYY-MM strings")
    stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    with store.connect() as conn:
        conn.execute(BLOCKED_REPORTS_SCHEMA)
        row = conn.execute(
            """INSERT INTO rival_research_blocked_reports
               (venue, asset, requested_months, missing_months_json,
                report_sha256, recorded_at)
               VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
            (venue, asset, int(requested_months),
             json.dumps(missing, separators=(",", ":")), report_sha256, stamp),
        )
        return int(row.rowcount == 1)


def list_blocked_receipts(store, *, since: datetime | None = None) -> list[dict]:
    """Blocked-run receipts, newest first (spec D17/D18).

    The driver consumes these (all of an experiment's venues blocked ->
    close with a finding naming the missing months); the status card
    surfaces the latest per venue as the actual blocker.
    """
    with store.connect() as conn:
        conn.execute(BLOCKED_REPORTS_SCHEMA)
        from parlay_bot.research.source_runs import latest_runs, supersedes_legacy

        runs = latest_runs(conn)
        if since is None:
            rows = conn.execute(
                """SELECT venue, asset, requested_months,
                          missing_months_json, report_sha256, recorded_at
                   FROM rival_research_blocked_reports
                   ORDER BY recorded_at DESC"""
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT venue, asset, requested_months,
                          missing_months_json, report_sha256, recorded_at
                   FROM rival_research_blocked_reports
                   WHERE recorded_at>=? ORDER BY recorded_at DESC""",
                (since.astimezone(UTC).isoformat(),),
            ).fetchall()
    out = []
    for row in rows:
        newer = runs.get((str(row['venue']), str(row['asset'] or '')))
        if supersedes_legacy(newer, dict(row)):
            continue
        try:
            missing = json.loads(row["missing_months_json"] or "[]")
        except (ValueError, TypeError):
            missing = []
        out.append({
            "venue": str(row["venue"]),
            "asset": row["asset"],
            "requested_months": int(row["requested_months"]),
            "missing_months": [str(m) for m in missing if isinstance(m, str)],
            "report_sha256": str(row["report_sha256"]),
            "recorded_at": str(row["recorded_at"]),
        })
    return out


def record_trials(store, trials: list, *, report_sha256: str,
                  now: datetime | None = None) -> int:
    """Save offline experiment results for proposals RivaL actually made.

    Every trial must name a stored proposal (its record SHA) whose asset and
    hypothesis match; anything else is skipped, never invented. Results are
    validation-period Brier scores only (the sealed holdout is not involved).
    """
    if not re.fullmatch(r"[0-9a-f]{64}", str(report_sha256)):
        raise ValueError("report_sha256 must be a SHA-256 hex digest")
    stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    saved = 0
    with store.connect() as conn:
        conn.execute(TRIALS_SCHEMA)
        # Phase 3: a scheduled trial names no LLM proposal, so the
        # lookup below must find an empty hypotheses table, not crash
        # on a missing one (the lab may never have run on this store).
        conn.execute(HYPOTHESES_SCHEMA)
        # Spec D17: outcome columns on rows created before this change.
        for column, definition in (("outcome_status", "TEXT"),
                                   ("outcome_reason", "TEXT")):
            if column not in store.database.column_names(
                    conn, "rival_llm_hypothesis_trials"):
                conn.execute(
                    "ALTER TABLE rival_llm_hypothesis_trials "
                    f"ADD COLUMN {column} {definition}"
                )
        for trial in trials[:200]:
            try:
                sha = str(trial["proposal_sha256"])
                horizon = int(trial["horizon_minutes"])
                base = float(trial["baseline_validation_brier"])
                llm = float(trial["llm_mask_validation_brier"])
                count = int(trial["validation_count"])
                indices = [int(i) for i in trial["feature_indices"]]
                venue, contract = str(trial["venue"]), str(trial["contract"])
            except (KeyError, TypeError, ValueError):
                continue
            if (not re.fullmatch(r"[0-9a-f]{64}", sha) or horizon not in (5, 15, 60)
                    or not all(math.isfinite(x) and 0 <= x <= 1 for x in (base, llm))
                    or count <= 0 or not re.fullmatch(r"[a-z0-9_-]{2,24}", venue)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{2,40}", contract)):
                continue
            # Spec D17: rejection status/reason as data on the trial
            # record, so a rejection is queryable in Postgres instead of
            # living only in the 14-day GHA report JSON. Absent on trials
            # produced without a candidate sink (no screen ran).
            raw_status = trial.get("retrospective_candidate_status")
            if isinstance(raw_status, str) and raw_status:
                outcome_status = raw_status[:120]
                outcome_reason = _TRIAL_OUTCOME_REASONS.get(
                    raw_status, raw_status[:120])
            else:
                outcome_status = None
                outcome_reason = None
            proposal = conn.execute(
                """SELECT asset, hypothesis FROM rival_llm_feature_hypotheses
                   WHERE record_sha256=? AND status='proposed'""", (sha,),
            ).fetchone()
            if (proposal is None or proposal["hypothesis"] != trial.get("hypothesis")
                    or proposal["asset"] != trial.get("asset")):
                continue
            row = conn.execute(
                """INSERT INTO rival_llm_hypothesis_trials
                   (proposal_sha256,venue,contract,horizon_minutes,asset,hypothesis,
                    feature_indices_json,baseline_brier,llm_brier,validation_count,
                    report_sha256,recorded_at,outcome_status,outcome_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                (sha, venue, contract, horizon, proposal["asset"], proposal["hypothesis"],
                 json.dumps(indices, separators=(",", ":")), base, llm, count,
                 report_sha256, stamp, outcome_status, outcome_reason),
            )
            saved += int(row.rowcount == 1)
    return saved


def trial_summary(store, *, asset: str, now: datetime | None = None,
                  days: int = 60) -> list[dict]:
    """Per idea: how many real tests, how many beat the baseline, mean change.

    ``mean_brier_gain`` is baseline minus idea on the validation period:
    positive means the idea scored better. Bounded to 12 ideas.
    """
    since = ((now or datetime.now(UTC)).astimezone(UTC) - timedelta(days=days)).isoformat()
    try:
        with store.connect() as conn:
            conn.execute(TRIALS_SCHEMA)
            rows = conn.execute(
                """SELECT hypothesis, feature_indices_json, COUNT(*) AS tests,
                          SUM(CASE WHEN baseline_brier > llm_brier THEN 1 ELSE 0 END) AS better,
                          AVG(baseline_brier - llm_brier) AS gain
                   FROM rival_llm_hypothesis_trials
                   WHERE asset=? AND recorded_at>=?
                   GROUP BY hypothesis, feature_indices_json
                   ORDER BY COUNT(*) DESC, hypothesis LIMIT 12""",
                (asset, since),
            ).fetchall()
    except Exception:  # noqa: BLE001 - feedback is optional; never blocks proposals
        _LOG.warning("RivaL hypothesis trial summary unavailable", exc_info=True)
        return []
    return [{"hypothesis": str(r["hypothesis"]),
             "feature_indices": json.loads(r["feature_indices_json"]),
             "tests": int(r["tests"]), "better": int(r["better"]),
             "mean_brier_gain": round(float(r["gain"]), 6)} for r in rows]
