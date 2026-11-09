"""Live liquidity challenger (liq_v1): standardized inputs + Platt calibration.

Spec §2.10.2, Phase 3. A shadow model only. It never serves calls; it
is graded on the same future outcome as every other family in
``rival_model_shadow_forecasts`` and can only ever serve through the
existing promotion gates (Route B, per stream).

Why a new family: the history gap blocks *training* a liquidity model
offline, not *learning* one live. liq_v1 learns live from the buckets
being recorded right now, needs no history to start, shadow-issues from
day one, and promotes only through Route B. The backfill (Track A)
feeds it later; it does not gate it now.

What it consumes (spec §2.10.2, verified at ``0cee87c``): 10 incumbent
price features + 21 flow features + 6 availability flags = 37 inputs
(+ bias = 38 weights). The flow definitions are the flow_v3 ``_vector``
definitions, reused. The 4 Binance-bucket features are new: Binance
buckets are recorded by the recorder (``liquidity_record.VENUES``
includes ``"binance"``) but queried by NO model reader at ``0cee87c``;
they are added as a flagged group, so an empty group costs nothing and
a populated one is used. Whether prod Binance buckets are actually
populated is UNVERIFIED (the feed-health row settles it, spec §7 item
24) -- the flag decides per cycle.

Coinbase tape is SPOT, labelled as such in every feature name, receipt,
and feature list: ``cb_spot_tape_imb_*`` is basis context only, never
filed or presented as perp tape (spec §6 spot rule). Liquidations are
OKX-only live; a complete OKX bucket with zero liquidations is a real
observation (values 0.0), a missing bucket is not.

Learning: per (asset, horizon) online logistic on graded rows only --
causal standardisation, shrinkage toward the up-rate, Platt fitted on a
validation window of graded rows with the ECE <= 0.10 + top-bucket gate
(the flow_v3 learning pattern, reused). The shrink half-life starts at
the flow precedent scale (300 graded results to half trust); the trust
weight itself is computed from liq_v1's OWN graded count, never
flow_v3's, and the scale is re-derived from liq_v1's own graded history
once it exists -- that re-derivation is an open research item, not a
copied fit. Skip rule: all flags 0 -> no issuance that cycle, receipt
logged. Settlement copies the verified incumbent outcome by exact key
match; no-trade withdrawal mirrors the incumbent ledger.

"Starts young": the family has days of history, not years. It cannot
promote before ~21 days of issuance + 150 targets (Route B minimums);
early Brier numbers on < 50 targets are noise and must never be
presented as a track record; the first evaluation is a finding even if
it loses. Kill switch: ``ISSUE_LIQ_V1_CHALLENGERS`` in
``forward_audit``; add ``"liq_v1"`` to ``BLOCKED_FAMILIES`` to stop
serving (Felix's explicit approval required, as coded). Existing rows
keep settling either way. OTG: the D11 rule applies -- the OTG call
only, no live lean at launch (``live_lean`` returns None for any
``CHALLENGER_PREFIXES`` family, which includes ``liq_``).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import UTC, datetime, timedelta

_LOG = logging.getLogger(__name__)

FAMILY = "liq_v1"
MODEL_VERSION = "liq-v1"
# Raw continuous features, in order. The 10 incumbent price features precede
# these in the model input; the 6 availability flags follow them.
FLOW_FEATURE_NAMES = (
    "binance_funding_bps",
    "hl_funding_bps",
    "binance_oi_1h_change",
    "hl_oi_log_ratio",
    "hl_depth_imb_10bps",
    "hl_depth_imb_25bps",
    "okx_depth_imb_10bps",
    "okx_depth_imb_25bps",
    # New in liq_v1 (spec §2.10.2): Binance buckets are recorded but were
    # read by no model at 0cee87c; they enter as a flagged group.
    "bin_depth_imb_10bps",
    "bin_depth_imb_25bps",
    "hl_tape_imb_5m",
    "hl_tape_imb_15m",
    "okx_tape_imb_5m",
    "okx_tape_imb_15m",
    "bin_tape_imb_5m",
    "bin_tape_imb_15m",
    "okx_liq_net_imb",
    "okx_liq_share",
    "hl_spread_bps",
    # Coinbase spot tape: labelled SPOT in the feature name itself, so the
    # label cannot be dropped from any receipt or feature list. Basis
    # context only, never perp tape.
    "cb_spot_tape_imb_5m",
    "cb_spot_tape_imb_15m",
)
N_FLOW_RAW = len(FLOW_FEATURE_NAMES)  # 21
# Per-source availability flags, in order, appended after the raw features.
# Binance is split: the in-memory funding/OI bundle vs the recorded buckets.
FLAG_NAMES = ("binance_bundle", "hl_context", "hl_liquidity", "okx",
              "coinbase", "binance_buckets")
N_FLAGS = len(FLAG_NAMES)  # 6
N_FLOW = N_FLOW_RAW + N_FLAGS  # 27
N_PRICE = 10
N_INPUTS = N_PRICE + N_FLOW  # 37 model inputs (+ bias = 38 weights)
# Feature contract digest: any change to the feature list, order, or
# contract version changes this digest, so a promotion earned under one
# contract cannot serve under another.
FEATURE_CONTRACT = "liq-price10+flow21+flags6-v1"
FEATURE_DIGEST = hashlib.sha256(
    json.dumps([FEATURE_CONTRACT, *FLOW_FEATURE_NAMES, *FLAG_NAMES],
               separators=(",", ":")).encode()
).hexdigest()
LEARNING_RATE = 0.02
L2 = 1e-4
# Shrink half-life: the graded count at which the model gets half its own
# say (trust = n / (n + SHRINK_HALF)). Starting scale is the flow
# precedent (300); the trust is computed from liq_v1's OWN graded count
# (state["updates"]), never flow_v3's. Re-deriving the scale from liq_v1's
# own graded history is an open research item once graded rows exist.
SHRINK_HALF = 300
MAX_LEARN_PER_CYCLE = 4000
LEARNED_BATCH = 100  # 5 keys each: 500 parameters, under SQLite's 999
# Platt calibration: fit on the older graded rows, gate on the newer ones.
MIN_FIT_ROWS = 30
MIN_EVAL_ROWS = 30
MAX_CALIBRATION_ROWS = 1000
MAX_ECE = 0.10
TOP_BUCKET_MIN_ROWS = 10
# Liquidations are OKX-only live; Coinbase tape is spot, never perp.
LIQUIDATION_COVERAGE = "OKX-only"
SPOT_COVERAGE = "Coinbase spot tape (basis context only, never perp tape)"


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
        # Serves the per-(asset, horizon) calibration refit, which otherwise
        # scans every stored feature row once per key per cycle.
        f"""CREATE INDEX IF NOT EXISTS idx_{features}_scope
            ON {features} (asset, horizon_minutes, learned)""",
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
    decile of max(p, 1-p). The direct acceptance test for Flow's failure
    mode (~78%-UP calls true 33-44%): a model whose most confident calls
    are wrong must not pass."""
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

