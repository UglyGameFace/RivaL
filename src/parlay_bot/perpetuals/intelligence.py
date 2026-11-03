"""RivaL Intelligence: reviews what RivaL's prediction models predict.

The prediction models make every call. RivaL Intelligence (the private LFM3
model) reads those calls, how many exchanges agree, each model's graded
record and the market evidence, then decides per timeframe whether to
"follow" the models' call or "abstain", and explains why.

It never invents a direction: a "follow" takes the models' consensus
direction, and only when the consensus is clear. Every stance is stored
against the exact model forecasts of that 5-minute slot, so the bot can
grade whether following RivaL Intelligence beats following the raw model.
"""
from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.forward_audit import MODEL as LIVE_MODEL
from parlay_bot.perpetuals.forward_audit import ForwardAccuracyMonitor

_LOG = logging.getLogger(__name__)
HORIZONS = (5, 15, 60)
# A "follow" needs a clear model consensus: mean probability at least this
# far from 50% and most exchanges on the same side.
CONSENSUS_MARGIN = 0.02
CONSENSUS_SHARE = 2 / 3


def utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.utcoffset() is not None else value.replace(tzinfo=UTC)


def model_calls(market, *, asset: str, now: datetime | None = None,
                summary: dict | None = None) -> dict:
    """Latest-slot calls of every live and challenger model, with records."""
    stamp = utc(now or datetime.now(UTC))
    since = (stamp - timedelta(minutes=12)).isoformat()
    with market.store.connect() as conn:
        rows = conn.execute(
            """SELECT horizon_minutes, target_opened_at, probability_up
               FROM perp_forward_audit
               WHERE asset=? AND model_version=? AND issued_at>=? AND issued_at<=?""",
            (asset, LIVE_MODEL, since, stamp.isoformat()),
        ).fetchall()
        try:
            challengers = conn.execute(
                """SELECT horizon_minutes, family, target_opened_at, probability_up
                   FROM rival_model_shadow_forecasts
                   WHERE asset=? AND family LIKE 'pooled_%' AND issued_at>=?
                     AND issued_at<=?""",
                (asset, since, stamp.isoformat()),
            ).fetchall()
        except Exception:  # noqa: BLE001 - optional challenger table
            challengers = []
    if summary is None:
        try:
            summary = ForwardAccuracyMonitor(market).summary(asset=asset, now=stamp)
        except Exception:  # noqa: BLE001 - calls are still useful without a record
            _LOG.warning("RivaL Intelligence model record unavailable")
            summary = {}
    out: dict[str, dict] = {}
    for horizon in HORIZONS:
        entry: dict[str, dict] = {}
        live = _latest_slot([r for r in rows if int(r["horizon_minutes"]) == horizon])
        if live is not None:
            part = (summary.get("last_7d") or {}).get(horizon) or {}
            entry["live"] = {**live, "record": {
                "hit_rate": part.get("hit_rate"), "graded": int(part.get("scored") or 0),
                "beats_basic": part.get("model_beats_baseline") is True,
            }}
        families = {str(r["family"]) for r in challengers if int(r["horizon_minutes"]) == horizon}
        for family in sorted(families):
            call = _latest_slot([r for r in challengers if int(r["horizon_minutes"]) == horizon
                                 and str(r["family"]) == family])
            if call is not None:
                entry[_short(family)] = {**call, "record": {}}
        if entry:
            out[str(horizon)] = entry
    return out


def _short(family: str) -> str:
    return "".join(ch for ch in family.lower() if ch.isalnum() or ch == "_")[:32]


def _latest_slot(rows) -> dict | None:
    if not rows:
        return None
    slot = max(str(r["target_opened_at"]) for r in rows)
    probs = [float(r["probability_up"]) for r in rows if str(r["target_opened_at"]) == slot]
    probs = [p for p in probs if math.isfinite(p) and 0 <= p <= 1]
    if not probs:
        return None
    return {"slot": slot, "p_up": round(sum(probs) / len(probs), 4),
            "venues": len(probs), "venues_up": sum(p > 0.5 for p in probs)}


def consensus_direction(call: dict | None) -> str | None:
    """'up'/'down' only when the models clearly agree; otherwise None."""
    if not call or not call.get("venues"):
        return None
    p, venues, up = float(call["p_up"]), int(call["venues"]), int(call["venues_up"])
    if p >= 0.5 + CONSENSUS_MARGIN and up >= CONSENSUS_SHARE * venues:
        return "up"
    if p <= 0.5 - CONSENSUS_MARGIN and (venues - up) >= CONSENSUS_SHARE * venues:
        return "down"
    return None


