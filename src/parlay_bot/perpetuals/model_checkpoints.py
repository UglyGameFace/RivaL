"""Versioned online state, trained once per matured observation and reused.

Matching, screened historical models may seed a cold research model. Actual
forward accuracy and trading qualification remain separate, prospective gates.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
from datetime import UTC, datetime

from parlay_bot.perpetuals.fusion import utc
from parlay_bot.perpetuals.learning import WalkForwardResult, _sigmoid

FEATURE_CONTRACT = "relative-rsi-macd-bollinger-atr-volume-40x5m-v1"
TARGET_CONTRACT = "future-5m-close-above-reference-close-with-horizon-delay-v1"
STATE_VERSION = "persistent-online-logistic-v3"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


# Correction fingerprints saved in the checkpoint's "recent" map. They only
# detect a changed (label time, inputs, label) for an issuance already
# trained on; 64 bits of SHA-256 makes an accidental match negligible, and a
# quarter-length digest shrinks the ~92 KB state rewritten every cycle.
# Checkpoints saved with full 64-hex digests are compared on their prefix,
# so existing learned weights are kept, not cold-restarted.
FINGERPRINT_HEX = 16


def _copy_state(state: dict) -> dict:
    """Independent copy for the cache. fit() mutates only ``weights`` in place;
    ``recent`` and ``cursor`` are always replaced, never edited, so a shallow
    copy of those is enough (deepcopy of the 2,000-entry map was measurable)."""
    copied = dict(state)
    copied["weights"] = list(state["weights"])
    copied["recent"] = dict(state["recent"])
    if state.get("cursor") is not None:
        copied["cursor"] = list(state["cursor"])
    return copied


def _same_fingerprint(saved, fresh: str) -> bool:
    """``fresh`` is FINGERPRINT_HEX long; ``saved`` may be a legacy full digest."""
    return (isinstance(saved, str) and len(saved) >= FINGERPRINT_HEX
            and saved[:FINGERPRINT_HEX] == fresh)


class LiveModelCheckpoints:
    def __init__(self, store):
        self.store = store
        self._initialized = False
        self._lock = threading.RLock()
        self._loaded = 0
        self._fitted = 0
        # A digest probe checks PostgreSQL on every fit; cached JSON is never
        # trusted when a different process has changed the durable checkpoint.
        self._checkpoint_cache = {}
        self._cache_hits = 0
        self._fingerprint_memo: dict = {}

    def _cache_checkpoint(self, identity, row, state):
        if identity not in self._checkpoint_cache and len(self._checkpoint_cache) >= 144:
            self._checkpoint_cache.pop(next(iter(self._checkpoint_cache)))
        self._checkpoint_cache[identity] = (row, _copy_state(state))

    def _fingerprints(self, identity, ordered, now) -> dict[str, str]:
        """Correction fingerprint per matured issuance, hashed once per process.

        Each cycle presents ~1,000 samples of which ~1 is new; re-hashing all of
        them was most of fit()'s own CPU. A memo hit requires the identical
        (label time, inputs, label), so any source correction is re-hashed.
        """
        previous = self._fingerprint_memo.get(identity, {})
        current: dict = {}
        result: dict[str, str] = {}
        for s in ordered:
            known = utc(s.label_known_at)
            if known > now:
                continue
            issued = utc(s.issued_at).isoformat()
            content = (known, tuple(s.features), s.label_up)
            hit = previous.get(issued)
            if hit is not None and hit[0] == content:
                digest = hit[1]
            else:
                digest = hashlib.sha256(canonical([
                    known.isoformat(), s.features, s.label_up,
                ]).encode()).hexdigest()[:FINGERPRINT_HEX]
            current[issued] = (content, digest)
            result[issued] = digest
        if identity not in self._fingerprint_memo and len(self._fingerprint_memo) >= 144:
            self._fingerprint_memo.pop(next(iter(self._fingerprint_memo)))
        self._fingerprint_memo[identity] = current
        return result

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
    def _validate(samples):
        ordered = sorted(samples, key=lambda s: utc(s.issued_at))
        if not ordered:
            return ordered
        first = ordered[0]
        scope = (first.venue, first.contract, first.quote, first.horizon_minutes)
        previous = None
        for sample in ordered:
            issued, known = utc(sample.issued_at), utc(sample.label_known_at)
            if ((sample.venue, sample.contract, sample.quote, sample.horizon_minutes) != scope
                or len(sample.features) != 10 or sample.label_up not in (0, 1)
                or issued >= known or (previous is not None and issued <= previous)
                or any(not math.isfinite(v) or abs(v) > 5 for v in sample.features)):
                raise ValueError("invalid persistent-model example")
            previous = issued
        return ordered

    @staticmethod
    def _restore(row, now):
        if row is None:
            return None
        try:
            state = json.loads(row["state_json"])
            weights = state["weights"]
            if (state["version"] != STATE_VERSION
                or utc(datetime.fromisoformat(row["trained_at"])) > now
                or len(weights) != 11
                or any(type(w) not in (int, float) or not math.isfinite(w) for w in weights)
                or hashlib.sha256(canonical(state).encode()).hexdigest() != row["training_digest"]
                or hashlib.sha256(canonical(weights).encode()).hexdigest() != row["weights_sha256"]
                or type(state["updates"]) is not int or state["updates"] < 0
                or type(state["historical_training_labels"]) is not int
                or state["historical_training_labels"] < 0):
                return None
            if state["cursor"] is not None:
                utc(datetime.fromisoformat(state["cursor"][0]))
                utc(datetime.fromisoformat(state["cursor"][1]))
            if not isinstance(state["recent"], dict):
                return None
            return state
        except (ValueError, TypeError, KeyError, IndexError, OverflowError, AttributeError):
            return None

    def _cold_state(self, first, now, seed=None):
        if seed and (
            seed.get("feature_contract") != FEATURE_CONTRACT
            or seed.get("target_contract") != TARGET_CONTRACT
            or seed.get("retrospective_screen") != "passed"
            or (seed["identity"]["venue"], seed["identity"]["contract"], seed["identity"]["quote"],
                seed["horizon_minutes"]) != (first.venue, first.contract, first.quote, first.horizon_minutes)
            or utc(datetime.fromisoformat(seed["evaluation_completed_through"])) >= now
        ):
            raise ValueError("ineligible historical research seed")
        return {"version": STATE_VERSION, "weights": list(seed["weights"]) if seed else [0.] * 11,
                "cursor": None, "updates": 0, "recent": {},
                "historical_seed": seed["model_sha256"] if seed else None,
                "historical_training_cutoff": seed["training_cutoff"] if seed else None,
                "historical_training_labels": seed["training_count"] if seed else 0}

    def fit(self, samples, *, as_of: datetime, seed=None) -> WalkForwardResult:
        """Train on new matured labels once; rolling input eviction retains memory.

        Source corrections rebuild this research state. Earlier fixture clocks
        cannot read future weights or overwrite a newer saved checkpoint.
        Frozen issued predictions are never rewritten.
        """
        ordered = self._validate(samples)
        if not ordered:
            return WalkForwardResult(0, 0, None, None, None, False, ())
        now = utc(as_of)
        first = ordered[0]
        identity = (first.venue, first.contract, first.quote, first.horizon_minutes,
                    FEATURE_CONTRACT + (":" + seed["model_sha256"] if seed else ""), TARGET_CONTRACT)
        self.initialize()
        with self._lock:
            query_scope = ("WHERE venue=? AND contract=? AND quote=? "
                           "AND horizon_minutes=? AND feature_contract=? AND target_contract=?")
            cached = self._checkpoint_cache.get(identity)
            cached_read = False
            with self.store.connect() as conn:
                if cached is not None:
                    # Small indexed probe avoids downloading the large
                    # correction-fingerprint map on every five-minute cycle.
                    probe = conn.execute(
                        "SELECT training_digest,trained_at FROM rival_live_model_checkpoints "
                        + query_scope, identity,
                    ).fetchone()
                    if (probe is not None
                        and probe["training_digest"] == cached[0]["training_digest"]
                        and probe["trained_at"] == cached[0]["trained_at"]):
                        row = cached[0]
                        cached_read = True
                    else:
                        row = conn.execute(
                            "SELECT * FROM rival_live_model_checkpoints " + query_scope,
                            identity,
                        ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT * FROM rival_live_model_checkpoints " + query_scope,
                        identity,
                    ).fetchone()
            # An earlier as_of must never reuse weights from the future.
            if cached_read and utc(datetime.fromisoformat(row["trained_at"])) <= now:
                state = _copy_state(cached[1])
                self._cache_hits += 1
            else:
                state = self._restore(row, now)
            fingerprints = self._fingerprints(identity, ordered, now)
            if state and any(k in state["recent"]
                             and not _same_fingerprint(state["recent"][k], v)
                             for k, v in fingerprints.items()):
                state = None
            if state is None:
                state = self._cold_state(first, now, seed)
            else:
                self._loaded += 1
            cursor = tuple(state["cursor"]) if state["cursor"] else None
            trained = 0
            for sample in sorted(ordered, key=lambda s: (utc(s.label_known_at), utc(s.issued_at))):
                known, issued = utc(sample.label_known_at), utc(sample.issued_at)
                key = (known.isoformat(), issued.isoformat())
                if known > now or (cursor is not None and key <= cursor):
                    continue
                cutoff = state["historical_training_cutoff"]
                if cutoff and known <= utc(datetime.fromisoformat(cutoff)):
                    continue
                weights = state["weights"]
                x = (1., *sample.features)
                error = sample.label_up - _sigmoid(sum(w*v for w, v in zip(weights, x)))
                for i, value in enumerate(x):
                    weights[i] += .025 * (error * value - (.0001 * weights[i] if i else 0))
                state["updates"] += 1
                state["cursor"] = list(key)
                cursor = key
                trained += 1
            # Bounded source-overlap validation; learned weights retain all
            # prior updates, even when their candles leave the hot window.
            state["recent"] = {
                k: v[:FINGERPRINT_HEX] for k, v in
                sorted({**state["recent"], **fingerprints}.items())[-2000:]
            }
            encoded = canonical(state)
            weights_sha = hashlib.sha256(canonical(state["weights"]).encode()).hexdigest()
            if row is None or encoded != row["state_json"]:
                digest = hashlib.sha256(encoded.encode()).hexdigest()
                with self.store.connect() as conn:
                    saved = conn.execute(
                        "INSERT INTO rival_live_model_checkpoints "
                        "(venue,contract,quote,horizon_minutes,feature_contract,target_contract,"
                        "training_digest,trained_at,weights_sha256,state_json) VALUES (?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(venue,contract,quote,horizon_minutes,feature_contract,target_contract) "
                        "DO UPDATE SET training_digest=excluded.training_digest,trained_at=excluded.trained_at,"
                        "weights_sha256=excluded.weights_sha256,state_json=excluded.state_json "
                        "WHERE rival_live_model_checkpoints.trained_at<=excluded.trained_at "
                        "AND rival_live_model_checkpoints.training_digest=?",
                        (*identity, digest, now.isoformat(), weights_sha, encoded,
                         row["training_digest"] if row is not None else ""),
                    )
                if saved.rowcount:
                    self._cache_checkpoint(
                        identity,
                        {"training_digest": digest, "trained_at": now.isoformat(),
                         "weights_sha256": weights_sha, "state_json": encoded},
                        state,
                    )
                elif row is None or utc(datetime.fromisoformat(row["trained_at"])) <= now:
                    # Another writer won the race. Refuse to issue a forecast
                    # whose learned weights were not durably checkpointed.
                    # The next cycle reloads the winning version.
                    self._checkpoint_cache.pop(identity, None)
                    raise RuntimeError("live checkpoint changed during fit; retry from database")
            elif (cached is None or row["training_digest"] != cached[0]["training_digest"]
                  or row["trained_at"] != cached[0]["trained_at"]):
                # Another writer can commit identical weights at a newer time.
                # Adopt that validated metadata even when no new labels exist.
                self._cache_checkpoint(
                    identity,
                    {"training_digest": row["training_digest"],
                     "trained_at": row["trained_at"],
                     "weights_sha256": row["weights_sha256"],
                     "state_json": row["state_json"]},
                    state,
                )
            self._fitted += int(trained > 0)
            labels = state["updates"] + state["historical_training_labels"]
            # Training labels are not unseen forward scores. Those are
            # recorded exclusively in perp_forward_audit before resolution.
            return WalkForwardResult(
                max(0, labels-20), state["updates"], None, None,
                None, False, tuple(state["weights"]),
                training_labels=labels,
                historical_training_labels=state["historical_training_labels"],
                historical_seed_sha256=state["historical_seed"],
            )

    def summary(self):
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute("SELECT * FROM rival_live_model_checkpoints").fetchall()
        valid = [self._restore(r, datetime.max.replace(tzinfo=UTC)) for r in rows]
        return {"persisted_checkpoints": len(rows),
                "latest_trained_at": max((r["trained_at"] for r in rows), default=None),
                "reused_this_process": self._loaded, "fitted_this_process": self._fitted,
                "verified_checkpoint_cache_hits": self._cache_hits,
                "historical_seeded_checkpoints": sum(bool(s and s["historical_seed"]) for s in valid),
                "feature_contract": FEATURE_CONTRACT, "target_contract": TARGET_CONTRACT,
                "model_version": STATE_VERSION}
