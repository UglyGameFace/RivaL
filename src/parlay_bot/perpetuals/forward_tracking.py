"""Persistent experiments on the NEXT genuine source-specific forward predictions.

A cohort starts at a persisted UTC timestamp. Only predictions first issued
after that time and for the frozen venue/instrument/model/horizon may enter it.
No candle backfill, old outcome, or other exchange can earn credit. These are
observational forecast scores, NOT proof of live trading profit.
"""
from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta

GOALS = (25, 50, 100, 150, 300, 500)
MODEL = "prospective-logistic-persistent-v3"


def _clock(now: datetime | None) -> datetime:
    stamp = now or datetime.now(UTC)
    if stamp.utcoffset() is None:
        raise ValueError("tracking requires a timezone-aware clock")
    return stamp.astimezone(UTC)


class TrackingUnavailable(RuntimeError):
    """No trustworthy, specific future forecast stream is available."""


class ForwardTracking:
    """Read-only evaluation of immutable future forecasts, except session setup."""

    def __init__(self, store):
        self.store = store

    def initialize(self):
        with self.store.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS rival_forward_tracking_sessions (
                    session_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    quote TEXT NOT NULL,
                    horizon_minutes INTEGER NOT NULL,
                    model_version TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    target_count INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_rival_forward_tracking_owner
                  ON rival_forward_tracking_sessions
                  (owner_id,asset,venue,started_at);
            """)

    def _current(self, conn, *, owner_id, asset, venue, horizon):
        return conn.execute(
            """SELECT * FROM rival_forward_tracking_sessions
               WHERE owner_id=? AND asset=? AND venue=? AND horizon_minutes=?
               ORDER BY started_at DESC,session_id DESC LIMIT 1""",
            (str(owner_id), asset, venue, horizon),
        ).fetchone()

    @staticmethod
    def _batch(conn, session):
        """First N issued in the cohort, NEVER select only winning/settled rows."""
        return conn.execute(
            """SELECT f.issued_at,f.target_opened_at,f.settled_at,f.outcome_up,
                      f.probability_up,f.baseline_up,f.model_brier,
                      f.baseline_brier,f.reference_opened_at,
                      l.weights_sha256,l.historical_seed_sha256
               FROM perp_forward_audit f
               LEFT JOIN rival_forward_model_lineage l
                 ON l.venue=f.venue AND l.contract=f.contract AND l.quote=f.quote
                AND l.horizon_minutes=f.horizon_minutes
                AND l.model_version=f.model_version
                AND l.reference_opened_at=f.reference_opened_at
                AND l.issued_at=f.issued_at
               WHERE f.asset=? AND f.venue=? AND f.contract=? AND f.quote=?
                 AND f.horizon_minutes=? AND f.model_version=?
                 AND f.issued_at>?
               ORDER BY f.issued_at,f.reference_opened_at
               LIMIT ?""",
            (session["asset"], session["venue"], session["contract"],
             session["quote"], session["horizon_minutes"],
             session["model_version"], session["started_at"],
             session["target_count"]),
        ).fetchall()

    def start(self, *, owner_id: int, asset: str, venue: str, horizon: int,
              target: int, now: datetime | None = None) -> dict:
        """Start or reuse a running cohort; only a finished cohort can restart."""
        if asset not in ("BTC", "ETH", "SOL", "XRP") or not venue or horizon not in (5, 15, 60):
            raise ValueError("unsupported tracking source")
        if target not in GOALS:
            raise ValueError("unsupported future sample goal")
        stamp = _clock(now)
        self.initialize()
        with self.store.connect() as conn:
            existing = self._current(conn, owner_id=owner_id, asset=asset,
                                     venue=venue, horizon=horizon)
            if existing is not None:
                current = self._evaluate(existing, self._batch(conn, existing), stamp)
                if not current["complete"]:
                    return {"status": "already_tracking",
                            "session_id": existing["session_id"],
                            "target": int(existing["target_count"])}
            latest = conn.execute(
                """SELECT contract,quote,model_version
                   FROM perp_forward_audit
                   WHERE asset=? AND venue=? AND horizon_minutes=?
                     AND issued_at<=? AND model_version=?
                   ORDER BY issued_at DESC LIMIT 1""",
                (asset, venue, horizon, stamp.isoformat(), MODEL),
            ).fetchone()
            if latest is None:
                raise TrackingUnavailable(
                    "No existing forward forecast stream for this exchange and timeframe"
                )
            session_id = uuid.uuid4().hex
            conn.execute(
                """INSERT INTO rival_forward_tracking_sessions
                   (session_id,owner_id,asset,venue,contract,quote,horizon_minutes,
                    model_version,started_at,target_count)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (session_id, str(owner_id), asset, venue,
                 latest["contract"], latest["quote"], horizon,
                 latest["model_version"], stamp.isoformat(), target),
            )
        return {"status": "started", "session_id": session_id, "target": target}

    def report(self, *, owner_id: int, asset: str, venue: str,
               now: datetime | None = None) -> dict:
        """Return each current timeframe cohort without changing original forecasts."""
        stamp = _clock(now)
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM rival_forward_tracking_sessions
                   WHERE owner_id=? AND asset=? AND venue=?
                   ORDER BY started_at DESC LIMIT 60""",
                (str(owner_id), asset, venue),
            ).fetchall()
            by_horizon = {}
            for session in rows:
                horizon = int(session["horizon_minutes"])
                if horizon in by_horizon:
                    continue
                by_horizon[horizon] = self._evaluate(session,
                                                    self._batch(conn, session), stamp)
        return {"status": "tracking" if by_horizon else "not_started",
                "venue": venue, "asset": asset,
                "cohorts": [by_horizon[key] for key in sorted(by_horizon)]}

    @staticmethod
    def _evaluate(session, batch, now: datetime) -> dict:
        target = int(session["target_count"])
        seen = set()
        scores = []
        missing_lineage = 0
        corrupted = 0
        pending = 0
        used_history = 0
        fingerprints = set()
        early_issued = len(batch)
        for record in batch:
            try:
                issued = datetime.fromisoformat(str(record["issued_at"]))
                close = datetime.fromisoformat(str(record["target_opened_at"]))
                if (issued.utcoffset() is None or close.utcoffset() is None
                        or issued <= datetime.fromisoformat(session["started_at"])
                        or issued >= close + timedelta(minutes=5)
                        or str(record["target_opened_at"]) in seen):
                    raise ValueError("invalid or duplicated future prediction")
                seen.add(str(record["target_opened_at"]))
                if record["settled_at"] is None:
                    pending += 1
                    continue
                if record["outcome_up"] is None and record["model_brier"] is None:
                    continue  # no trades in the target window: nothing to grade
                settled = datetime.fromisoformat(str(record["settled_at"]))
                y = int(record["outcome_up"])
                p = float(record["probability_up"])
                b = float(record["baseline_up"])
                loss = float(record["model_brier"])
                reference = float(record["baseline_brier"])
                if (settled.utcoffset() is None or settled > now or
                        settled <= close + timedelta(minutes=5)
                        or y not in (0, 1) or not all(
                            math.isfinite(v) and 0 <= v <= 1
                            for v in (p, b, loss, reference)
                        ) or abs(loss - (p-y)**2) > 1e-8
                        or abs(reference - (b-y)**2) > 1e-8):
                    raise ValueError("invalid independently settled score")
                if not record["weights_sha256"]:
                    missing_lineage += 1
                else:
                    fingerprints.add(str(record["weights_sha256"]))
                if record["historical_seed_sha256"]:
                    used_history += 1
                scores.append((str(record["issued_at"]), p, b, y, loss, reference))
            except (ValueError, TypeError, OverflowError, KeyError):
                corrupted += 1
        score_count = len(scores)
        model_brier = (sum(r[4] for r in scores) / score_count
                       if score_count else None)
        baseline_brier = (sum(r[5] for r in scores) / score_count
                          if score_count else None)
        # Trends use the preselected first and second HALF of issued forecasts.
        # A model cannot cherry-pick only its most recent successful windows.
        half = target // 2
        early = list(batch)[:half]
        late = list(batch)[half:target]
        def _phase(phase):
            lookup = {r[0]: r for r in scores}
            current = [lookup[str(r["issued_at"])] for r in phase
                       if str(r["issued_at"]) in lookup]
            if len(current) != len(phase) or len(current) < 10:
                return None
            return sum(r[5] - r[4] for r in current) / len(current)
        early_gain = _phase(early) if len(early) == half else None
        later_gain = _phase(late) if len(late) == target-half else None
        completed = (early_issued == target and score_count == target
                     and not corrupted and not missing_lineage)
        # An improving observational score is not a causal demonstration of
        # continual learning. Do not claim a win from the two periods alone.
        trend = ("later_period_higher_paired_gain"
                 if completed and early_gain is not None
                 and later_gain is not None and later_gain > early_gain
                 else "not_demonstrated")
        correct = sum((p > .5 and y == 1) or (p < .5 and y == 0)
                      for _, p, _, y, _, _ in scores)
        return {
            "session_id": str(session["session_id"]),
            "horizon": int(session["horizon_minutes"]),
            "contract": str(session["contract"]), "quote": str(session["quote"]),
            "started_at": str(session["started_at"]), "target": target,
            "issued": early_issued, "settled": score_count,
            "pending": pending, "invalid": corrupted,
            "unverified_lineage": missing_lineage,
            "correct": correct, "hit_rate": correct / score_count if score_count else None,
            "model_brier": model_brier, "baseline_brier": baseline_brier,
            "paired_gain": baseline_brier-model_brier if score_count else None,
            "early_gain": early_gain, "later_gain": later_gain,
            "trend": trend, "complete": completed,
            "verified_historical_seed_count": used_history,
            "weight_fingerprints": len(fingerprints),
            "real_profit_verified": False,
        }


def tracking_text(result: dict) -> str:
    if not result["cohorts"]:
        return ("No tracking session yet. Pick an exchange under "
                "**Is any model proven better?**, then return here and "
                "choose how many NEW predictions to follow.")
    lines = []
    for item in result["cohorts"]:
        line = (f"**{item['horizon']}m · {item['contract']}** · "
                f"{item['issued']}/{item['target']} future predictions issued, "
                f"{item['settled']} scored")
        if item["settled"]:
            line += (
                f"\nDirection right: {item['correct']}/{item['settled']} "
                f"({item['hit_rate']:.1%}). Brier: {item['model_brier']:.4f} "
                f"vs baseline {item['baseline_brier']:.4f} (lower wins)."
            )
        if item["settled"]:
            line += (
                f"\nModel weight snapshots: {item['weight_fingerprints']}; "
                f"historical seeds used: {item['verified_historical_seed_count']}/"
                f"{item['settled']}."
            )
        if item["invalid"] or item["unverified_lineage"]:
            line += (f"\nEvidence issues: {item['invalid']} invalid, "
                     f"{item['unverified_lineage']} missing model fingerprints.")
        if item["complete"] and item["early_gain"] is not None and item["later_gain"] is not None:
            line += (
                f"\nCompared with baseline: earlier {item['early_gain']:+.4f}, "
                f"later {item['later_gain']:+.4f} Brier gain (higher is better)."
            )
        if item["complete"]:
            if item["trend"] == "later_period_higher_paired_gain":
                line += ("\nLater predictions beat earlier ones RELATIVE TO "
                         "their respective baselines. Not proof learning caused it.")
            else:
                line += "\nNo verified improvement trend versus the baseline."
        else:
            line += "\nTracking continues as the normal forecast worker runs."
        lines.append(line)
    return ("\n".join(lines) +
            "\n**No real trades or verified after-cost profit.** "
            "Full promotion still requires 21 days, quote proof and stress tests.")[:1024]