class IntelligenceLedger:
    """Stores every RivaL Intelligence review and grades it later."""

    def __init__(self, store) -> None:
        self.store = store
        self._ready = False

    def initialize(self) -> None:
        if self._ready:
            return
        with self.store.connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS rival_intelligence_calls (
                    asset TEXT NOT NULL, horizon_minutes INTEGER NOT NULL,
                    target_opened_at TEXT NOT NULL, issued_at TEXT NOT NULL,
                    model TEXT NOT NULL, stance TEXT NOT NULL,
                    direction TEXT NOT NULL, consensus_up DOUBLE PRECISION NOT NULL,
                    venues INTEGER NOT NULL, venues_up INTEGER NOT NULL,
                    thesis TEXT NOT NULL, risk TEXT NOT NULL,
                    PRIMARY KEY (asset, horizon_minutes, target_opened_at)
                )"""
            )
        self._ready = True

    def record(self, *, asset: str, calls: dict, review: dict, model: str,
               issued_at: datetime) -> int:
        """Persist one stance per timeframe that had a live model call."""
        self.initialize()
        saved = 0
        with self.store.connect() as conn:
            for horizon in HORIZONS:
                live = (calls.get(str(horizon)) or {}).get("live")
                stance = review.get(f"stance_{horizon}m")
                if live is None or stance not in ("follow", "abstain"):
                    continue
                # Too late to count: the slot's result window already opened.
                if utc(issued_at) >= utc(datetime.fromisoformat(live["slot"])) + timedelta(minutes=5):
                    continue
                consensus = consensus_direction(live)
                direction = consensus if stance == "follow" and consensus else "none"
                row = conn.execute(
                    """INSERT INTO rival_intelligence_calls
                       (asset,horizon_minutes,target_opened_at,issued_at,model,stance,
                        direction,consensus_up,venues,venues_up,thesis,risk)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                    (asset, horizon, live["slot"], utc(issued_at).isoformat(), model,
                     stance, direction, float(live["p_up"]), int(live["venues"]),
                     int(live["venues_up"]), str(review.get("thesis", ""))[:240],
                     str(review.get("risk", ""))[:160]),
                )
                saved += int(row.rowcount == 1)
        return saved

    def latest(self, *, asset: str) -> dict | None:
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM rival_intelligence_calls WHERE asset=?
                   ORDER BY issued_at DESC LIMIT 3""", (asset,),
            ).fetchall()
        if not rows:
            return None
        return {"issued_at": str(rows[0]["issued_at"]), "thesis": str(rows[0]["thesis"]),
                "risk": str(rows[0]["risk"]),
                "stances": {int(r["horizon_minutes"]): (str(r["stance"]), str(r["direction"]))
                            for r in rows}}

    def report(self, *, asset: str, horizon: int, now: datetime | None = None) -> dict:
        """Grade 'follow' vs 'abstain' on the live model's actual outcomes."""
        self.initialize()
        stamp = utc(now or datetime.now(UTC))
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT c.stance, c.direction, c.target_opened_at,
                          AVG(f.outcome_up) AS up_rate,
                          AVG(CASE WHEN (f.probability_up>0.5 AND f.outcome_up=1)
                                    OR (f.probability_up<0.5 AND f.outcome_up=0)
                               THEN 1.0 ELSE 0.0 END) AS model_hit,
                          COUNT(*) AS n
                   FROM rival_intelligence_calls c
                   JOIN perp_forward_audit f
                     ON f.asset=c.asset AND f.horizon_minutes=c.horizon_minutes
                    AND f.target_opened_at=c.target_opened_at AND f.model_version=?
                    AND f.outcome_up IN (0,1) AND f.probability_up<>0.5
                   WHERE c.asset=? AND c.horizon_minutes=? AND c.issued_at>=?
                   GROUP BY c.stance, c.direction, c.target_opened_at""",
                (LIVE_MODEL, asset, horizon, (stamp - timedelta(days=7)).isoformat()),
            ).fetchall()
        followed = [r for r in rows if r["stance"] == "follow" and r["direction"] != "none"]
        abstained = [r for r in rows if r["stance"] == "abstain"]

        def call_hit(r) -> float:
            # Majority outcome across exchanges for that slot decides the call.
            up = float(r["up_rate"]) > 0.5
            return 1.0 if (r["direction"] == "up") == up else 0.0

        return {
            "followed": len(followed),
            "followed_hit_rate": (sum(call_hit(r) for r in followed) / len(followed)
                                  if followed else None),
            "abstained": len(abstained),
            "model_hit_when_abstained": (sum(float(r["model_hit"]) for r in abstained)
                                         / len(abstained) if abstained else None),
            "model_hit_overall": (sum(float(r["model_hit"]) for r in rows) / len(rows)
                                  if rows else None),
            "slots_graded": len(rows),
        }
