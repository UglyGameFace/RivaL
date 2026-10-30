"""Source-matched historical challengers, actual forward selection and rollback.

Candidates receive their own persistent online state and frozen predictions.
Retrospective passing only admits a challenger. Research serving needs paired
live improvement; existing separate quote/cost gates still govern any trade call.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.governance import score_paired_forward
from parlay_bot.perpetuals.learning import _sigmoid
from parlay_bot.perpetuals.model_checkpoints import canonical
from parlay_bot.research.candidate_registry import ResearchCandidateRegistry


class HistoricalChallengers:
    def __init__(self, store, checkpoints):
        self.store = store
        self.checkpoints = checkpoints
        self.registry = ResearchCandidateRegistry(store)
        self._initialized = False
        self._settled_at = None

    def initialize(self):
        if self._initialized:
            return
        self.registry.initialize()
        with self.store.connect() as conn:
            conn.executescript("""CREATE TABLE IF NOT EXISTS rival_history_forward (
                model_sha256 TEXT NOT NULL,venue TEXT NOT NULL,contract TEXT NOT NULL,
                quote TEXT NOT NULL,horizon_minutes INTEGER NOT NULL,
                reference_opened_at TEXT NOT NULL,reference_close DOUBLE PRECISION NOT NULL,
                issued_at TEXT NOT NULL,target_opened_at TEXT NOT NULL,
                probability_up DOUBLE PRECISION NOT NULL,incumbent_up DOUBLE PRECISION NOT NULL,
                weights_sha256 TEXT NOT NULL,outcome_up INTEGER,settled_at TEXT,
                PRIMARY KEY(model_sha256,venue,contract,quote,horizon_minutes,reference_opened_at)
            );
            CREATE INDEX IF NOT EXISTS idx_rival_history_pending
                ON rival_history_forward(settled_at,target_opened_at);
            CREATE TABLE IF NOT EXISTS rival_history_selection (
                venue TEXT NOT NULL,contract TEXT NOT NULL,quote TEXT NOT NULL,
                horizon_minutes INTEGER NOT NULL,evaluated_at TEXT NOT NULL,
                selected_sha256 TEXT,reason TEXT NOT NULL,evidence_json TEXT NOT NULL,
                PRIMARY KEY(venue,contract,quote,horizon_minutes)
            );""")
        self._initialized = True

    def _settle(self, now):
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT f.*,c.close_price,c.first_observed_at FROM rival_history_forward f "
                "JOIN perp_market_candles c ON c.venue=f.venue AND c.contract=f.contract "
                "AND c.quote=f.quote AND c.interval_minutes=5 AND c.opened_at=f.target_opened_at "
                "WHERE f.settled_at IS NULL AND c.closed_at<? "
                "AND c.first_observed_at>f.issued_at AND c.first_observed_at<=? "
                "AND f.target_opened_at>=? ORDER BY f.target_opened_at LIMIT 120",
                (now.isoformat(), now.isoformat(), (now-timedelta(days=30)).isoformat()),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE rival_history_forward SET outcome_up=?,settled_at=? WHERE "
                    "model_sha256=? AND venue=? AND contract=? AND quote=? AND horizon_minutes=? "
                    "AND reference_opened_at=? AND settled_at IS NULL",
                    (int(row["close_price"] > row["reference_close"]), row["first_observed_at"],
                     row["model_sha256"], row["venue"], row["contract"], row["quote"],
                     row["horizon_minutes"], row["reference_opened_at"]),
                )

    def choose(self, samples, features, last, *, horizon, now, incumbent, wall_clock=False):
        """Return a screened research model; absent/failed proof returns incumbent."""
        self.initialize()
        if self._settled_at is None or now-self._settled_at >= timedelta(minutes=5):
            self._settle(now)
            self._settled_at = now
        scope = (last.venue, last.contract, last.quote, horizon)
        candidates = self.registry.compatible_candidates(
            venue=last.venue, contract=last.contract, quote=last.quote, horizon=horizon, now=now,
        )
        selected, selected_sha, selected_gain = incumbent, None, 0.
        evidence = []
        for candidate in candidates:
            state = self.checkpoints.fit(samples, as_of=now, seed=candidate)
            probability = _sigmoid(sum(w*x for w, x in zip(state.final_weights, (1., *features))))
            incumbent_p = _sigmoid(sum(w*x for w, x in zip(incumbent.final_weights, (1., *features))))
            target = last.opened_at + timedelta(minutes=horizon)
            weight_sha = hashlib.sha256(canonical(state.final_weights).encode()).hexdigest()
            issued = datetime.now(UTC) if wall_clock else now
            with self.store.connect() as conn:
                if issued < target+timedelta(minutes=5, milliseconds=-1):
                    conn.execute(
                        "INSERT INTO rival_history_forward "
                        "(model_sha256,venue,contract,quote,horizon_minutes,reference_opened_at,"
                        "reference_close,issued_at,target_opened_at,probability_up,incumbent_up,weights_sha256) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                        (candidate["model_sha256"], *scope, last.opened_at.isoformat(), last.close_price,
                         issued.isoformat(), target.isoformat(), probability, incumbent_p, weight_sha),
                    )
                rows = conn.execute(
                    "SELECT target_opened_at,probability_up,incumbent_up,outcome_up "
                    "FROM rival_history_forward WHERE model_sha256=? AND venue=? AND contract=? "
                    "AND quote=? AND horizon_minutes=? AND outcome_up IS NOT NULL AND settled_at<=? "
                    "AND issued_at>=? AND issued_at<=? ORDER BY target_opened_at",
                    (candidate["model_sha256"], *scope, now.isoformat(),
                     (now-timedelta(days=14)).isoformat(), now.isoformat()),
                ).fetchall()
            # Horizon forecasts share overlapping bars. Count only UTC targets
            # aligned to that horizon when selecting a model.
            paired = [r for r in rows if int(datetime.fromisoformat(r["target_opened_at"]).timestamp()) % (60*horizon) == 0]
            scored = score_paired_forward([
                (r["target_opened_at"], (r["probability_up"]-r["outcome_up"])**2,
                 (r["incumbent_up"]-r["outcome_up"])**2) for r in paired
            ])
            bins = [[] for _ in range(10)]
            for row in paired:
                bins[min(9, int(row["probability_up"]*10))].append(row)
            ece = sum(len(b)/len(paired)*abs(
                sum(r["probability_up"] for r in b)/len(b) -
                sum(r["outcome_up"] for r in b)/len(b)) for b in bins if b) if paired else None
            qualifies = scored.status == "qualified_shadow" and ece is not None and ece <= .10
            evidence.append({"model_sha256": candidate["model_sha256"],
                             "targets": scored.targets, "days": scored.days,
                             "paired_gain": scored.paired_gain, "ece_10": ece,
                             "status": "selected_research" if qualifies else scored.status})
            if qualifies and scored.paired_gain > selected_gain:
                selected, selected_sha, selected_gain = state, candidate["model_sha256"], scored.paired_gain
        reason = "Paired forward research winner" if selected_sha else "Incumbent retained; challenger proof insufficient or failed"
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO rival_history_selection "
                "(venue,contract,quote,horizon_minutes,evaluated_at,selected_sha256,reason,evidence_json) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(venue,contract,quote,horizon_minutes) DO UPDATE SET "
                "evaluated_at=excluded.evaluated_at,selected_sha256=excluded.selected_sha256,"
                "reason=excluded.reason,evidence_json=excluded.evidence_json",
                (*scope, now.isoformat(), selected_sha, reason, json.dumps(evidence)),
            )
        return selected
