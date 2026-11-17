"""Cross-exchange test models (cross_v1, cross_all_v1). Shadow only.

Why these exist. The main model is one small learner per stream (venue,
coin, horizon). BTC on nine exchanges is nearly the same price series, so
nine learners each read a copy of the same lesson, and what one learns never
reaches another coin. Two questions follow, and each family answers one:

  cross_v1      One learner per coin and horizon. Does a model that sees
                every exchange at once, including how much they disagree,
                beat the per-exchange main model?
  cross_all_v1  One learner per horizon, shared by all coins (the coin is an
                input). Does sharing what is learned across coins help on
                top of that?

What the learner sees (14 inputs, all from closed candles at or before the
reference bar, like the main model -- nothing from inside the target window):

  0-9   the main model's 10 inputs, each taken as the median across the
        exchanges that issued this cycle (a consensus series, so one stale
        or odd exchange cannot move it);
  10    how far apart the exchanges' last 5-minute returns are (max - min);
  11    how many exchanges saw the last bar go up (share * 2 - 1);
  12    how far apart their 5-bar returns are (max - min);
  13    how many exchanges contributed, as a share of nine.

cross_all_v1 adds one flag per coin (18 inputs).

What it learns from. One example per (coin, horizon, target): the consensus
inputs above with the majority outcome of the main model's own graded rows
on the contributing exchanges. A tie teaches nothing and is skipped. Nine
exchanges agreeing on one outcome is ONE lesson, not nine: pooling exchanges
steadies the inputs and labels, it does not multiply the evidence. Sharing
across coins is what adds examples, which is what cross_all_v1 tests.

Rules it lives under (unchanged house rules):

- Same online logistic step as the main model (rate 0.025, L2 0.0001),
  weights start at zero, no history is replayed and nothing is invented.
- It issues nothing until a learner has ``MIN_UPDATES`` graded lessons, so
  the trial record does not include the cold start.
- The same probability is issued on every contributing stream and graded
  there against that stream's main-model call, so Route B, the matched
  quotes and the early-stop rule apply per stream as they do to every
  family. It never serves until Route B promotes it.
- Never raises into the cycle: an unavailable test model is no rows.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import statistics
from datetime import datetime, timedelta

from parlay_bot.perpetuals.fusion import utc
from parlay_bot.perpetuals.markets import ASSETS

_LOG = logging.getLogger(__name__)

FAMILY = "cross_v1"
ALL_FAMILY = "cross_all_v1"
FAMILIES = (FAMILY, ALL_FAMILY)
ALL_SCOPE = "ALL"

MIN_VENUES = 3
MIN_UPDATES = 288  # one day of 5-minute targets
LEARNING_RATE = 0.025
L2 = 0.0001
BASE_INPUTS = 10
CROSS_INPUTS = 14
VENUE_COUNT = 9
# Every target is one 5-minute candle (markets.py: a horizon is the time
# until that candle opens), so it can be graded 5 minutes after it opens.
TARGET_BAR = timedelta(minutes=5)
# A target still missing some exchanges' grades this long after its candle
# closed learns from the ones that did grade (or is dropped if too few), so
# the queue cannot grow without bound.
LABEL_PATIENCE = timedelta(hours=6)
LEARN_BATCH = 400

_CONTRACT = (
    "cross-exchange-consensus-v1",
    "median-of-10-incumbent-features",
    "last-return-spread", "last-return-up-share", "five-bar-return-spread",
    "venue-share-of-9",
    "majority-label", f"min-venues-{MIN_VENUES}",
    f"sgd-{LEARNING_RATE}-l2-{L2}", "zero-start",
)
FEATURE_DIGEST = hashlib.sha256("|".join(_CONTRACT).encode()).hexdigest()
ALL_FEATURE_DIGEST = hashlib.sha256(
    "|".join((*_CONTRACT, "coin-flags", *ASSETS)).encode()).hexdigest()

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS rival_cross_model_examples (
        asset TEXT NOT NULL, horizon_minutes INTEGER NOT NULL,
        reference_opened_at TEXT NOT NULL, target_opened_at TEXT NOT NULL,
        issued_at TEXT NOT NULL, features_json TEXT NOT NULL,
        streams_json TEXT NOT NULL, label_up INTEGER, learned_at TEXT,
        PRIMARY KEY (asset,horizon_minutes,reference_opened_at)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_rival_cross_examples_pending
        ON rival_cross_model_examples (learned_at,target_opened_at)""",
    """CREATE TABLE IF NOT EXISTS rival_cross_model_state (
        family TEXT NOT NULL, scope TEXT NOT NULL,
        horizon_minutes INTEGER NOT NULL, weights_json TEXT NOT NULL,
        updates INTEGER NOT NULL, trained_through TEXT NOT NULL,
        weights_sha256 TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY (family,scope,horizon_minutes)
    )""",
)


