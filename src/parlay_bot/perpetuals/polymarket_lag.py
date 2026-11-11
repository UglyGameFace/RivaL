"""Odds-lag learner: when are Polymarket's odds behind the live price?

The four declared contestants each see one side. The book logistic sees only
Polymarket's order book. The formula, jump and queue-pressure models see
only the live spot price against the contract's strike. None of them sees
both, so none can learn the thing that would beat the market: how often,
and by how much, Polymarket's odds have not yet caught up with where the
price already is.

This learner starts AS the market and learns corrections to it:

    logit(p) = w0 + w1*logit(market) + w2*gap_formula + w3*gap_pressure
               + w4*has_pressure + w5*spread

``gap_formula`` is how far the live-price formula's chance sits from the
market's, in log-odds; ``gap_pressure`` is the same for the order-book
pressure (microprice) version. The weights start at (0, 1, 0, 0, 0, 0), so
before it has learned anything its forecast IS the market price: no
improvement, no trades, no harm. Each update is pulled back toward that
start (L2 toward the prior), so a correction survives only if outcomes keep
supporting it.

House rules it follows:

- Inputs are ONLY what the contest captured at its checkpoint, before the
  outcome: the normalized midpoint and asks, and the formula and pressure
  forecasts frozen in the same receipt. Nothing is recomputed afterwards.
- It learns only from verified contract resolutions (the contest's own
  outcome table), oldest first, each contract once, and only from contracts
  that resolved before the forecast being made.
- One learner per coin, timeframe and settlement source (schema), like the
  contest's scopes.
- It issues nothing until a learner has ``MIN_LABELS`` resolutions.
- It is a new contest identity, so it draws its error budget from the half
  reserved for later identities; the four declared models' tests and their
  budgets are unchanged.
- Any failure here costs only this forecast: the caller drops it.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from datetime import UTC, datetime

MODEL = "digital-odds-lag-logistic-v1"
MIN_LABELS = 50
LEARNING_RATE = 0.02
PRIOR_PULL = 0.01  # L2 toward the start (the market), per update
LEARN_EVERY_SECONDS = 120
LEARN_BATCH = 500
PRIOR = (0.0, 1.0, 0.0, 0.0, 0.0, 0.0)
GAP_LIMIT = 6.0

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS rival_poly_lag_state (
        asset TEXT NOT NULL, horizon INTEGER NOT NULL, schema_key TEXT NOT NULL,
        weights_json TEXT NOT NULL, weights_sha256 TEXT NOT NULL,
        updates INTEGER NOT NULL, trained_at TEXT NOT NULL,
        PRIMARY KEY (asset,horizon,schema_key)
    )""",
    """CREATE TABLE IF NOT EXISTS rival_poly_lag_learned (
        slug TEXT PRIMARY KEY, learned_at TEXT NOT NULL
    )""",
)


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(weights) -> str:
    return hashlib.sha256(_canonical(list(weights)).encode()).hexdigest()


def _logit(p: float) -> float:
    p = min(0.99, max(0.01, float(p)))
    return math.log(p / (1 - p))


def _sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-max(-35.0, min(35.0, z))))


def market_up(up_bid, up_ask, down_bid, down_ask) -> float | None:
    """The contest's normalized midpoint, or None for an unusable book."""
    try:
        ub, ua, db, da = (float(x) for x in (up_bid, up_ask, down_bid, down_ask))
    except (TypeError, ValueError):
        return None
    if not (0 < ub <= ua < 1 and 0 < db <= da < 1):
        return None
    return (ub + ua) / (ub + ua + db + da)


def features(*, market: float, formula: float, pressure: float | None,
             up_ask: float, down_ask: float) -> tuple[float, ...] | None:
    """Bias plus the five inputs, all bounded. None if an input is unusable."""
    values = [market, formula, up_ask, down_ask]
    if pressure is not None:
        values.append(pressure)
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
        return None
    if not (0 <= market <= 1 and 0 <= formula <= 1 and 0 < up_ask < 1 and 0 < down_ask < 1):
        return None
    if pressure is not None and not 0 <= pressure <= 1:
        return None
    base = _logit(market)

    def gap(p):
        return max(-GAP_LIMIT, min(GAP_LIMIT, _logit(p) - base))

    spread = max(0.0, min(0.2, up_ask + down_ask - 1)) * 10
    return (1.0, base, gap(formula), 0.0 if pressure is None else gap(pressure),
            0.0 if pressure is None else 1.0, spread)


