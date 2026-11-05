"""Order-flow challenger: price features + live order flow and derivatives.

A shadow model only. It never serves calls; it is graded on the same future
outcome as every other family in ``rival_model_shadow_forecasts`` and can
only ever serve through the existing promotion gates.

Inputs (all observable BEFORE issuance, persisted at issuance):
- the incumbent's 10 causal price features, unchanged;
- Hyperliquid order flow from ``perp_liquidity_5m``: aggressor trade
  imbalance over the last 5 and 15 minutes, book imbalance within 10 and
  25 bps of the mid, and the mean spread;
- Hyperliquid derivatives context from ``perp_market_contexts``: funding
  rate, 1-hour open-interest change and the mark-vs-oracle premium;
- a flag saying whether order-flow data was available at all.

One online logistic model per (asset, horizon), pooled across every
exchange of that asset (9x the training data of a per-venue model). It
learns only from matured, settled incumbent outcomes, in settlement order,
using the exact inputs stored when the forecast was issued. Its probability
is shrunk toward the observed up-rate until it has enough graded results to
earn confidence, so a young model cannot be confidently wrong.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta

FAMILY = "flow_v1"
FLOW_FEATURES = 9
LEARNING_RATE = 0.02
L2 = 1e-4
SHRINK_HALF = 300  # graded results at which the model gets half its own say
MAX_LEARN_PER_CYCLE = 4000
LEARNED_BATCH = 100  # 5 keys each: 500 parameters, under SQLite's 999

def _schema(features: str, state: str) -> tuple[str, ...]:
    return (
        f"""CREATE TABLE IF NOT EXISTS {features} (
            venue TEXT NOT NULL, contract TEXT NOT NULL, quote TEXT NOT NULL,
            horizon_minutes INTEGER NOT NULL, reference_opened_at TEXT NOT NULL,
            asset TEXT NOT NULL, issued_at TEXT NOT NULL, features TEXT NOT NULL,
            learned INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (venue,contract,quote,horizon_minutes,reference_opened_at)
        )""",
        f"""CREATE INDEX IF NOT EXISTS idx_{features}_pending
            ON {features} (learned, issued_at)""",
        f"""CREATE TABLE IF NOT EXISTS {state} (
            asset TEXT NOT NULL, horizon_minutes INTEGER NOT NULL,
            weights TEXT NOT NULL, updates INTEGER NOT NULL, positives INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (asset, horizon_minutes)
        )""",
    )


def _clip(value: float, low: float = -5.0, high: float = 5.0) -> float:
    if not math.isfinite(value):
        return 0.0
    return max(low, min(high, value))


def _ratio(a, b) -> float:
    a, b = float(a or 0), float(b or 0)
    return 0.0 if a + b <= 0 else (a - b) / (a + b)


def _when(value) -> datetime:
    stamp = datetime.fromisoformat(str(value).replace(" ", "T", 1))
    return stamp if stamp.utcoffset() is not None else stamp.replace(tzinfo=UTC)


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, x))))


class FlowChallenger:
    family = FAMILY
    n_flow = FLOW_FEATURES
    features_table = "rival_flow_features"
    state_table = "rival_flow_model_state"

    def __init__(self, store) -> None:
        self.store = store
        self._ready = False
        # One instance serves one forecast cycle. Weights only change in
        # learn(), which runs after every forecast of the cycle is issued, so
        # each (asset, horizon) state is read once per cycle, not per forecast.
        self._states: dict[tuple[str, int], dict] = {}

    def initialize(self) -> None:
        if self._ready:
            return
        from parlay_bot.perpetuals.liquidity_record import SCHEMA as LIQUIDITY_SCHEMA
        from parlay_bot.perpetuals.market_store import PerpetualMarketStore

        PerpetualMarketStore(self.store).initialize()  # contexts + forecasts' inputs
        with self.store.connect() as conn:
            conn.execute(LIQUIDITY_SCHEMA)  # same definition the recorder uses
            for statement in _schema(self.features_table, self.state_table):
                conn.execute(statement)
        self._ready = True

    # ---------- inputs ----------
    def asset_inputs(self, now: datetime, assets) -> dict[str, tuple[float, ...]]:
        """One causal order-flow/derivatives vector per asset for this cycle."""
        self.initialize()
        stamp = now.astimezone(UTC)
        out: dict[str, tuple[float, ...]] = {}
        with self.store.connect() as conn:
            for asset in assets:
                buckets = conn.execute(
                    """SELECT bucket_start, complete, spread_bps_mean,
                              bid_depth_10bps_mean, ask_depth_10bps_mean,
                              bid_depth_25bps_mean, ask_depth_25bps_mean,
                              buy_volume, sell_volume
                       FROM perp_liquidity_5m
                       WHERE venue='hyperliquid' AND asset=? AND recorded_at<=?
                         AND bucket_start>=?
                       ORDER BY bucket_start DESC LIMIT 3""",
                    (asset, stamp.isoformat(), (stamp - timedelta(minutes=25)).isoformat()),
                ).fetchall()
                contexts = conn.execute(
                    """SELECT observed_at, mark_price, oracle_price, funding_rate,
                              open_interest_base
                       FROM perp_market_contexts
                       WHERE venue='hyperliquid' AND contract=? AND observed_at<=?
                         AND observed_at>=?
                       ORDER BY observed_at DESC LIMIT 40""",
                    (asset, stamp.isoformat(), (stamp - timedelta(minutes=80)).isoformat()),
                ).fetchall()
                out[asset] = self._vector(buckets, contexts, stamp)
        return out

    @staticmethod
    def _vector(buckets, contexts, stamp: datetime) -> tuple[float, ...]:
        usable = [b for b in buckets if int(b["complete"] or 0) == 1]
        if usable:
            last = usable[0]
            flow_5 = _ratio(last["buy_volume"], last["sell_volume"])
            flow_15 = _ratio(sum(float(b["buy_volume"] or 0) for b in usable),
                             sum(float(b["sell_volume"] or 0) for b in usable))
            book_10 = _ratio(last["bid_depth_10bps_mean"], last["ask_depth_10bps_mean"])
            book_25 = _ratio(last["bid_depth_25bps_mean"], last["ask_depth_25bps_mean"])
            spread = _clip(float(last["spread_bps_mean"] or 0) / 5, 0.0, 5.0)
            available = 1.0
        else:
            flow_5 = flow_15 = book_10 = book_25 = spread = available = 0.0
        funding = oi_change = premium = 0.0
        if contexts:
            now_ctx = contexts[0]
            funding = _clip(float(now_ctx["funding_rate"]) * 1e5 / 2)
            oracle = float(now_ctx["oracle_price"] or 0)
            if oracle > 0:
                premium = _clip((float(now_ctx["mark_price"]) / oracle - 1) * 1e4 / 5)
            hour_ago = [c for c in contexts
                        if _when(c["observed_at"]) <= stamp - timedelta(minutes=55)]
            if hour_ago:
                before = float(hour_ago[0]["open_interest_base"] or 0)
                after = float(now_ctx["open_interest_base"] or 0)
                if before > 0 and after > 0:
                    oi_change = _clip(math.log(after / before) * 100)
        return (flow_5, flow_15, book_10, book_25, spread, funding, oi_change,
                premium, available)

    # ---------- model ----------
    def _state(self, conn, asset: str, horizon: int) -> dict:
        row = conn.execute(
            f"SELECT weights, updates, positives FROM {self.state_table} "
            "WHERE asset=? AND horizon_minutes=?", (asset, horizon)).fetchone()
        if row is None:
            return {"weights": [0.0] * (11 + self.n_flow), "updates": 0, "positives": 0}
        weights = json.loads(row["weights"])
        if len(weights) != 11 + self.n_flow:
            weights = [0.0] * (11 + self.n_flow)
        return {"weights": weights, "updates": int(row["updates"]),
                "positives": int(row["positives"])}

    @staticmethod
    def probability(state: dict, inputs) -> float:
        x = (1.0, *inputs)
        raw = _sigmoid(sum(w * v for w, v in zip(state["weights"], x, strict=True)))
        n = state["updates"]
        base = (state["positives"] + 5) / (n + 10)
        trust = n / (n + SHRINK_HALF)
        return max(0.02, min(0.98, base + trust * (raw - base)))

    def call(self, conn, *, venue, contract, quote, asset, horizon,
             reference_opened_at, issued_at, features, flow) -> dict | None:
        """Persist this forecast's exact inputs and return the shadow call."""
        if flow is None or len(features) != 10 or len(flow) != self.n_flow:
            return None
        inputs = tuple(_clip(float(v)) for v in (*features, *flow))
        conn.execute(
            f"""INSERT INTO {self.features_table}
               (venue,contract,quote,horizon_minutes,reference_opened_at,asset,
                issued_at,features) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
            (venue, contract, quote, horizon, reference_opened_at.isoformat(),
             asset, issued_at.isoformat(), json.dumps(inputs, separators=(",", ":"))),
        )
        key = (asset, horizon)
        if key not in self._states:
            self._states[key] = self._state(conn, asset, horizon)
        state = self._states[key]
        return {
            "family": self.family,
            "probability": self.probability(state, inputs),
            "training_rows": state["updates"],
            "model_sha256": hashlib.sha256(json.dumps(
                state["weights"], separators=(",", ":")).encode()).hexdigest(),
        }

    def learn(self, now: datetime) -> int:
        """Train on forecasts whose real outcome has since been graded."""
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                f"""SELECT x.venue,x.contract,x.quote,x.horizon_minutes,
                          x.reference_opened_at,x.asset,x.features,f.outcome_up
                   FROM {self.features_table} x
                   JOIN perp_forward_audit f
                     ON f.venue=x.venue AND f.contract=x.contract AND f.quote=x.quote
                    AND f.horizon_minutes=x.horizon_minutes
                    AND f.reference_opened_at=x.reference_opened_at
                   WHERE x.learned=0 AND f.settled_at IS NOT NULL AND f.settled_at<=?
                   ORDER BY f.settled_at, x.issued_at LIMIT ?""",
                (now.astimezone(UTC).isoformat(), MAX_LEARN_PER_CYCLE),
            ).fetchall()
            if not rows:
                return 0
            states: dict[tuple[str, int], dict] = {}
            learned = 0
            done: list[tuple] = []
            for row in rows:
                key = (str(row["asset"]), int(row["horizon_minutes"]))
                if key not in states:
                    states[key] = self._state(conn, *key)
                label = row["outcome_up"]
                if label in (0, 1):  # no-trade targets are withdrawn, not NOT-UP
                    state = states[key]
                    x = (1.0, *json.loads(row["features"]))
                    weights = state["weights"]
                    error = label - _sigmoid(sum(w * v for w, v in zip(weights, x, strict=True)))
                    for i, value in enumerate(x):
                        weights[i] += LEARNING_RATE * (error * value - (L2 * weights[i] if i else 0))
                    state["updates"] += 1
                    state["positives"] += int(label)
                    learned += 1
                done.append((row["venue"], row["contract"], row["quote"],
                             row["horizon_minutes"], row["reference_opened_at"]))
            # Mark learned rows in batches instead of one round trip per row
            # (up to 4,000 a cycle). Same rows, same transaction as the
            # weight update; plain OR of primary keys works on both backends.
            match = "(venue=? AND contract=? AND quote=? AND horizon_minutes=? AND reference_opened_at=?)"
            for offset in range(0, len(done), LEARNED_BATCH):
                chunk = done[offset:offset + LEARNED_BATCH]
                conn.execute(
                    f"UPDATE {self.features_table} SET learned=1 WHERE "
                    + " OR ".join(match for _ in chunk),
                    tuple(value for key in chunk for value in key),
                )
            stamp = now.astimezone(UTC).isoformat()
            self._states.clear()  # new weights: never serve a stale copy
            for (asset, horizon), state in states.items():
                conn.execute(
                    f"""INSERT INTO {self.state_table}
                       (asset,horizon_minutes,weights,updates,positives,updated_at)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(asset,horizon_minutes) DO UPDATE SET
                         weights=excluded.weights, updates=excluded.updates,
                         positives=excluded.positives, updated_at=excluded.updated_at""",
                    (asset, horizon, json.dumps(state["weights"]), state["updates"],
                     state["positives"], stamp),
                )
        return learned


class FlowChallengerV2(FlowChallenger):
    """v1's inputs plus OKX perp and Coinbase spot order flow and the
    spot-vs-perp basis. Runs beside v1 so the extra data has to prove it
    helps on the same graded outcomes.

    Extra inputs (7): OKX 5-min trade imbalance and 10 bps book imbalance,
    Coinbase spot 5-min trade imbalance and top-of-book imbalance, the
    Hyperliquid-perp-over-Coinbase-spot basis, and an availability flag for
    each of OKX and Coinbase.
    """

    family = "flow_v2"
    n_flow = FLOW_FEATURES + 7
    features_table = "rival_flow2_features"
    state_table = "rival_flow2_model_state"

    def asset_inputs(self, now: datetime, assets) -> dict[str, tuple[float, ...]]:
        base = super().asset_inputs(now, assets)
        stamp = now.astimezone(UTC)
        out = {}
        with self.store.connect() as conn:
            for asset in assets:
                latest = {}
                for venue in ("hyperliquid", "okx", "coinbase"):
                    row = conn.execute(
                        """SELECT complete, mid_last, bid_depth_10bps_mean,
                                  ask_depth_10bps_mean, buy_volume, sell_volume
                           FROM perp_liquidity_5m
                           WHERE venue=? AND asset=? AND recorded_at<=? AND bucket_start>=?
                           ORDER BY bucket_start DESC LIMIT 1""",
                        (venue, asset, stamp.isoformat(),
                         (stamp - timedelta(minutes=15)).isoformat()),
                    ).fetchone()
                    if row is not None and int(row["complete"] or 0) == 1:
                        latest[venue] = row
                okx, spot, perp = latest.get("okx"), latest.get("coinbase"), latest.get("hyperliquid")
                basis = 0.0
                if spot is not None and perp is not None:
                    s_mid, p_mid = float(spot["mid_last"] or 0), float(perp["mid_last"] or 0)
                    if s_mid > 0 and p_mid > 0:
                        basis = _clip((p_mid / s_mid - 1) * 1e4 / 5)
                extra = (
                    _ratio(okx["buy_volume"], okx["sell_volume"]) if okx else 0.0,
                    _ratio(okx["bid_depth_10bps_mean"], okx["ask_depth_10bps_mean"]) if okx else 0.0,
                    _ratio(spot["buy_volume"], spot["sell_volume"]) if spot else 0.0,
                    _ratio(spot["bid_depth_10bps_mean"], spot["ask_depth_10bps_mean"]) if spot else 0.0,
                    basis,
                    1.0 if okx else 0.0,
                    1.0 if spot else 0.0,
                )
                out[asset] = (*base[asset], *extra)
        return out
