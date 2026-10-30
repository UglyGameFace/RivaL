"""Persistent, checksum-verified checkpoints for the current live learner.

An exact training input/feature/target identity reuses its saved weights after
restart. Changed or newly matured inputs are fitted again; retrospective
candidate weights never enter this incumbent checkpoint by implication.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
from dataclasses import asdict
from datetime import datetime

from parlay_bot.perpetuals.fusion import utc
from parlay_bot.perpetuals.learning import WalkForwardResult, walk_forward

FEATURE_CONTRACT = "relative-rsi-macd-bollinger-atr-volume-40x5m-v1"
TARGET_CONTRACT = "future-5m-close-above-reference-close-with-horizon-delay-v1"


class LiveModelCheckpoints:
    def __init__(self, store):
        self.store = store
        self._initialized = False
        self._lock = threading.Lock()
        self._loaded = 0
        self._fitted = 0

    def initialize(self):
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            with self.store.connect() as conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS rival_live_model_checkpoints (
                    venue TEXT NOT NULL, contract TEXT NOT NULL, quote TEXT NOT NULL,
                    horizon_minutes INTEGER NOT NULL, feature_contract TEXT NOT NULL,
                    target_contract TEXT NOT NULL, training_digest TEXT NOT NULL,
                    trained_at TEXT NOT NULL, weights_sha256 TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    PRIMARY KEY(venue,contract,quote,horizon_minutes,feature_contract,target_contract)
                )""")
            self._initialized = True

    @staticmethod
    def _digest(samples, now):
        current = utc(now)
        rows = [{**asdict(s), "issued_at": utc(s.issued_at).isoformat(),
                 "label_known_at": utc(s.label_known_at).isoformat(),
                 "available": utc(s.label_known_at) <= current,
                 "issued": utc(s.issued_at) <= current} for s in samples]
        return hashlib.sha256(json.dumps(
            {"features": FEATURE_CONTRACT, "target": TARGET_CONTRACT,
             "hyperparameters": [20, 50, .025, .0001], "samples": rows},
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode()).hexdigest()

    def fit(self, samples, *, as_of: datetime) -> WalkForwardResult:
        if not samples:
            return walk_forward(samples, as_of=as_of, minimum_evaluated=50)
        self.initialize()
        first = samples[0]
        identity = (first.venue, first.contract, first.quote, first.horizon_minutes,
                    FEATURE_CONTRACT, TARGET_CONTRACT)
        digest = self._digest(samples, as_of)
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM rival_live_model_checkpoints "
                "WHERE venue=? AND contract=? AND quote=? AND horizon_minutes=? "
                "AND feature_contract=? AND target_contract=?", identity,
            ).fetchone()
        if row is not None and row["training_digest"] == digest and (
            datetime.fromisoformat(row["trained_at"]) <= utc(as_of)
        ):
            state = json.loads(row["state_json"])
            weights = state.get("final_weights")
            encoded = json.dumps(weights, separators=(",", ":"), allow_nan=False).encode()
            if (isinstance(weights, list) and len(weights) == len(first.features) + 1
                and all(type(w) in (int, float) and math.isfinite(w) for w in weights)
                and hashlib.sha256(encoded).hexdigest() == row["weights_sha256"]):
                state["final_weights"] = tuple(weights)
                self._loaded += 1
                return WalkForwardResult(**state)
        fitted = walk_forward(samples, as_of=as_of, minimum_evaluated=50)
        self._fitted += 1
        state = json.dumps(asdict(fitted), sort_keys=True, separators=(",", ":"), allow_nan=False)
        weights_sha = hashlib.sha256(json.dumps(
            fitted.final_weights, separators=(",", ":"), allow_nan=False,
        ).encode()).hexdigest()
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO rival_live_model_checkpoints "
                "(venue,contract,quote,horizon_minutes,feature_contract,target_contract,"
                "training_digest,trained_at,weights_sha256,state_json) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(venue,contract,quote,horizon_minutes,feature_contract,target_contract) "
                "DO UPDATE SET training_digest=excluded.training_digest,trained_at=excluded.trained_at,"
                "weights_sha256=excluded.weights_sha256,state_json=excluded.state_json "
                "WHERE rival_live_model_checkpoints.trained_at<=excluded.trained_at",
                (*identity, digest, utc(as_of).isoformat(), weights_sha, state),
            )
        return fitted

    def summary(self):
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n,MAX(trained_at) AS latest FROM rival_live_model_checkpoints",
            ).fetchone()
        return {"persisted_checkpoints": int(row["n"]), "latest_trained_at": row["latest"],
                "reused_this_process": self._loaded, "fitted_this_process": self._fitted,
                "feature_contract": FEATURE_CONTRACT, "target_contract": TARGET_CONTRACT}