class OddsLagLearner:
    def __init__(self, store):
        self.store = store
        self._lock = threading.RLock()
        self._ready = False
        self._learned_at = 0.0

    def initialize(self) -> None:
        if self._ready:
            return
        with self.store.connect() as conn:
            for statement in SCHEMA:
                conn.execute(statement)
        self._ready = True

    # -- learning ------------------------------------------------------------

    def learn(self, *, now: datetime | None = None, force: bool = False) -> int:
        """Teach every learner the contracts resolved since it last learned.

        Returns the number of contracts learned. Throttled to once every
        ``LEARN_EVERY_SECONDS`` unless ``force``.
        """
        from parlay_bot.perpetuals.model_league import FORMULA, PRESSURE

        with self._lock:
            if not force and time.monotonic() - self._learned_at < LEARN_EVERY_SECONDS:
                return 0
            self._learned_at = time.monotonic()
        self.initialize()
        stamp = (now or datetime.now(UTC)).astimezone(UTC)
        clock = stamp.isoformat()
        learned = 0
        with self._lock, self.store.connect() as conn:
            rows = conn.execute(
                """SELECT r.slug, r.asset, r.horizon, r.schema_key, r.market_up,
                          r.evidence_json, o.direction,
                          ff.probability_up AS formula_up,
                          fp.probability_up AS pressure_up
                   FROM rival_poly_contest_rounds r
                   JOIN rival_polymarket_ai_outcomes o
                     ON o.slug=r.slug AND o.condition_id=r.condition_id
                   JOIN rival_poly_contest_forecasts ff
                     ON ff.slug=r.slug AND ff.model=?
                   LEFT JOIN rival_poly_contest_forecasts fp
                     ON fp.slug=r.slug AND fp.model=?
                   LEFT JOIN rival_poly_lag_learned l ON l.slug=r.slug
                   WHERE l.slug IS NULL AND o.direction IN ('UP','DOWN')
                     AND o.resolved_at>=r.expires_at AND o.observed_at<=?
                     AND r.issued_at<r.expires_at
                   ORDER BY o.observed_at, r.slug LIMIT ?""",
                (FORMULA, PRESSURE, clock, LEARN_BATCH),
            ).fetchall()
            states: dict[tuple[str, int, str], list] = {}
            for row in rows:
                try:
                    receipt = json.loads(row["evidence_json"])
                    x = features(
                        market=float(row["market_up"]), formula=float(row["formula_up"]),
                        pressure=(None if row["pressure_up"] is None
                                  else float(row["pressure_up"])),
                        up_ask=float(receipt["up_ask"]), down_ask=float(receipt["down_ask"]))
                except (TypeError, ValueError, KeyError):
                    x = None
                conn.execute(
                    "INSERT INTO rival_poly_lag_learned (slug,learned_at) VALUES (?,?) "
                    "ON CONFLICT DO NOTHING", (row["slug"], clock))
                if x is None:
                    continue  # recorded as seen, never retried, never learned
                key = (str(row["asset"]), int(row["horizon"]), str(row["schema_key"]))
                if key not in states:
                    states[key] = [*self._load(conn, *key)]
                weights = states[key][0]
                y = 1.0 if row["direction"] == "UP" else 0.0
                error = y - _sigmoid(sum(w * v for w, v in zip(weights, x)))
                for i, value in enumerate(x):
                    weights[i] += LEARNING_RATE * (
                        error * value - PRIOR_PULL * (weights[i] - PRIOR[i]))
                states[key][1] += 1
                learned += 1
            for (asset, horizon, schema), (weights, updates) in states.items():
                conn.execute(
                    """INSERT INTO rival_poly_lag_state (asset,horizon,schema_key,
                         weights_json,weights_sha256,updates,trained_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT (asset,horizon,schema_key) DO UPDATE SET
                         weights_json=excluded.weights_json,
                         weights_sha256=excluded.weights_sha256,
                         updates=excluded.updates, trained_at=excluded.trained_at""",
                    (asset, horizon, schema, _canonical(weights), _sha(weights),
                     updates, clock))
        return learned

    @staticmethod
    def _load(conn, asset, horizon, schema):
        row = conn.execute(
            """SELECT weights_json,weights_sha256,updates FROM rival_poly_lag_state
               WHERE asset=? AND horizon=? AND schema_key=?""",
            (asset, horizon, schema),
        ).fetchone()
        if row is None:
            return list(PRIOR), 0
        weights = [float(w) for w in json.loads(row["weights_json"])]
        if (len(weights) != len(PRIOR) or not all(math.isfinite(w) for w in weights)
                or _sha(weights) != row["weights_sha256"]):
            raise ValueError("corrupt odds-lag learner state")
        return weights, int(row["updates"])

    # -- forecasting ---------------------------------------------------------

    def forecast(self, notice, schema: str, *, formula: float, pressure: float | None,
                 now: datetime | None = None) -> tuple[float, str] | None:
        """(probability_up, weights_sha256) for this checkpoint, or None."""
        self.learn(now=now)
        market = market_up(notice.up_bid, notice.up_ask, notice.down_bid, notice.down_ask)
        if market is None:
            return None
        x = features(market=market, formula=formula, pressure=pressure,
                     up_ask=float(notice.up_ask), down_ask=float(notice.down_ask))
        if x is None:
            return None
        with self.store.connect() as conn:
            weights, updates = self._load(conn, notice.asset, int(notice.horizon), schema)
        if updates < MIN_LABELS:
            return None
        p = min(0.99, max(0.01, _sigmoid(sum(w * v for w, v in zip(weights, x)))))
        return p, _sha(weights)

    def status(self) -> list[dict]:
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT asset,horizon,schema_key,weights_json,updates,trained_at
                   FROM rival_poly_lag_state ORDER BY asset,horizon,schema_key""",
            ).fetchall()
        out = []
        for row in rows:
            weights = json.loads(row["weights_json"])
            out.append({"asset": row["asset"], "horizon": int(row["horizon"]),
                        "schema_key": row["schema_key"], "updates": int(row["updates"]),
                        "issuing": int(row["updates"]) >= MIN_LABELS,
                        "trust_in_market": weights[1], "weight_on_live_price": weights[2],
                        "weight_on_book_pressure": weights[3],
                        "trained_at": row["trained_at"]})
        return out
