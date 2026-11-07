"""Order-flow successor challenger (flow_v3): standardized inputs + Platt calibration.

A shadow model only. It never serves calls; it is graded on the same future
outcome as every other family in ``rival_model_shadow_forecasts`` and can
only ever serve through the existing promotion gates (Route B, per stream).

Why a new family: flow_v1/flow_v2 were retired 2026-11-05 for losing to
every venue's up-rate with ~78%-UP calls coming true 33-44% of the time
(``forward_audit.py`` retirement comment). The retiring comment prescribes
"a calibrated, input-standardized successor would be a new family, not
these" -- this is that successor.

What changes vs v1/v2 (spec §2, batch 2):
- Every continuous input is standardized causally: ``z = clip((x - mean) /
  std, -5, 5)`` with trailing mean/std computed from graded data available
  at issuance only, updated once per cycle in ``learn()``, stored in
  ``rival_flow3_model_state`` alongside the weights. The +/-5 clip matches
  the incumbent feature contract's ``|x| <= 5`` bound.
- Liquidations (OKX only) are read for the first time: the columns exist in
  ``perp_liquidity_5m`` and no model has ever read them. Coverage is
  OKX-only and must be disclosed in every receipt.
- Platt calibration (2-parameter logistic on the raw score), fit on a
  validation window disjoint from the evaluation window. The calibration
  gate (ECE <= 0.10 plus an explicit top-bucket hit-rate check) is the direct
  acceptance test for Flow's overconfidence failure. Until a fit passes its
  gate, the raw probability is served and calibration status is recorded as
  ``unevaluated`` -- never claim calibration you do not have.
- Skip rule: if no flow input is available for an (asset, horizon) in a
  cycle (all availability flags 0), issuance is skipped for that slot that
  cycle and a receipt is logged. No all-zero vector is ever issued.

What is reused unchanged from Flow v1: one online logistic per
(asset, horizon) pooled across venues; causal issuance (only buckets
recorded before issuance); exact-input persistence at issuance as JSON;
graded-only learning in settlement order (no-trade/withdrawn targets
skipped, nothing learned twice); shrinkage toward the up-rate until 300
graded results; one state read per cycle; savepoint isolation provided by
the ``forward_audit`` wiring. Hyperparameters start at Flow v1's values.

Calibration / promotion-window note (the P1-7 lesson): the Platt fit uses
only graded rows settled before the fit. The promotion gate scores the
probabilities that were fixed at issuance, so a later fit cannot rewrite
the scored record; the fit only changes probabilities issued afterwards.

Binance funding/OI are in-memory only (``binance_context._bundles``,
3-minute expiry). They are read live at issuance exactly as the pooled v2
``fund`` feature does, and never assumed to persist: a restart loses them
and the availability flag records that. No new collection is added.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import UTC, datetime, timedelta

_LOG = logging.getLogger(__name__)

FAMILY = "flow_v3"
# Raw continuous features, in order. The 10 incumbent price features precede
# these in the model input; the 5 availability flags follow them.
FLOW_FEATURE_NAMES = (
    "binance_funding_bps",
    "hl_funding_bps",
    "binance_oi_1h_change",
    "hl_oi_log_ratio",
    "hl_depth_imb_10bps",
    "hl_depth_imb_25bps",
    "okx_depth_imb_10bps",
    "okx_depth_imb_25bps",
    "hl_tape_imb_5m",
    "hl_tape_imb_15m",
    "okx_tape_imb_5m",
    "okx_tape_imb_15m",
    "coinbase_tape_imb_5m",
    "coinbase_tape_imb_15m",
    "okx_liq_net_imb",
    "okx_liq_share",
    "hl_spread_bps",
)
N_FLOW_RAW = len(FLOW_FEATURE_NAMES)  # 17
# Per-source availability flags, in order, appended after the raw features.
FLAG_NAMES = ("binance", "hl_context", "hl_liquidity", "okx", "coinbase")
N_FLAGS = len(FLAG_NAMES)  # 5
N_FLOW = N_FLOW_RAW + N_FLAGS  # 22
N_PRICE = 10
N_INPUTS = N_PRICE + N_FLOW  # 32 model inputs (+ bias = 33 weights)
# Feature contract digest: the artifact identity. Any change to the feature
# list, order, or contract version changes this digest, so a promotion earned
# under one contract cannot serve under another.
FEATURE_CONTRACT = "flow-v3-price10+flow17+flags5-v1"
FEATURE_DIGEST = hashlib.sha256(
    json.dumps([FEATURE_CONTRACT, *FLOW_FEATURE_NAMES, *FLAG_NAMES],
               separators=(",", ":")).encode()
).hexdigest()
LEARNING_RATE = 0.02
L2 = 1e-4
SHRINK_HALF = 300  # graded results at which the model gets half its own say
MAX_LEARN_PER_CYCLE = 4000
LEARNED_BATCH = 100  # 5 keys each: 500 parameters, under SQLite's 999
# Platt calibration: fit on the older graded rows, gate on the newer ones.
MIN_FIT_ROWS = 30
MIN_EVAL_ROWS = 30
MAX_CALIBRATION_ROWS = 1000
MAX_ECE = 0.10
TOP_BUCKET_MIN_ROWS = 10
# Liquidations are OKX-only live; every receipt must disclose that.
LIQUIDATION_COVERAGE = "OKX-only"


def _schema(features: str, state: str) -> tuple[str, ...]:
    return (
        f"""CREATE TABLE IF NOT EXISTS {features} (
            venue TEXT NOT NULL, contract TEXT NOT NULL, quote TEXT NOT NULL,
            horizon_minutes INTEGER NOT NULL, reference_opened_at TEXT NOT NULL,
            asset TEXT NOT NULL, issued_at TEXT NOT NULL, features TEXT NOT NULL,
            raw_features TEXT NOT NULL,
            learned INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (venue,contract,quote,horizon_minutes,reference_opened_at)
        )""",
        f"""CREATE INDEX IF NOT EXISTS idx_{features}_pending
            ON {features} (learned, issued_at)""",
        f"""CREATE TABLE IF NOT EXISTS {state} (
            asset TEXT NOT NULL, horizon_minutes INTEGER NOT NULL,
            weights TEXT NOT NULL, updates INTEGER NOT NULL, positives INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            stats_json TEXT NOT NULL DEFAULT '{{}}',
            calibration_json TEXT NOT NULL DEFAULT '{{}}',
            feature_digest TEXT NOT NULL DEFAULT '',
            weights_sha256 TEXT NOT NULL DEFAULT '',
            trained_through TEXT,
            graded_count INTEGER NOT NULL DEFAULT 0,
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


def _logit(p: float) -> float:
    p = max(1e-6, min(1 - 1e-6, p))
    return math.log(p / (1 - p))


def _weights_sha256(weights) -> str:
    return hashlib.sha256(json.dumps(
        list(weights), separators=(",", ":")).encode()).hexdigest()


def _empty_stats() -> dict:
    return {"count": 0, "mean": [0.0] * (N_PRICE + N_FLOW_RAW),
            "m2": [0.0] * (N_PRICE + N_FLOW_RAW)}


def _standardize(raw, stats: dict) -> tuple[float, ...]:
    """Causal z-scores from trailing graded-data stats; identity pre-data."""
    count = int(stats.get("count") or 0)
    mean = stats.get("mean") or [0.0] * len(raw)
    m2 = stats.get("m2") or [0.0] * len(raw)
    out = []
    for i, value in enumerate(raw):
        if count < 2:
            out.append(_clip(float(value)))
            continue
        var = m2[i] / count
        std = math.sqrt(var) if var > 0 else 0.0
        out.append(_clip((float(value) - mean[i]) / std) if std > 1e-9
                   else _clip(float(value) - mean[i]))
    return tuple(out)


def _welford_update(stats: dict, raw) -> None:
    count = int(stats.get("count") or 0) + 1
    mean = list(stats.get("mean") or [0.0] * len(raw))
    m2 = list(stats.get("m2") or [0.0] * len(raw))
    for i, value in enumerate(raw):
        delta = float(value) - mean[i]
        mean[i] += delta / count
        m2[i] += delta * (float(value) - mean[i])
    stats["count"] = count
    stats["mean"] = mean
    stats["m2"] = m2


def _ece(rows) -> float | None:
    """Expected calibration error over 10 bins for (probability, label)."""
    if not rows:
        return None
    bins = [{"n": 0, "sum_p": 0.0, "wins": 0} for _ in range(10)]
    for p, label in rows:
        bucket = bins[min(9, int(p * 10))]
        bucket["n"] += 1
        bucket["sum_p"] += p
        bucket["wins"] += int(label)
    n = len(rows)
    return sum((b["n"] / n) * abs(b["sum_p"] / b["n"] - b["wins"] / b["n"])
               for b in bins if b["n"])


def _top_bucket(rows):
    """Hit rate of the highest-confidence calls: the highest occupied
    decile of max(p, 1-p).

    The direct acceptance test for Flow's failure mode (~78%-UP calls true
    33-44%): a model whose most confident calls are wrong must not pass.
    The bucket is the highest decile that actually has calls, so a model
    whose best calls sit at 78% is judged on those calls, not on an empty
    90%+ bucket.
    """
    by_decile: dict[int, list] = {}
    for p, label in rows:
        decile = min(9, int(max(p, 1 - p) * 10))
        by_decile.setdefault(decile, []).append((p, label))
    if not by_decile:
        return {"n": 0, "hit_rate": None, "mean_confidence": None}
    top = by_decile[max(by_decile)]
    hits = sum(1 for p, label in top if (p >= 0.5) == bool(label))
    return {"n": len(top), "hit_rate": hits / len(top),
            "mean_confidence": sum(max(p, 1 - p) for p, _ in top) / len(top)}


def fit_platt(fit_rows, eval_rows) -> dict:
    """Fit Platt scaling on ``fit_rows``, gate it on disjoint ``eval_rows``.

    Rows are (raw_probability, label). The fit window and the evaluation
    window must be disjoint; the caller splits chronologically so the fit
    never sees evaluation data (the P1-7 lesson).
    """
    if len(fit_rows) < MIN_FIT_ROWS or len(eval_rows) < MIN_EVAL_ROWS:
        return {"method": "platt", "status": "unevaluated",
                "reason": "insufficient graded rows for a validation split",
                "fit_rows": len(fit_rows), "eval_rows": len(eval_rows),
                "a": None, "b": None, "ece": None,
                "top_bucket_hit_rate": None, "top_bucket_n": 0}
    a, b = 1.0, 0.0
    scores = [(_logit(p), label) for p, label in fit_rows]
    for _ in range(200):
        grad_a = grad_b = 0.0
        for score, label in scores:
            pred = _sigmoid(a * score + b)
            err = pred - label
            grad_a += err * score
            grad_b += err
        n = len(scores)
        a -= 0.1 * grad_a / n
        b -= 0.1 * grad_b / n
        a = max(0.05, min(20.0, a))
    calibrated = [(_sigmoid(a * _logit(p) + b), label) for p, label in eval_rows]
    ece = _ece(calibrated)
    top = _top_bucket(calibrated)
    top_ok = (top["n"] < TOP_BUCKET_MIN_ROWS or (
        top["hit_rate"] is not None and top["mean_confidence"] is not None
        and abs(top["mean_confidence"] - top["hit_rate"]) <= MAX_ECE))
    passes = ece is not None and ece <= MAX_ECE and top_ok
    return {"method": "platt",
            "status": "evaluated" if passes else "failed_gate",
            "reason": ("Platt fit passed ECE + top-bucket gate on the "
                       "disjoint evaluation window" if passes else
                       "Platt fit failed the ECE / top-bucket gate; "
                       "raw probabilities are served instead"),
            "fit_rows": len(fit_rows), "eval_rows": len(eval_rows),
            "a": a, "b": b, "ece": ece,
            "top_bucket_hit_rate": top["hit_rate"], "top_bucket_n": top["n"],
            "top_bucket_mean_confidence": top["mean_confidence"]}


def calibration_gate(state: dict, eval_rows) -> dict:
    """ECE + top-bucket check of the state's calibration on ``eval_rows``."""
    calibration = state.get("calibration") or {}
    if calibration.get("status") != "evaluated":
        return {"passes": False, "status": calibration.get("status", "unevaluated"),
                "ece": None, "top_bucket_hit_rate": None, "top_bucket_n": 0}
    a, b = float(calibration["a"]), float(calibration["b"])
    calibrated = [(_sigmoid(a * _logit(p) + b), label) for p, label in eval_rows]
    ece = _ece(calibrated)
    top = _top_bucket(calibrated)
    top_ok = (top["n"] < TOP_BUCKET_MIN_ROWS or (
        top["hit_rate"] is not None and top["mean_confidence"] is not None
        and abs(top["mean_confidence"] - top["hit_rate"]) <= MAX_ECE))
    passes = ece is not None and ece <= MAX_ECE and top_ok
    return {"passes": passes, "status": "evaluated", "ece": ece,
            "top_bucket_hit_rate": top["hit_rate"], "top_bucket_n": top["n"],
            "top_bucket_mean_confidence": top["mean_confidence"]}


class FlowChallengerV3:
    family = FAMILY
    n_flow = N_FLOW
    features_table = "rival_flow3_features"
    state_table = "rival_flow3_model_state"

    def __init__(self, store, binance_context=None) -> None:
        self.store = store
        # Live Binance bundles (in-memory, 3-min expiry); None means the
        # Binance source is unavailable and its flag stays 0.0. Set by the
        # forward-audit wiring from the market's BinanceContextFeed.
        self._binance_context = binance_context
        self._ready = False
        # One instance serves one forecast cycle. Weights only change in
        # learn(), which runs after every forecast of the cycle is issued, so
        # each (asset, horizon) state is read once per cycle, not per forecast.
        self._states: dict[tuple[str, int], dict] = {}

    def initialize(self) -> None:
        if self._ready:
            return
        from parlay_bot.perpetuals.liquidity_record import (
            SCHEMA as LIQUIDITY_SCHEMA,
        )
        from parlay_bot.perpetuals.liquidity_record import (
            ensure_liquidation_columns,
        )
        from parlay_bot.perpetuals.market_store import PerpetualMarketStore

        PerpetualMarketStore(self.store).initialize()  # contexts + forecasts' inputs
        with self.store.connect() as conn:
            conn.execute(LIQUIDITY_SCHEMA)  # same definition the recorder uses
            ensure_liquidation_columns(self.store, conn)  # pre-Nov-4 tables
            for statement in _schema(self.features_table, self.state_table):
                conn.execute(statement)
        self._ready = True

    # ---------- inputs ----------
    def asset_inputs(self, now: datetime, assets) -> dict[str, tuple[float, ...]]:
        """One causal flow vector per asset for this cycle.

        Returns only assets with at least one available source (skip rule):
        an asset whose flags are all 0 is omitted, so no forecast is issued
        for it this cycle, and the skip is logged as a receipt.
        """
        self.initialize()
        stamp = now.astimezone(UTC)
        out: dict[str, tuple[float, ...]] = {}
        with self.store.connect() as conn:
            for asset in assets:
                buckets: dict[str, list] = {}
                for venue in ("hyperliquid", "okx", "coinbase"):
                    buckets[venue] = conn.execute(
                        """SELECT bucket_start, complete, spread_bps_mean,
                                  bid_depth_10bps_mean, ask_depth_10bps_mean,
                                  bid_depth_25bps_mean, ask_depth_25bps_mean,
                                  buy_volume, sell_volume,
                                  long_liq_volume, short_liq_volume
                           FROM perp_liquidity_5m
                           WHERE venue=? AND asset=? AND recorded_at<=?
                             AND bucket_start>=?
                           ORDER BY bucket_start DESC LIMIT 3""",
                        (venue, asset, stamp.isoformat(),
                         (stamp - timedelta(minutes=25)).isoformat()),
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
                bundle = None
                if self._binance_context is not None:
                    try:
                        bundle = self._binance_context.bundle(asset)
                    except Exception:  # noqa: BLE001 - stale/missing bundle is a flag, not a crash
                        bundle = None
                vector = self._vector(buckets, contexts, bundle, stamp)
                if not any(vector[N_FLOW_RAW:]):
                    _LOG.info("RIVAL_FLOW_V3_SKIP asset=%s reason=no_flow_inputs "
                              "coverage=binance,hyperliquid,okx,coinbase all unavailable",
                              asset)
                    continue
                out[asset] = vector
        return out

    @staticmethod
    def _vector(buckets, contexts, bundle, stamp: datetime) -> tuple[float, ...]:
        """17 raw flow features + 5 per-source availability flags."""
        usable = {v: [b for b in rows if int(b["complete"] or 0) == 1]
                  for v, rows in buckets.items()}

        def tape(venue: str, minutes: int) -> float:
            # 5m: the latest usable bucket (v1 semantics). 15m: all usable
            # buckets starting within the window. Buckets are only queried
            # 25 minutes back, so staleness is bounded either way.
            rows = usable.get(venue, [])
            if not rows:
                return 0.0
            if minutes <= 5:
                rows = rows[:1]
            else:
                rows = [b for b in rows
                        if _when(b["bucket_start"]) >= stamp - timedelta(minutes=minutes)]
            if not rows:
                return 0.0
            return _ratio(sum(float(b["buy_volume"] or 0) for b in rows),
                          sum(float(b["sell_volume"] or 0) for b in rows))

        def depth(venue: str, band: str) -> float:
            rows = usable.get(venue, [])
            if not rows:
                return 0.0
            last = rows[0]
            return _ratio(last[f"bid_depth_{band}bps_mean"],
                          last[f"ask_depth_{band}bps_mean"])

        hl_rows = usable.get("hyperliquid", [])
        okx_rows = usable.get("okx", [])
        cb_rows = usable.get("coinbase", [])
        hl_depth_10 = depth("hyperliquid", "10")
        hl_depth_25 = depth("hyperliquid", "25")
        okx_depth_10 = depth("okx", "10")
        okx_depth_25 = depth("okx", "25")
        hl_tape_5 = tape("hyperliquid", 5)
        hl_tape_15 = tape("hyperliquid", 15)
        okx_tape_5 = tape("okx", 5)
        okx_tape_15 = tape("okx", 15)
        cb_tape_5 = tape("coinbase", 5)
        cb_tape_15 = tape("coinbase", 15)
        hl_spread = float(hl_rows[0]["spread_bps_mean"] or 0) if hl_rows else 0.0
        # Liquidations are OKX-only live. A complete OKX bucket with zero
        # liquidations is a real observation (flag 1.0, values 0.0); a missing
        # bucket is not (flag 0.0). The OKX-only coverage is disclosed in
        # every receipt.
        liq_net = liq_share = 0.0
        if okx_rows:
            last = okx_rows[0]
            long_liq = float(last["long_liq_volume"] or 0)
            short_liq = float(last["short_liq_volume"] or 0)
            total_liq = long_liq + short_liq
            liq_net = (long_liq - short_liq) / total_liq if total_liq > 0 else 0.0
            volume = float(last["buy_volume"] or 0) + float(last["sell_volume"] or 0)
            liq_share = total_liq / volume if volume > 0 else 0.0

        # Hyperliquid derivatives context (persisted perp_market_contexts).
        hl_funding = hl_oi = 0.0
        hl_ctx_available = False
        if contexts:
            now_ctx = contexts[0]
            if stamp - _when(now_ctx["observed_at"]) <= timedelta(minutes=5):
                hl_funding = float(now_ctx["funding_rate"] or 0) * 1e4
                hl_ctx_available = True
            hour_ago = [c for c in contexts
                        if _when(c["observed_at"]) <= stamp - timedelta(minutes=55)]
            if hour_ago:
                before = float(hour_ago[0]["open_interest_base"] or 0)
                after = float(now_ctx["open_interest_base"] or 0)
                if before > 0 and after > 0:
                    hl_oi = math.log(after / before)
                    hl_ctx_available = True

        # Binance funding/OI from the live in-memory bundle (never persisted).
        bin_funding = bin_oi = 0.0
        bin_available = False
        if bundle:
            from parlay_bot.perpetuals.pooled_features import funding_known_at

            known = [rate for when, rate in bundle.get("funding", ())
                     if funding_known_at(when) <= stamp]
            if known:
                bin_funding = float(known[-1]) * 1e4
                bin_available = True
            oi = [(when, value) for when, value in bundle.get("open_interest", ())
                  if when <= stamp and when >= stamp - timedelta(minutes=65)]
            if len(oi) >= 2 and oi[-1][0] - oi[0][0] >= timedelta(minutes=50) \
                    and float(oi[0][1]) > 0:
                bin_oi = float(oi[-1][1]) / float(oi[0][1]) - 1
                bin_available = True

        flags = (
            1.0 if bin_available else 0.0,
            1.0 if hl_ctx_available else 0.0,
            1.0 if hl_rows else 0.0,
            1.0 if okx_rows else 0.0,
            1.0 if cb_rows else 0.0,
        )
        return (bin_funding, hl_funding, bin_oi, hl_oi,
                hl_depth_10, hl_depth_25, okx_depth_10, okx_depth_25,
                hl_tape_5, hl_tape_15, okx_tape_5, okx_tape_15,
                cb_tape_5, cb_tape_15, liq_net, liq_share, hl_spread, *flags)

    # ---------- model ----------
    def _state(self, conn, asset: str, horizon: int) -> dict:
        row = conn.execute(
            f"SELECT weights, updates, positives, stats_json, calibration_json, "
            f"feature_digest, weights_sha256, trained_through, graded_count "
            f"FROM {self.state_table} WHERE asset=? AND horizon_minutes=?",
            (asset, horizon)).fetchone()
        if row is None:
            return {"weights": [0.0] * (N_INPUTS + 1), "updates": 0, "positives": 0,
                    "stats": _empty_stats(),
                    "calibration": {"method": "platt", "status": "unevaluated"},
                    "feature_digest": FEATURE_DIGEST, "weights_sha256": None,
                    "trained_through": None, "graded_count": 0}
        weights = json.loads(row["weights"])
        if len(weights) != N_INPUTS + 1:
            weights = [0.0] * (N_INPUTS + 1)
        try:
            stats = json.loads(row["stats_json"] or "{}") or {}
        except ValueError:
            stats = {}
        if not stats or len(stats.get("mean", [])) != N_PRICE + N_FLOW_RAW:
            stats = _empty_stats()
        try:
            calibration = json.loads(row["calibration_json"] or "{}") or {}
        except ValueError:
            calibration = {}
        if not calibration:
            calibration = {"method": "platt", "status": "unevaluated"}
        return {"weights": weights, "updates": int(row["updates"]),
                "positives": int(row["positives"]), "stats": stats,
                "calibration": calibration,
                "feature_digest": row["feature_digest"] or FEATURE_DIGEST,
                "weights_sha256": row["weights_sha256"] or None,
                "trained_through": row["trained_through"],
                "graded_count": int(row["graded_count"] or 0)}

    @staticmethod
    def raw_probability(state: dict, inputs) -> float:
        """Shrunk sigmoid on the standardized inputs (pre-calibration)."""
        x = (1.0, *inputs)
        raw = _sigmoid(sum(w * v for w, v in zip(state["weights"], x, strict=True)))
        n = state["updates"]
        base = (state["positives"] + 5) / (n + 10)
        trust = n / (n + SHRINK_HALF)
        return max(0.02, min(0.98, base + trust * (raw - base)))

    @classmethod
    def probability(cls, state: dict, inputs) -> float:
        """Served probability: Platt-calibrated only when the fit passed its
        gate; the raw shrunk probability otherwise, never a claimed
        calibration."""
        raw = cls.raw_probability(state, inputs)
        calibration = state.get("calibration") or {}
        if calibration.get("status") != "evaluated":
            return raw
        try:
            calibrated = _sigmoid(float(calibration["a"]) * _logit(raw)
                                  + float(calibration["b"]))
        except (TypeError, ValueError, OverflowError):
            return raw
        return max(0.02, min(0.98, calibrated))

    def call(self, conn, *, venue, contract, quote, asset, horizon,
             reference_opened_at, issued_at, features, flow) -> dict | None:
        """Persist this forecast's exact inputs and return the shadow call.

        ``features`` is the incumbent's 10 causal price features; ``flow``
        is the 17 raw flow features + 5 availability flags from
        ``asset_inputs``. Standardization uses only trailing graded-data
        stats read at the start of this cycle. An all-flags-zero vector is
        refused (skip rule, defense in depth behind ``asset_inputs``).
        """
        if flow is None or len(features) != N_PRICE or len(flow) != self.n_flow:
            return None
        if not any(flow[N_FLOW_RAW:]):
            return None
        raw = tuple(float(v) for v in (*features, *flow[:N_FLOW_RAW]))
        flags = tuple(float(v) for v in flow[N_FLOW_RAW:])
        key = (asset, horizon)
        if key not in self._states:
            self._states[key] = self._state(conn, asset, horizon)
        state = self._states[key]
        inputs = (*_standardize(raw, state["stats"]), *flags)
        conn.execute(
            f"""INSERT INTO {self.features_table}
               (venue,contract,quote,horizon_minutes,reference_opened_at,asset,
                issued_at,features,raw_features) VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT DO NOTHING""",
            (venue, contract, quote, horizon, reference_opened_at.isoformat(),
             asset, issued_at.isoformat(),
             json.dumps(inputs, separators=(",", ":")),
             json.dumps(raw, separators=(",", ":"))),
        )
        return {
            "family": self.family,
            "probability": self.probability(state, inputs),
            "training_rows": state["updates"],
            "model_sha256": _weights_sha256(state["weights"]),
        }

    def learn(self, now: datetime) -> int:
        """Train on forecasts whose real outcome has since been graded."""
        self.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                f"""SELECT x.venue,x.contract,x.quote,x.horizon_minutes,
                          x.reference_opened_at,x.asset,x.features,x.raw_features,
                          f.outcome_up,f.settled_at
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
            trained_through: dict[tuple[str, int], str] = {}
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
                    state["graded_count"] = state["updates"]
                    trained_through[key] = max(
                        trained_through.get(key, ""), str(row["settled_at"]))
                    try:
                        _welford_update(state["stats"], json.loads(row["raw_features"]))
                    except (ValueError, TypeError):
                        pass
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
                if (asset, horizon) in trained_through:
                    state["trained_through"] = trained_through[(asset, horizon)]
                conn.execute(
                    f"""INSERT INTO {self.state_table}
                       (asset,horizon_minutes,weights,updates,positives,updated_at,
                        stats_json,calibration_json,feature_digest,weights_sha256,
                        trained_through,graded_count)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(asset,horizon_minutes) DO UPDATE SET
                         weights=excluded.weights, updates=excluded.updates,
                         positives=excluded.positives, updated_at=excluded.updated_at,
                         stats_json=excluded.stats_json,
                         calibration_json=excluded.calibration_json,
                         feature_digest=excluded.feature_digest,
                         weights_sha256=excluded.weights_sha256,
                         trained_through=excluded.trained_through,
                         graded_count=excluded.graded_count""",
                    (asset, horizon, json.dumps(state["weights"]), state["updates"],
                     state["positives"], stamp, json.dumps(state["stats"]),
                     json.dumps(state["calibration"]), FEATURE_DIGEST,
                     _weights_sha256(state["weights"]), state["trained_through"],
                     state["graded_count"]),
                )
            # Refit Platt calibration from graded history only, per touched
            # (asset, horizon): older graded rows fit, newer graded rows gate.
            # The fit never sees evaluation data, and it only changes
            # probabilities issued afterwards -- never the scored record.
            for key, state in states.items():
                self._refit_calibration(conn, key, state, stamp)
        return learned

    def _refit_calibration(self, conn, key, state: dict, stamp: str) -> None:
        graded = conn.execute(
            f"""SELECT x.features, f.outcome_up
               FROM {self.features_table} x
               JOIN perp_forward_audit f
                 ON f.venue=x.venue AND f.contract=x.contract AND f.quote=x.quote
                AND f.horizon_minutes=x.horizon_minutes
                AND f.reference_opened_at=x.reference_opened_at
               WHERE x.asset=? AND x.horizon_minutes=? AND x.learned=1
                 AND f.settled_at IS NOT NULL AND f.outcome_up IN (0,1)
               ORDER BY f.settled_at, x.issued_at""",
            (key[0], key[1]),
        ).fetchall()
        if len(graded) < MIN_FIT_ROWS + MIN_EVAL_ROWS:
            return
        graded = graded[-MAX_CALIBRATION_ROWS:]
        split = int(len(graded) * 0.7)
        fit_rows = [(self.raw_probability(state, json.loads(r["features"])),
                     int(r["outcome_up"])) for r in graded[:split]]
        eval_rows = [(self.raw_probability(state, json.loads(r["features"])),
                      int(r["outcome_up"])) for r in graded[split:]]
        result = fit_platt(fit_rows, eval_rows)
        result["fit_window"] = {"graded_rows": len(fit_rows)}
        result["eval_window"] = {"graded_rows": len(eval_rows)}
        state["calibration"] = result
        conn.execute(
            f"""UPDATE {self.state_table} SET calibration_json=?, updated_at=?
               WHERE asset=? AND horizon_minutes=?""",
            (json.dumps(result), stamp, key[0], key[1]),
        )
        _LOG.info("RivaL flow_v3 calibration asset=%s horizon=%s status=%s "
                  "ece=%s top_bucket_hit_rate=%s",
                  key[0], key[1], result["status"], result.get("ece"),
                  result.get("top_bucket_hit_rate"))

    # ---------- artifact identity (for FamilyPromotion.selected) ----------
    @classmethod
    def artifact_identity(cls, store, asset: str, horizon: int) -> dict | None:
        """Currently-trained artifact for (asset, horizon), or None.

        Weights SHA is recomputed from the stored weights (never trusted
        from the stored column), so a tampered row fails its own check.
        """
        challenger = cls(store)
        challenger.initialize()
        with store.connect() as conn:
            state = challenger._state(conn, asset, horizon)
        if state["updates"] == 0 and state["trained_through"] is None:
            return None
        return {"feature_digest": state["feature_digest"],
                "weights_sha256": _weights_sha256(state["weights"]),
                "trained_through": state["trained_through"],
                "graded_count": state["graded_count"]}

    @classmethod
    def calibration_status(cls, store, asset: str, horizon: int) -> dict:
        """Calibration record for receipts; unevaluated until a fit passes."""
        challenger = cls(store)
        challenger.initialize()
        with store.connect() as conn:
            state = challenger._state(conn, asset, horizon)
        calibration = dict(state.get("calibration") or {})
        calibration.setdefault("method", "platt")
        calibration.setdefault("status", "unevaluated")
        calibration["liquidation_coverage"] = LIQUIDATION_COVERAGE
        return calibration
