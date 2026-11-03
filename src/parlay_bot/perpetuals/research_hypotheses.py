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

from parlay_bot.perpetuals.analysis_evidence import InsufficientEvidence
from parlay_bot.perpetuals.ai_types import ResearchUnavailable

_LOG = logging.getLogger(__name__)
ASSETS = ("BTC", "ETH", "SOL", "XRP")
HYPOTHESES = ("trend_only", "momentum_only", "volatility_only", "volume_only")
MODEL = "LiquidAI/LFM3-1.7B-GGUF:Q4_K_M"


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
                  rationale TEXT,
                  falsification TEXT,
                  source_venues_json TEXT,
                  record_sha256 TEXT,
                  status TEXT NOT NULL,
                  PRIMARY KEY(asset,slot_utc)
                )"""
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
        digest = hashlib.sha256(
            json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.store.connect() as conn:
            conn.execute(
                """UPDATE rival_llm_feature_hypotheses
                   SET evidence_sha256=?, model=?, hypothesis=?,
                       rationale=?, falsification=?, source_venues_json=?,
                       record_sha256=?,status='proposed'
                   WHERE asset=? AND slot_utc=? AND status='attempted'""",
                (result["evidence_sha256"], MODEL, result["hypothesis"],
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
            """SELECT asset,slot_utc,model,hypothesis,evidence_sha256,
                      source_venues_json,record_sha256
               FROM rival_llm_feature_hypotheses
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