class LiquidityChallengerV1:
    family = FAMILY
    n_flow = N_FLOW
    features_table = "rival_liq1_features"
    state_table = "rival_liq1_model_state"

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
                for venue in ("hyperliquid", "okx", "coinbase", "binance"):
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
                    _LOG.info("RIVAL_LIQ_V1_SKIP asset=%s reason=no_flow_inputs "
                              "coverage=binance,hyperliquid,okx,coinbase all unavailable",
                              asset)
                    continue
                out[asset] = vector
        return out

    @staticmethod
    def _vector(buckets, contexts, bundle, stamp: datetime) -> tuple[float, ...]:
        """21 raw flow features + 6 per-source availability flags.

        The flow definitions are the flow_v3 ``_vector`` definitions,
        reused (spec §2.10.2), plus the Binance bucket group (depth and
        tape from the recorded Binance buckets, which no model read at
        ``0cee87c``). Coinbase tape is spot, named as such.
        """
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
        bin_rows = usable.get("binance", [])
        hl_depth_10 = depth("hyperliquid", "10")
        hl_depth_25 = depth("hyperliquid", "25")
        okx_depth_10 = depth("okx", "10")
        okx_depth_25 = depth("okx", "25")
        bin_depth_10 = depth("binance", "10")
        bin_depth_25 = depth("binance", "25")
        hl_tape_5 = tape("hyperliquid", 5)
        hl_tape_15 = tape("hyperliquid", 15)
        okx_tape_5 = tape("okx", 5)
        okx_tape_15 = tape("okx", 15)
        bin_tape_5 = tape("binance", 5)
        bin_tape_15 = tape("binance", 15)
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
            1.0 if bin_rows else 0.0,
        )
        return (bin_funding, hl_funding, bin_oi, hl_oi,
                hl_depth_10, hl_depth_25, okx_depth_10, okx_depth_25,
                bin_depth_10, bin_depth_25,
                hl_tape_5, hl_tape_15, okx_tape_5, okx_tape_15,
                bin_tape_5, bin_tape_15,
                liq_net, liq_share, hl_spread,
                cb_tape_5, cb_tape_15, *flags)

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
        """Shrunk sigmoid on the standardized inputs (pre-calibration).

        The shrink trust uses this family's OWN graded count
        (``state["updates"]``): a young family stays near the up-rate
        until its own graded record earns it a say.
        """
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
        is the 21 raw flow features + 6 availability flags from
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
        # Only the newest MAX_CALIBRATION_ROWS graded rows are ever used, so
        # fetch just those (newest first, then back to oldest-first) instead
        # of the whole, ever-growing graded history every cycle. Same rows,
        # same order; the key columns only break exact timestamp ties.
        # The audit side is scoped to the same (asset, horizon) as well: a
        # feature row and its audit row are written in one transaction with
        # the same asset, so the rows are unchanged, but Postgres can now use
        # idx_perp_forward_asset_horizon instead of hashing the whole audit
        # table once per key (slow SELECT / held connection, ~10 an hour).
        graded = conn.execute(
            f"""SELECT features, outcome_up FROM (
                 SELECT x.features, f.outcome_up, f.settled_at AS s_at,
                        x.issued_at AS i_at, x.venue AS v, x.contract AS c,
                        x.quote AS q
                 FROM {self.features_table} x
                 JOIN perp_forward_audit f
                   ON f.venue=x.venue AND f.contract=x.contract AND f.quote=x.quote
                  AND f.horizon_minutes=x.horizon_minutes
                  AND f.reference_opened_at=x.reference_opened_at
                 WHERE x.asset=? AND x.horizon_minutes=? AND x.learned=1
                   AND f.asset=? AND f.horizon_minutes=?
                   AND f.settled_at IS NOT NULL AND f.outcome_up IN (0,1)
                 ORDER BY f.settled_at DESC, x.issued_at DESC, x.venue DESC,
                          x.contract DESC, x.quote DESC
                 LIMIT ?) recent
               ORDER BY s_at, i_at, v, c, q""",
            (key[0], key[1], key[0], key[1], MAX_CALIBRATION_ROWS),
        ).fetchall()
        if len(graded) < MIN_FIT_ROWS + MIN_EVAL_ROWS:
            return
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
        _LOG.info("RivaL liq_v1 calibration asset=%s horizon=%s status=%s "
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
        calibration["spot_coverage"] = SPOT_COVERAGE
        return calibration