def _sigmoid(value: float) -> float:
    return 1 / (1 + math.exp(-max(-35.0, min(35.0, value))))


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def cross_features(vectors) -> tuple[float, ...] | None:
    """The 14 consensus inputs from each exchange's 10 main-model inputs.

    None when fewer than ``MIN_VENUES`` usable vectors are given. Every
    output stays inside the main model's |x| <= 5 input bound.
    """
    usable = [
        tuple(float(x) for x in vector[:BASE_INPUTS]) for vector in vectors or ()
        if len(vector) >= BASE_INPUTS
        and all(isinstance(x, (int, float)) and math.isfinite(x) and abs(x) <= 5
                for x in vector[:BASE_INPUTS])
    ]
    if len(usable) < MIN_VENUES:
        return None
    medians = tuple(statistics.median(column) for column in zip(*usable))
    last = [vector[0] for vector in usable]
    five = [vector[1] for vector in usable]
    return (
        *medians,
        _clip(max(last) - min(last), 0.0, 5.0),
        sum(x > 0 for x in last) / len(last) * 2 - 1,
        _clip(max(five) - min(five), 0.0, 5.0),
        _clip(len(usable) / VENUE_COUNT, 0.0, 1.0),
    )


def model_inputs(family: str, asset: str, features) -> tuple[float, ...]:
    """Bias plus the inputs this family's learner reads."""
    if family == ALL_FAMILY:
        return (1.0, *features, *(1.0 if asset == coin else 0.0 for coin in ASSETS))
    return (1.0, *features)


def _scope(family: str, asset: str) -> str:
    return ALL_SCOPE if family == ALL_FAMILY else asset


# One batched outcome read per 100 stream keys (500 parameters) stays under
# SQLite's 999-parameter floor as well as PostgreSQL's limit; the per-stream
# primary-key reads it replaces ran one query per stream per target.
_OUTCOME_BATCH_KEYS = 100


def _fetch_outcomes(conn, keys, model_version: str) -> dict[tuple, int]:
    """Graded outcome per (venue, contract, quote, horizon, reference).

    One batched read per chunk instead of one primary-key SELECT per
    contributing stream. The keys are the audit table's primary key minus
    model_version, which is filtered once for the whole batch; the settled
    and outcome predicates match the per-stream read exactly.
    """
    found: dict[tuple, int] = {}
    for start in range(0, len(keys), _OUTCOME_BATCH_KEYS):
        chunk = keys[start:start + _OUTCOME_BATCH_KEYS]
        groups = ",".join("(?,?,?,?,?)" for _ in chunk)
        params: list = [model_version]
        for venue, contract, quote, horizon, reference in chunk:
            params.extend((venue, contract, quote, horizon, reference))
        rows = conn.execute(
            f"""SELECT venue,contract,quote,horizon_minutes,reference_opened_at,
                       outcome_up
                FROM perp_forward_audit
                WHERE model_version=? AND settled_at IS NOT NULL AND outcome_up IN (0,1)
                  AND (venue,contract,quote,horizon_minutes,reference_opened_at)
                      IN ({groups})""",
            tuple(params),
        ).fetchall()
        for row in rows:
            found[(str(row["venue"]), str(row["contract"]), str(row["quote"]),
                   int(row["horizon_minutes"]),
                   str(row["reference_opened_at"]))] = int(row["outcome_up"])
    return found


