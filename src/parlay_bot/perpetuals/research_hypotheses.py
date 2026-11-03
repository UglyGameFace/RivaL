"""Bounded LFM3-generated feature hypotheses, never trading signals.

The live bot records at most one research attempt per six-hour UTC slot.
The six-hour rotation visits BTC, ETH, SOL, and XRP every day without using
the 5-minute prediction worker or treating LLM text as training labels.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.ai_types import ResearchUnavailable
from parlay_bot.perpetuals.analysis_evidence import InsufficientEvidence

_LOG = logging.getLogger(__name__)
ASSETS = ("BTC", "ETH", "SOL", "XRP")
HYPOTHESES = ("trend_only", "momentum_only", "volatility_only", "volume_only",
              "feature_subset_v2")
MODEL = "LiquidAI/LFM3-1.7B-GGUF:Q4_K_M"


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


class FeatureHypothesisLab:
    """Persistent, CPU-bounded LFM3 experiments independent of issuance."""

    def __init__(self, store, ai, *, venues: tuple[str, ...]):
        self.store, self.ai, self.venues = store, ai, venues
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        with self.store.connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS rival_llm_feature_hypotheses (
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
            )
            if "attempt_count" not in self.store.database.column_names(
                conn, "rival_llm_feature_hypotheses",
            ):
                conn.execute(
                    "ALTER TABLE rival_llm_feature_hypotheses "
                    "ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 1"
                )
            if "feature_indices_json" not in self.store.database.column_names(
                conn, "rival_llm_feature_hypotheses",
            ):
                conn.execute(
                    "ALTER TABLE rival_llm_feature_hypotheses "
                    "ADD COLUMN feature_indices_json TEXT"
                )
        self._initialized = True

    async def run_once(self, *, now: datetime | None = None) -> dict:
        stamp = now or datetime.now(UTC)
        if stamp.utcoffset() is None:
            raise ValueError("UTC time must be timezone aware")
        stamp = stamp.astimezone(UTC)
        slot = stamp.replace(hour=stamp.hour // 6 * 6, minute=0,
                             second=0, microsecond=0)
        asset = ASSETS[slot.hour // 6]
        await asyncio.to_thread(self.initialize)
        with self.store.connect() as conn:
            claimed = conn.execute(
                """INSERT INTO rival_llm_feature_hypotheses
                   (asset,slot_utc,attempted_at,status)
                   VALUES (?,?,?,?)
                   ON CONFLICT DO NOTHING""",
                (asset, slot.isoformat(), stamp.isoformat(), "attempted"),
            )
        if claimed.rowcount != 1:
            # A temporary LFM3 outage or unavailable market source should
            # not lose a whole six-hour slot. Reclaim atomically at most twice,
            # no sooner than twenty minutes after the previous attempt.
            cutoff = (stamp - timedelta(minutes=20)).isoformat()
            with self.store.connect() as conn:
                retry = conn.execute(
                    """UPDATE rival_llm_feature_hypotheses
                       SET attempted_at=?,status='attempted',
                           attempt_count=attempt_count+1
                       WHERE asset=? AND slot_utc=? AND attempt_count<3
                         AND attempted_at<=?
                         AND status IN ('attempted','unavailable',
                                        'local_research_not_configured')""",
                    (stamp.isoformat(), asset, slot.isoformat(), cutoff),
                )
            if retry.rowcount != 1:
                return {"status": "already_attempted", "asset": asset}
        if self.ai is None or not self.ai.available:
            status = "local_research_not_configured"
            result = None
        else:
            try:
                result = await self.ai.propose_feature_hypothesis(
                    asset=asset, venues=self.venues,
                )
                status = "proposed"
            except (InsufficientEvidence, ResearchUnavailable, ValueError):
                result, status = None, "unavailable"
        if result is None:
            with self.store.connect() as conn:
                conn.execute(
                    """UPDATE rival_llm_feature_hypotheses SET status=?
                       WHERE asset=? AND slot_utc=? AND status='attempted'""",
                    (status, asset, slot.isoformat()),
                )
            return {"status": status, "asset": asset}
        # All fields and the exact evidence SHA are checked before storage.
        if (
            result.get("asset") != asset
            or result.get("model") != MODEL
            or result.get("hypothesis") not in HYPOTHESES
            or (not valid_feature_indices(result.get("feature_indices"))
                if result.get("hypothesis") == "feature_subset_v2"
                else result.get("feature_indices") is not None)
            or not isinstance(result.get("evidence_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", result["evidence_sha256"])
            or not isinstance(result.get("venues"), list)
            or not all(v in self.venues for v in result["venues"])
            or not result["venues"]
            or result.get("research_only") is not True
            or any(not isinstance(result.get(k), str)
                   or not 5 <= len(result[k].strip()) <= 120
                   for k in ("rationale", "falsification"))
        ):
            raise ValueError("invalid LFM3 hypothesis receipt")
        record = {
            "asset": asset, "slot_utc": slot.isoformat(),
            "model": MODEL, "evidence_sha256": result["evidence_sha256"],
            "hypothesis": result["hypothesis"], "venues": result["venues"],
        }
        if result["hypothesis"] == "feature_subset_v2":
            record["feature_indices"] = result["feature_indices"]
        digest = hashlib.sha256(
            json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE rival_llm_feature_hypotheses
                   SET evidence_sha256=?, model=?, hypothesis=?,
                       feature_indices_json=?, rationale=?, falsification=?,
                       source_venues_json=?,record_sha256=?,status='proposed'
                   WHERE asset=? AND slot_utc=? AND status='attempted'""",
                (result["evidence_sha256"], MODEL, result["hypothesis"],
                 json.dumps(result["feature_indices"], separators=(",", ":"))
                 if result["hypothesis"] == "feature_subset_v2" else None,
                 result["rationale"].strip(), result["falsification"].strip(),
                 json.dumps(result["venues"], separators=(",", ":")),
                 digest, asset, slot.isoformat()),
            )
        _LOG.info("RivaL Intelligence suggested bounded feature experiment asset=%s",
                  asset)
        return {"status": "proposed", "asset": asset,
                "hypothesis": result["hypothesis"], "record_sha256": digest}


def recent_hypotheses(store, *, venue: str, now: datetime | None = None) -> list[dict]:
    """Read bounded, genuine prior proposals for one exact source venue."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    with store.connect() as conn:
        rows = conn.execute(
            """SELECT * FROM rival_llm_feature_hypotheses
               WHERE status='proposed' AND attempted_at>=?
               ORDER BY attempted_at DESC LIMIT 96""",
            ((current - timedelta(days=7)).isoformat(),),
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
            if not isinstance(venues, list) or venue not in venues:
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