def _sha(weights) -> str:
    return hashlib.sha256(json.dumps(
        list(weights), separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def majority_label(graded: int, ups: int) -> int | None:
    """1 or 0 when a strict majority of the exchanges agree, else None."""
    if graded < MIN_VENUES or ups * 2 == graded:
        return None
    return 1 if ups * 2 > graded else 0


class CrossModel:
    def __init__(self, store):
        self.store = store

    def initialize(self) -> None:
        if getattr(self.store, "_cross_model_ready", False):
            return
        with self.store.connect() as conn:
            for statement in SCHEMA:
                conn.execute(statement)
        try:
            self.store._cross_model_ready = True
        except AttributeError:
            pass

    # -- state ---------------------------------------------------------------

    @staticmethod
    def _decode_state(row, size: int):
        """Validate a stored state row exactly as _load does, without a connection."""
        weights = [float(x) for x in json.loads(row["weights_json"])]
        if len(weights) != size or not all(math.isfinite(x) for x in weights):
            raise ValueError("cross model state does not match its input contract")
        return weights, int(row["updates"])

    @staticmethod
    def _load(conn, family: str, scope: str, horizon: int, size: int):
        row = conn.execute(
            """SELECT weights_json,updates,trained_through,weights_sha256
               FROM rival_cross_model_state
               WHERE family=? AND scope=? AND horizon_minutes=?""",
            (family, scope, horizon),
        ).fetchone()
        if row is None:
            return [0.0] * size, 0
        return CrossModel._decode_state(row, size)

    @staticmethod
    def _save(conn, family, scope, horizon, weights, updates, through, clock):
        conn.execute(
            """INSERT INTO rival_cross_model_state (
                 family,scope,horizon_minutes,weights_json,updates,
                 trained_through,weights_sha256,updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT (family,scope,horizon_minutes) DO UPDATE SET
                 weights_json=excluded.weights_json, updates=excluded.updates,
                 trained_through=excluded.trained_through,
                 weights_sha256=excluded.weights_sha256,
                 updated_at=excluded.updated_at""",
            (family, scope, horizon,
             json.dumps(weights, separators=(",", ":"), allow_nan=False),
             updates, through, _sha(weights), clock),
        )

    # -- issuance ------------------------------------------------------------

    def issue(self, pending, *, issued_at: datetime, stops=None) -> int:
        """Issue both families for every target with enough exchanges.

        ``pending`` holds one entry per stream and horizon whose main-model
        forecast was stored this cycle: venue, contract, quote, asset,
        horizon, reference_opened_at, target_opened_at, incumbent_up,
        baseline_up and that stream's 10 main-model inputs. ``stops`` is
        the cycle's early-stop map (stream and horizon -> families); without
        it each stream looks its own stops up. Returns the shadow rows saved.
        """
        from parlay_bot.perpetuals.model_competition import ShadowModelCompetition

        groups: dict[tuple[str, int, str], list[dict]] = {}
        for entry in pending or ():
            key = (str(entry["asset"]), int(entry["horizon"]),
                   utc(entry["reference_opened_at"]).isoformat())
            groups.setdefault(key, []).append(entry)
        if not groups:
            return 0
        self.initialize()
        clock = utc(issued_at).isoformat()
        competition = ShadowModelCompetition(self.store)
        saved = 0
        for (asset, horizon, reference), entries in sorted(groups.items()):
            if asset not in ASSETS:
                continue
            by_venue = {str(entry["venue"]): entry for entry in entries}
            features = cross_features([e["features"] for e in by_venue.values()])
            if features is None:
                continue
            target = utc(entries[0]["target_opened_at"]).isoformat()
            with self.store.connect() as conn:
                conn.execute(
                    """INSERT INTO rival_cross_model_examples (
                         asset,horizon_minutes,reference_opened_at,
                         target_opened_at,issued_at,features_json,streams_json)
                       VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                    (asset, horizon, reference, target, clock,
                     json.dumps(features, separators=(",", ":"), allow_nan=False),
                     json.dumps(sorted(
                         [e["venue"], e["contract"], e["quote"]]
                         for e in by_venue.values()), separators=(",", ":"))),
                )
                calls = []
                for family in FAMILIES:
                    inputs = model_inputs(family, asset, features)
                    weights, updates = self._load(
                        conn, family, _scope(family, asset), horizon, len(inputs))
                    if updates < MIN_UPDATES:
                        continue
                    probability = _clip(_sigmoid(sum(
                        w * x for w, x in zip(weights, inputs))), 0.02, 0.98)
                    calls.append({"family": family, "probability": probability,
                                  "training_rows": updates,
                                  "model_sha256": _sha(weights)})
                if not calls:
                    continue
                for entry in by_venue.values():
                    saved += competition.issue(
                        conn, samples=[], features=tuple(entry["features"]),
                        now=utc(issued_at), venue=entry["venue"],
                        contract=entry["contract"], quote=entry["quote"],
                        asset=asset, horizon=horizon,
                        reference_opened_at=utc(entry["reference_opened_at"]),
                        target_opened_at=utc(entry["target_opened_at"]),
                        issued_at=utc(issued_at),
                        incumbent_up=float(entry["incumbent_up"]),
                        baseline_up=float(entry["baseline_up"]),
                        challengers=calls,
                        prepared={"predictions": {}, "training_count": 0,
                                  "fingerprint": "0" * 64},
                        stopped=(None if stops is None else stops.get(
                            (entry["venue"], entry["contract"], entry["quote"],
                             horizon), frozenset())),
                    )
        return saved

    # -- learning ------------------------------------------------------------

    def learn(self, now: datetime, *, model_version: str) -> int:
        """Teach both families every target whose outcome is now graded.

        Lessons are applied oldest first. A target learns once: with its
        majority outcome, or not at all when the exchanges tie or too few
        are graded within ``LABEL_PATIENCE``. Returns the lessons applied.

        Fetch-release-compute-write: one short read borrow fetches the
        waiting targets, every contributing stream's graded outcome in
        batched reads, and every learner state; the connection is released
        while labels and weight updates are computed in Python; one short
        write borrow persists the learned marks and states. Learning used
        to hold one of eight pool connections across the whole batch while
        issuing one primary-key SELECT per stream per target.
        """
        self.initialize()
        stamp = utc(now)
        clock = stamp.isoformat()
        # Phase 1: batched reads in one short borrow. No outcome is decided
        # here; the streams of every waiting target are collected first so
        # their grades come back in as few queries as the batch allows.
        with self.store.connect() as conn:
            waiting = conn.execute(
                """SELECT asset,horizon_minutes,reference_opened_at,
                          target_opened_at,features_json,streams_json
                   FROM rival_cross_model_examples
                   WHERE learned_at IS NULL AND target_opened_at<=?
                   ORDER BY target_opened_at,asset,horizon_minutes LIMIT ?""",
                ((stamp - TARGET_BAR).isoformat(), LEARN_BATCH),
            ).fetchall()
            streams: list[list[tuple[str, str, str]]] = []
            keys: list[tuple] = []
            seen: set[tuple] = set()
            for row in waiting:
                parsed = [(str(venue), str(contract), str(quote))
                          for venue, contract, quote in json.loads(row["streams_json"])]
                streams.append(parsed)
                for venue, contract, quote in parsed:
                    key = (venue, contract, quote, int(row["horizon_minutes"]),
                           str(row["reference_opened_at"]))
                    if key not in seen:
                        seen.add(key)
                        keys.append(key)
            outcomes = _fetch_outcomes(conn, keys, model_version)
            raw_states = {
                (str(row["family"]), str(row["scope"]), int(row["horizon_minutes"])): row
                for row in conn.execute(
                    """SELECT family,scope,horizon_minutes,weights_json,updates
                       FROM rival_cross_model_state"""
                ).fetchall()
            }
        # Phase 2: compute with no connection held. A corrupt state still
        # raises before anything is written, and a target that is still
        # waiting on grades, ties, or has unusable features is marked
        # learned (or left waiting) exactly as the single-borrow version did.
        states: dict[tuple[str, str, int], list] = {}
        marks: list[tuple] = []
        lessons = 0
        for row, parsed in zip(waiting, streams):
            asset, horizon = str(row["asset"]), int(row["horizon_minutes"])
            reference = str(row["reference_opened_at"])
            graded = ups = 0
            for venue, contract, quote in parsed:
                outcome = outcomes.get((venue, contract, quote, horizon, reference))
                if outcome is not None:
                    graded += 1
                    ups += outcome
            expected = len(parsed)
            closes = datetime.fromisoformat(str(row["target_opened_at"])) + TARGET_BAR
            if graded < expected and stamp < closes + LABEL_PATIENCE:
                continue  # some exchanges are still ungraded: wait
            label = majority_label(graded, ups)
            marks.append((label, clock, asset, horizon, reference))
            if label is None:
                continue
            features = tuple(float(x) for x in json.loads(row["features_json"]))
            if len(features) != CROSS_INPUTS:
                continue
            for family in FAMILIES:
                inputs = model_inputs(family, asset, features)
                key = (family, _scope(family, asset), horizon)
                if key not in states:
                    raw = raw_states.get(key)
                    if raw is None:
                        weights, updates = [0.0] * len(inputs), 0
                    else:
                        weights, updates = self._decode_state(raw, len(inputs))
                    states[key] = [weights, updates, reference]
                weights = states[key][0]
                error = label - _sigmoid(sum(w * x for w, x in zip(weights, inputs)))
                for index, value in enumerate(inputs):
                    penalty = 0.0 if index == 0 else L2 * weights[index]
                    weights[index] += LEARNING_RATE * (error * value - penalty)
                states[key][1] += 1
                states[key][2] = max(states[key][2], reference)
            lessons += 1
        if not marks and not states:
            return 0
        # Phase 3: one short write borrow. Nothing was written before this
        # point, so a failure above leaves no partial marks.
        with self.store.connect() as conn:
            for label, learned_at, asset, horizon, reference in marks:
                conn.execute(
                    """UPDATE rival_cross_model_examples
                       SET label_up=?, learned_at=?
                       WHERE asset=? AND horizon_minutes=? AND reference_opened_at=?""",
                    (label, learned_at, asset, horizon, reference),
                )
            for (family, scope, horizon), (weights, updates, through) in states.items():
                self._save(conn, family, scope, horizon, weights, updates, through, clock)
        return lessons

    # -- reporting -----------------------------------------------------------

    def status(self) -> list[dict]:
        """Each learner's progress, for cards and receipts."""
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT family,scope,horizon_minutes,updates,trained_through,
                          weights_sha256,updated_at
                   FROM rival_cross_model_state
                   ORDER BY family,scope,horizon_minutes""",
            ).fetchall()
        return [{**dict(row), "issuing": int(row["updates"]) >= MIN_UPDATES,
                 "lessons_needed": max(0, MIN_UPDATES - int(row["updates"]))}
                for row in rows]


def artifact_identity(store, family: str, asset: str, horizon: int) -> dict | None:
    """Continuity identity for Route B, or None. Same shape as regime_v1's.

    There is no offline artifact: the identity is the live learner behind
    the latest graded shadow row -- its weights sha, when it issued, and how
    many graded rows exist. selected() requires the same feature digest and
    a record that only moves forward.
    """
    if family not in FAMILIES:
        return None
    try:
        with store.connect() as conn:
            latest = conn.execute(
                """SELECT training_sha256, issued_at
                   FROM rival_model_shadow_forecasts
                   WHERE family=? AND asset=? AND horizon_minutes=?
                     AND settled_at IS NOT NULL AND outcome_up IN (0,1)
                   ORDER BY issued_at DESC LIMIT 1""",
                (family, asset, int(horizon)),
            ).fetchone()
            if latest is None:
                return None
            row = conn.execute(
                """SELECT COUNT(*) AS n
                   FROM rival_model_shadow_forecasts
                   WHERE family=? AND asset=? AND horizon_minutes=?
                     AND settled_at IS NOT NULL AND outcome_up IN (0,1)""",
                (family, asset, int(horizon)),
            ).fetchone()
    except Exception:  # noqa: BLE001 - an unavailable test model is None
        return None
    return {
        "feature_digest": ALL_FEATURE_DIGEST if family == ALL_FAMILY else FEATURE_DIGEST,
        "weights_sha256": str(latest["training_sha256"]),
        "trained_through": str(latest["issued_at"]),
        "graded_count": int(row["n"]) if row is not None else 0,
    }


def live_continuity(recorded, current) -> bool:
    """True when ``current`` is a forward continuation of ``recorded``."""
    if not recorded or not current:
        return False
    try:
        recorded_sha = str(recorded["weights_sha256"])
        recorded_through = utc(datetime.fromisoformat(str(recorded["trained_through"])))
        recorded_count = int(recorded["graded_count"])
        current_through = utc(datetime.fromisoformat(str(current["trained_through"])))
        current_count = int(current["graded_count"])
        if (len(recorded_sha) != 64
                or str(recorded["feature_digest"]) != current["feature_digest"]
                or current_through < recorded_through
                or current_count < recorded_count):
            return False
        return (str(current["weights_sha256"]) == recorded_sha
                or (current_count > recorded_count
                    and current_through >= recorded_through))
    except (ValueError, TypeError, KeyError):
        return False


__all__ = [
    "ALL_FAMILY", "FAMILIES", "FAMILY", "MIN_UPDATES", "MIN_VENUES", "CrossModel",
    "artifact_identity", "cross_features", "live_continuity", "majority_label",
    "model_inputs",
]
