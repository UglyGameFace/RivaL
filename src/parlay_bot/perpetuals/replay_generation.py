"""Phase-2 generation grid for the replay workbench (amendment
``rival-replay-workbench-phase2-amendment-20261120``, owner-approved
2026-11-20).

Phase 1 (``replay_workbench``) built the grading engine and graded the
backlog. This module is the factory's other half: systematic candidate
generation over the collected estate, the new-estate loaders, the
LLM-mask intake, and the workbench's own results store (the Models
board reads what this module's CLI runs record -- written only by the
workbench, never by the bot).

Frozen rules (the amendment, sections 3-6 -- changing any number here
is a NEW registration, not an edit):

- Generation candidates are computed only from inputs recorded at
  their decision points: the settled forward record's stored 10-value
  book vector. The frozen trial ledger stores probabilities only, so
  no new design is computed on it.
- Mask refits are walk-forward batch logistics: for score day D the
  fit set is the scope rows whose outcomes were observed before D
  begins, truncated to the most recent 5,000 by issuance; Newton fit,
  L2 1.0 on non-intercept weights, warm-started, <= 25 iterations.
- Grading reuses phase 1 verbatim (paired deltas vs the frozen
  issuance baseline, the production book call rule, the four legs;
  conditioning candidates use screen R3's leg structure).
- New-estate candidates and LLM masks are registered and graded
  exactly as far as their recorded inputs allow; below that they are
  NOT MEASURABLE with the cause named -- never padded, never PASS/KILL.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math
from datetime import UTC, datetime, time, timedelta

from parlay_bot.perpetuals import replay_workbench as wb

AMENDMENT = "rival-replay-workbench-phase2-amendment-20261120"

# --- The book vector (polymarket_learning.book_features), grouped ---
BOOK_GROUPS = {
    "levels": (0, 1, 2),   # up ask, down ask, implied ratio
    "shape": (3, 8),       # overround, up-down
    "depth": (4, 5, 9),    # depth slippages, depth difference
    "timing": (6, 7),      # fraction remaining, price-to-beat flag
}
FEATURE_COUNT = 10

# --- Screen G-A fit spec (amendment section 3, frozen) ---
FIT_WINDOW = 5000
FIT_MIN_ROWS = {"A": 1000, "B": 300}
TIER_FLOORS = {"A": {"min_rows": 1000, "min_days": 7, "min_calls": 250,
                     "min_clusters": 100},
               "B": {"min_rows": 500, "min_days": 7, "min_calls": 100,
                     "min_clusters": 40}}
# --- Screen G-D floors (owner-approved 2026-11-24) ---
# G-D grades LLM-proposed masks over the exchange feature vector
# recorded on rival_forward_model_lineage.features_json. The owner
# approved floors mirroring tier B on 2026-11-24. Grading correction
# 2026-11-25: clusters are asset/target clocks, shared across venues
# and horizons, rather than instrument identities (only ~9 per asset).
# No floor, fitted model, or already-recorded verdict is changed.
GD_GRADING_VERSION = "gd-target-clock-v2-20261125"
GD_CLUSTER_UNIT = "asset_target_clock"
GD_FLOORS = {"min_rows": 500, "min_days": 7, "min_calls": 100,
             "min_clusters": 40}
GD_FIT_MIN_ROWS = FIT_MIN_ROWS["B"]
# Grading cutoff: features_json landed on lineage rows in commit
# c2a8d4a9ecd29ecc93d0520791a200fefb789b2f ("Record decision-time
# exchange features on lineage rows", 2026-11-20T22:24:10Z). Rows
# issued before that instant cannot carry a recorded vector (the
# column is nullable and pre-change rows keep NULL), so the G-D
# loader grades only rows issued at or after it AND with a non-NULL
# recorded vector -- never a reconstructed one (design section 4.1).
GD_FEATURES_CUTOFF = "2026-11-20T22:24:10+00:00"
# --- Screen G-C floors (owner-approved 2026-11-25) ---
# The owner approved floors mirroring tier B for G-D on 2026-11-24 and
# extended the identical floors to G-C on 2026-11-25. Registration
# predates the first settled threshold outcome: the outcome record
# was empty when the floors were set (the settler's due-scan defect,
# fixed the same day, had recorded nothing), so no graded sample
# predates the criteria.
GC_GRADING_VERSION = "gc-estate-v1-20261125"
GC_FLOORS = {"min_rows": 500, "min_days": 7, "min_calls": 100,
             "min_clusters": 40}
# The options-implied probability for a contract ending at E is read
# off the listed Deribit expiry nearest E: listed expiries are daily
# 08:00Z against Polymarket's 17:00Z contract ends, so the nearest
# listed expiry sits ~9h from the end. 24h admits that pairing and
# nothing beyond one listed-expiry step past it.
GC_EXPIRY_TOLERANCE = timedelta(hours=24)
# Venue-gap matching: same asset and contract type, strikes within
# 0.25% (Kalshi strikes print at cent precision off the spot, e.g.
# 73799.99, against Polymarket's round thousands), expiries within
# 2 hours (Kalshi hourly closes against Polymarket's daily 17:00Z
# ends), snapshots from the same collection cycle.
GC_STRIKE_TOLERANCE = 0.0025
GC_PAIR_EXPIRY_TOLERANCE = timedelta(hours=2)
# Exchange feature vector layout (features.feature_vector /
# features._window_features, 10 values): 0 last-bar log return x100,
# 1 5-bar log close ratio x100, 2 return volatility x100, 3 bar range
# pct, 4 volume log-ratio, 5 RSI state, 6 MACD state, 7 Bollinger
# z-score, 8 ATR pct, 9 indicator volume regime. The proposal masks
# (NAMED_MASKS / valid_feature_indices) index exactly this vector.
EXCHANGE_FEATURE_COUNT = 10
LOGISTIC_L2 = 1.0
LOGISTIC_MAX_ITER = 25
LOGISTIC_TOL = 1e-7
BURN_IN_DAYS = wb.RECAL_BURN_IN_DAYS


def _mask(*groups: str) -> tuple[int, ...]:
    out: list[int] = []
    for group in groups:
        out.extend(BOOK_GROUPS[group])
    return tuple(sorted(out))


# --- Screen G-A registry (amendment section 3) ---
MASK_CANDIDATES = (
    {"id": "gen-book-all", "factory": "lab", "screen": "G-A", "tier": "A",
     "display": "Book model · all book signals",
     "mask": _mask("levels", "shape", "depth", "timing"), "horizon": None,
     "mechanism": "Control for the class: the live model is an online "
                  "SGD logistic on these same features (fixed lr, "
                  "path-dependent). A clean batch refit tests whether "
                  "the fitting method, not the features, loses to the "
                  "market."},
    {"id": "gen-book-nodepth", "factory": "lab", "screen": "G-A",
     "tier": "A", "display": "Book model · no depth signals",
     "mask": _mask("levels", "shape", "timing"), "horizon": None,
     "mechanism": "Depth-slippage values are arithmetic on the same "
                  "book that sets the baseline; hypothesis: they mirror "
                  "the baseline and add variance, not signal."},
    {"id": "gen-book-notiming", "factory": "lab", "screen": "G-A",
     "tier": "A", "display": "Book model · no clock signals",
     "mask": _mask("levels", "shape", "depth"), "horizon": None,
     "mechanism": "Clock features describe the contract, not its "
                  "direction; the record's own history (the pre-"
                  "2026-11-04 start-price defect class) shows "
                  "clock-coupled inputs are where staleness enters."},
    {"id": "gen-book-levels", "factory": "lab", "screen": "G-A",
     "tier": "A", "display": "Book model · prices only",
     "mask": _mask("levels"), "horizon": None,
     "mechanism": "The minimal-sufficiency claim: the two asks and "
                  "their ratio are the market; everything else in the "
                  "vector is bookkeeping about it."},
    {"id": "gen-book-15m-all", "factory": "lab", "screen": "G-A",
     "tier": "A", "display": "Book model 15m · all signals",
     "mask": _mask("levels", "shape", "depth", "timing"), "horizon": 15,
     "mechanism": "Per-horizon control: pooled fits are 72% 5m rows; "
                  "the 15m scope gets its own refit so its verdict is "
                  "not the 5m verdict restated."},
    {"id": "gen-book-60m-all", "factory": "lab", "screen": "G-A",
     "tier": "B", "display": "Book model 60m · all signals",
     "mask": _mask("levels", "shape", "depth", "timing"), "horizon": 60,
     "mechanism": "The owner's 60m priority: the horizon where the X2 fit "
                  "showed skill, tested with a fresh design on the "
                  "live record (a new design on a different record -- "
                  "X2 itself stays KILL, unregraded)."},
    {"id": "gen-book-60m-nodepth", "factory": "lab", "screen": "G-A",
     "tier": "B", "display": "Book model 60m · no depth signals",
     "mask": _mask("levels", "shape", "timing"), "horizon": 60,
     "mechanism": "The nodepth hypothesis at the priority horizon: "
                  "if depth features are variance anywhere, it shows "
                  "first on the thinnest record."},
)

# --- Screen G-B registry (amendment section 4) ---
CONDITIONING_CANDIDATES = (
    {"id": "cond-late-window", "factory": "screen", "screen": "G-B",
     "display": "Main model's calls · late window only",
     "horizon": None, "min_calls": 250,
     "rule": "fraction of window remaining >= 0.5",
     "mechanism": "Early-window books are the record's known "
                  "staleness class; the model's deviation may carry "
                  "information only once the book is live and the "
                  "window mature."},
    {"id": "cond-tight-book", "factory": "screen", "screen": "G-B",
     "display": "Main model's calls · tight book only",
     "horizon": None, "min_calls": 250,
     "rule": "book overround <= 0.02",
     "mechanism": "A wide two-sided book is an uncertain quote; "
                  "conditioning isolates where the baseline itself is "
                  "reliable, so a deviation there is model information "
                  "rather than quote noise."},
    {"id": "cond-60m-late", "factory": "screen", "screen": "G-B",
     "display": "Main model's calls · 60m late window",
     "horizon": 60, "min_calls": 100,
     "rule": "fraction of window remaining >= 0.5",
     "mechanism": "The late-window hypothesis at the priority "
                  "horizon, where the X2 fit's skill appeared."},
)
CONDITIONING_MIN_DAYS = 7


def _condition(candidate_id: str, features) -> bool:
    if candidate_id in ("cond-late-window", "cond-60m-late"):
        return features[6] >= 0.5
    if candidate_id == "cond-tight-book":
        return features[3] <= 0.02
    raise ValueError(f"unknown conditioning candidate {candidate_id}")


# --- Screen G-C registry (amendment section 5) ---
ESTATE_CANDIDATES = (
    {"id": "opt-implied-btc-weekly", "factory": "lab", "screen": "G-C",
     "display": "Options-implied · BTC weekly",
     "mechanism": "Options-implied probability (Breeden-Litzenberger "
                  "on the Deribit snapshot chain) for Polymarket BTC "
                  "weekly Above contracts, graded against the "
                  "contracts' settlements (the N1 wedge hypothesis, "
                  "workbench form).",
     "floors": dict(GC_FLOORS),
     "cause": "no_settled_threshold_outcomes"},
    {"id": "thr-venue-gap", "factory": "lab", "screen": "G-C",
     "display": "PM vs Kalshi threshold gap",
     "mechanism": "Cross-venue threshold gap at matched strike/tenor "
                  "as a call rule on the cheaper venue (the "
                  "convergence hypothesis at threshold tenors).",
     "floors": dict(GC_FLOORS),
     "cause": "no_settled_threshold_outcomes"},
)

# --- Screen G-D: LLM-mask intake (amendment section 6) ---
# Named hypotheses map to the lab's feature groups over the EXCHANGE
# feature vector (features.feature_vector positions).
NAMED_MASKS = {
    "trend_only": (0, 1), "momentum_only": (0, 1),
    "volatility_only": (2, 3, 8), "volume_only": (4, 9),
}
# Named cause kept ONLY for the case where literally zero
# post-cutoff lineage rows exist for a candidate's asset: there is
# then no recorded exchange-feature sample to grade at all.
LLM_CAUSE = "no_post_cutoff_lineage_rows"


def _mask_slug(indices) -> str:
    key = tuple(indices)
    return {(2, 3, 4, 5, 6, 7, 8, 9): "wide",
            (2, 3, 4, 5, 6, 7, 8): "narrow",
            (2, 3): "volrange",
            (2, 3, 8): "volrange-atr"}.get(
                key, "set" + "-".join(str(i) for i in key))


# ---------------------------------------------------------------------------
# Batch logistic fit (amendment section 3 fit spec). Newton iterations
# on the masked design; intercept unpenalised; L2 on the rest.
# ---------------------------------------------------------------------------

def _solve(matrix, rhs):
    """Gaussian elimination with partial pivoting; None if singular."""
    n = len(rhs)
    aug = [list(matrix[i]) + [rhs[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        for row in range(n):
            if row != col and aug[row][col] != 0.0:
                factor = aug[row][col] / aug[col][col]
                for k in range(col, n + 1):
                    aug[row][k] -= factor * aug[col][k]
    return [aug[i][n] / aug[i][i] for i in range(n)]


def fit_logistic(samples, mask, *, warm=None) -> list[float]:
    """Newton fit of [intercept, *mask weights] on (features, label)."""
    design = [((1.0,) + tuple(float(f[i]) for i in mask), float(y))
              for f, y in samples]
    d = 1 + len(mask)
    weights = (list(warm) if warm is not None and len(warm) == d
               else [0.0] * d)
    if not design:
        return weights
    for _ in range(LOGISTIC_MAX_ITER):
        grad = [0.0] * d
        hess = [[0.0] * d for _ in range(d)]
        for x, y in design:
            z = sum(w * v for w, v in zip(weights, x, strict=True))
            p = wb._sigmoid(z)
            diff = p - y
            wt = p * (1.0 - p)
            for a in range(d):
                grad[a] += diff * x[a]
                for b in range(a, d):
                    hess[a][b] += wt * x[a] * x[b]
        for a in range(d):
            for b in range(a):
                hess[a][b] = hess[b][a]
        for a in range(1, d):
            grad[a] += LOGISTIC_L2 * weights[a]
            hess[a][a] += LOGISTIC_L2
        if max(abs(g) for g in grad) < LOGISTIC_TOL:
            break
        step = _solve(hess, grad)
        if step is None:
            break
        weights = [w - s for w, s in zip(weights, step, strict=True)]
    return weights


def predict_logistic(weights, mask, features) -> float:
    x = (1.0,) + tuple(float(features[i]) for i in mask)
    return wb._sigmoid(sum(w * v for w, v in zip(weights, x, strict=True)))


# ---------------------------------------------------------------------------
# Loader: the settled forward record WITH its book vector retained.
# Eligibility is phase 1's rule verbatim (the production book
# verification); the phase-1 loader's coverage keys are reused so the
# two loaders' counts are directly comparable.
# ---------------------------------------------------------------------------

def load_generation_record(store) -> tuple[list[dict], dict]:
    from parlay_bot.perpetuals.polymarket_learning import MODEL

    sql = (
        "SELECT f.slug, f.probability_up, f.market_baseline, f.fee_rate,"
        " f.issued_at, v.asset, v.horizon, v.features_json,"
        " o.direction, o.observed_at AS outcome_observed_at"
        " FROM rival_polymarket_model_forward f"
        " JOIN rival_polymarket_observations v ON v.slug=f.slug"
        " JOIN rival_polymarket_ai_outcomes o"
        " ON o.slug=f.slug AND o.condition_id=v.condition_id"
        " WHERE f.model_version=? AND f.issued_at<v.expires_at"
        " AND f.issued_at<o.resolved_at AND o.resolved_at>=v.expires_at"
        " AND o.observed_at>=v.expires_at"
        " AND o.direction IN ('UP','DOWN')"
        " AND f.probability_up BETWEEN 0 AND 1"
        " AND f.market_baseline BETWEEN 0 AND 1"
        " ORDER BY f.issued_at, f.slug"
    )
    rows: list[dict] = []
    coverage = {"settled_rows_seen": 0, "unpriceable_book": 0,
                "no_directional_call": 0, "unparseable_row": 0}
    for raw in wb._stream(store, sql, (MODEL,)):
        coverage["settled_rows_seen"] += 1
        issued = wb._parse_time(raw["issued_at"])
        observed = wb._parse_time(raw["outcome_observed_at"])
        try:
            features = [float(v) for v in json.loads(raw["features_json"])]
        except (TypeError, ValueError):
            features = None
        if issued is None or observed is None or features is None \
                or len(features) != FEATURE_COUNT \
                or not all(math.isfinite(v) for v in features):
            coverage["unparseable_row"] += 1
            continue
        p = float(raw["probability_up"])
        up_price, down_price = wb.book_prices(features, raw["market_baseline"])
        if p == 0.5:
            coverage["no_directional_call"] += 1
            continue
        favoured = up_price if p > 0.5 else down_price
        if favoured is None:
            coverage["unpriceable_book"] += 1
            continue
        rows.append({
            "slug": str(raw["slug"]), "asset": str(raw["asset"]),
            "horizon": int(raw["horizon"]), "issued_at": issued,
            "day": issued.date().isoformat(),
            "probability_up": p,
            "market_baseline": float(raw["market_baseline"]),
            "direction": str(raw["direction"]),
            "label": 1 if raw["direction"] == "UP" else 0,
            "outcome_observed_at": observed,
            "fee_rate": (None if raw["fee_rate"] is None
                         else float(raw["fee_rate"])),
            "up_price": up_price, "down_price": down_price,
            "features": tuple(features),
        })
    coverage["eligible_rows"] = len(rows)
    return rows, coverage


# ---------------------------------------------------------------------------
# Screen G-A -- mask logistic refits, walk-forward (amendment 3).
# ---------------------------------------------------------------------------

def screen_generation_masks(rows: list[dict],
                            candidates=MASK_CANDIDATES) -> dict:
    out = {}
    for cand in candidates:
        scope = [r for r in rows
                 if cand["horizon"] is None
                 or r["horizon"] == cand["horizon"]]
        scope.sort(key=lambda r: r["issued_at"])
        days = sorted({r["day"] for r in scope})
        score_days = days[BURN_IN_DAYS:]
        floors = TIER_FLOORS[cand["tier"]]
        fit_min = FIT_MIN_ROWS[cand["tier"]]
        observations: list[dict] = []
        weights = None
        scored_days = 0
        unscored_fit_floor = 0
        fit_sizes: list[int] = []
        for day in score_days:
            day_start = datetime.combine(
                datetime.fromisoformat(day).date(), time.min, tzinfo=UTC)
            fit_rows = [r for r in scope
                        if r["outcome_observed_at"] < day_start]
            fit_rows = fit_rows[-FIT_WINDOW:]
            if len(fit_rows) < fit_min:
                unscored_fit_floor += 1
                continue
            scored_days += 1
            fit_sizes.append(len(fit_rows))
            weights = fit_logistic(
                [(r["features"], r["label"]) for r in fit_rows],
                cand["mask"], warm=weights)
            for row in (r for r in scope if r["day"] == day):
                g = wb._clamp(predict_logistic(
                    weights, cand["mask"], row["features"]))
                y = int(row["label"])
                baseline = float(row["market_baseline"])
                call = wb.call_for(row, g)
                observations.append({
                    "issued_at": row["issued_at"], "day": row["day"],
                    "delta_log_loss": wb._log_loss(g, y)
                    - wb._log_loss(baseline, y),
                    "delta_brier": (g - y) ** 2 - (baseline - y) ** 2,
                    "call_net": call[0] if call else None,
                    "call_cost": call[1] if call else None,
                    "cluster": row["slug"], "asset": row["asset"],
                    "horizon": row["horizon"],
                })
        block = wb._paired_block(observations,
                                 family_size=len(candidates))
        verdict = wb._four_leg_verdict(
            block, min_rows=floors["min_rows"],
            min_days=floors["min_days"], min_calls=floors["min_calls"],
            min_clusters=floors["min_clusters"])
        out[cand["id"]] = {
            **verdict, "score": block,
            "coverage": {
                "scope_rows": len(scope), "scope_days": len(days),
                "scored_days": scored_days,
                "unscored_days_fit_floor": unscored_fit_floor,
                "fit_rows_last_day": fit_sizes[-1] if fit_sizes else 0,
                "fee_basis": wb._fee_basis(scope),
            },
        }
    return {"screen": "G-A", "candidates": out}


# ---------------------------------------------------------------------------
# Screen G-B -- book-state conditioning (amendment 4; R3 leg structure).
# ---------------------------------------------------------------------------

def _calls_block(rows: list[dict]) -> dict:
    calls = []
    for row in rows:
        call = wb.call_for(row, float(row["probability_up"]))
        if call is not None:
            calls.append({"day": row["day"], "call_net": call[0]})
    return {
        "rows": len(rows), "calls": len(calls),
        "call_days": len({c["day"] for c in calls}),
        "mean_net": wb._mean([c["call_net"] for c in calls]),
        "mean_net_ci95": wb._day_cluster_interval(calls, "call_net"),
    }


def screen_conditioning(rows: list[dict],
                        candidates=CONDITIONING_CANDIDATES) -> dict:
    out = {}
    for cand in candidates:
        scope = [r for r in rows
                 if cand["horizon"] is None
                 or r["horizon"] == cand["horizon"]]
        subset = [r for r in scope if _condition(cand["id"], r["features"])]
        pooled = _calls_block(scope)
        block = _calls_block(subset)
        ci = block["mean_net_ci95"] or {}
        floored = (block["calls"] >= cand["min_calls"]
                   and block["call_days"] >= CONDITIONING_MIN_DAYS)
        if not floored:
            status, verdict = "NOT_MEASURABLE", None
        elif (block["mean_net"] is not None and block["mean_net"] > 0
              and ci.get("lower") is not None and ci["lower"] > 0
              and pooled["mean_net"] is not None
              and pooled["mean_net"] <= 0):
            status, verdict = "EVALUATED", "PASS"
        else:
            status, verdict = "EVALUATED", "KILL"
        out[cand["id"]] = {
            "status": status, "verdict": verdict,
            "floors": {
                "calls": {"have": block["calls"], "need": cand["min_calls"]},
                "call_days": {"have": block["call_days"],
                              "need": CONDITIONING_MIN_DAYS},
            },
            "subset": block, "pooled": pooled,
            "coverage": {"scope_rows": len(scope),
                         "subset_rows": len(subset)},
        }
    return {"screen": "G-B", "candidates": out}


# ---------------------------------------------------------------------------
# Screen G-C -- the threshold estate, graded (amendment 5; grader
# registered 2026-11-25 under the owner-approved GC floors). Both
# candidates grade from the recorded estate only: hourly snapshots
# (Deribit surface, Polymarket and Kalshi threshold markets) joined
# to the settled outcomes the threshold settler records in
# rival_threshold_outcomes, through the shared four-leg template.
# Until the first settled outcome exists both candidates stay
# NOT_MEASURABLE with the cause named; below the floors they are
# NOT_MEASURABLE with floor progress, never a stale cause.
# ---------------------------------------------------------------------------

def _coverage(store, table: str, stamp: str) -> dict:
    with store.connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n, COUNT(DISTINCT {stamp}) AS snaps,"
            f" MIN({stamp}) AS first, MAX({stamp}) AS last FROM {table}",
            ()).fetchone()
    item = dict(row) if isinstance(row, dict) else {
        "n": row[0], "snaps": row[1], "first": row[2], "last": row[3]}
    return {"rows": int(item["n"]), "snapshots": int(item["snaps"]),
            "first": item["first"], "last": item["last"]}


def estate_coverage(store) -> dict:
    ticks: dict = {}
    for raw in wb._stream(
            store,
            "SELECT venue, COUNT(*) AS n, MIN(source_at) AS first,"
            " MAX(source_at) AS last"
            " FROM rival_settlement_index_ticks"
            " GROUP BY venue ORDER BY venue", ()):
            item = dict(raw) if isinstance(raw, dict) else {
                "venue": raw[0], "n": raw[1],
                "first": raw[2], "last": raw[3]}
            ticks[str(item["venue"])] = {
                "rows": int(item["n"]), "first": item["first"],
                "last": item["last"]}
    from parlay_bot.perpetuals.threshold_outcomes import settled_count

    return {
        "options_surface": _coverage(
            store, "rival_options_surface_snapshots", "snapshot_at"),
        "threshold_polymarket": _coverage(
            store, "rival_threshold_market_snapshots", "snapshot_at"),
        "threshold_kalshi": _coverage(
            store, "rival_kalshi_threshold_snapshots", "snapshot_at"),
        "settlement_index_ticks": ticks,
        "settled_threshold_outcomes": settled_count(store),
    }


def _implied_above_probability(chain, strike: float) -> float | None:
    """Risk-neutral P(S_T > strike) from one expiry's call chain.

    chain: (strike, USD call price) pairs. Breeden-Litzenberger: the
    digital is minus the strike-derivative of the call price,
    estimated as the slope of the listed segment bracketing the
    strike (the nearest segment when the strike sits outside the
    listed range). None when the chain cannot speak: fewer than two
    usable strikes, or one strike carrying conflicting prices.
    """
    points: dict[float, float] = {}
    for level, price in chain:
        if level is None or price is None:
            continue
        level, price = float(level), float(price)
        if not (math.isfinite(level) and math.isfinite(price)) \
                or price < 0:
            continue
        if level in points and abs(points[level] - price) > 1e-9:
            return None
        points[level] = price
    strikes = sorted(points)
    if len(strikes) < 2:
        return None
    idx = bisect.bisect_right(strikes, strike)
    if idx == 0:
        lo, hi = strikes[0], strikes[1]
    elif idx >= len(strikes):
        lo, hi = strikes[-2], strikes[-1]
    else:
        lo, hi = strikes[idx - 1], strikes[idx]
    return wb._clamp((points[lo] - points[hi]) / (hi - lo))


def _gc_observation(*, issued_at, g, baseline, label, call, cluster,
                    asset, horizon, contract_type) -> dict:
    """One paired observation in the shared four-leg shape."""
    return {
        "issued_at": issued_at, "day": issued_at.date().isoformat(),
        "delta_log_loss": wb._log_loss(g, label)
        - wb._log_loss(baseline, label),
        "delta_brier": (g - label) ** 2 - (baseline - label) ** 2,
        "call_net": call[0] if call else None,
        "call_cost": call[1] if call else None,
        "cluster": cluster, "asset": asset, "horizon": horizon,
        "contract_type": contract_type,
    }


def _gc_pm_call(*, g, baseline, best_bid, best_ask, fee_rate, label):
    """Call on a Polymarket threshold contract at recorded prices.

    Buys the side the candidate probability favours over the market's
    own price: YES at the recorded best ask when g > baseline, NO at
    1 - best bid when g < baseline. The fee comes from the snapshot's
    recorded fee_rate (the estate's rule: grading reads the fee from
    the recorded snapshot); an unrecorded fee_rate prices no call
    rather than guessing a schedule.
    """
    if g == baseline or fee_rate is None:
        return None
    from parlay_bot.perpetuals.polymarket_learning import polymarket_fee

    if g > baseline:
        price, won = best_ask, label == 1
    else:
        price = None if best_bid is None else 1.0 - float(best_bid)
        won = label == 0
    if price is None or not 0.0 < float(price) < 1.0:
        return None
    fee = polymarket_fee(float(price), float(fee_rate))
    return (1.0 if won else 0.0) - float(price) - fee, float(price) + fee


def _gc_kalshi_call(*, g, baseline, yes_ask, no_ask, label):
    """Call on a Kalshi threshold contract at recorded asks, fee by
    the venue's published taker formula (kalshi_fee)."""
    if g == baseline:
        return None
    from parlay_bot.perpetuals.kalshi_updown import kalshi_fee

    if g > baseline:
        price, won = yes_ask, label == 1
    else:
        price, won = no_ask, label == 0
    if price is None or not 0.0 < float(price) < 1.0:
        return None
    fee = kalshi_fee(float(price))
    return (1.0 if won else 0.0) - float(price) - fee, float(price) + fee


def _settled_outcomes(store, venue: str) -> dict[str, int]:
    """market_key -> resolved_yes for one venue's settled outcomes."""
    out: dict[str, int] = {}
    for raw in wb._stream(
            store,
            "SELECT market_key, resolved_yes"
            " FROM rival_threshold_outcomes"
            " WHERE venue=? AND resolved_yes IS NOT NULL", (venue,)):
        row = dict(raw)
        out[str(row["market_key"])] = int(row["resolved_yes"])
    return out


def load_gc_opt_implied(store) -> tuple[list[dict], dict]:
    """Observations for opt-implied-btc-weekly.

    One observation per (hourly snapshot, settled BTC weekly Above
    contract): the options-implied probability from the Deribit call
    chain of the same collection cycle (mark prices are coin-denominated,
    so the USD chain is mark x underlying), the market's recorded yes
    price as the paired baseline, the contract's own settlement as
    the label. Cluster = the contract.
    """
    coverage = {"settled_contracts": 0, "snapshot_rows_seen": 0,
                "surface_snapshot_missing": 0, "expiry_unmatched": 0,
                "chain_unusable": 0, "baseline_unusable": 0}
    outcomes: dict[str, int] = {}
    for raw in wb._stream(
            store,
            "SELECT market_key, resolved_yes"
            " FROM rival_threshold_outcomes"
            " WHERE venue=? AND asset=? AND contract_type=?"
            " AND resolved_yes IS NOT NULL",
            ("polymarket", "BTC", "above")):
        row = dict(raw)
        outcomes[str(row["market_key"])] = int(row["resolved_yes"])
    coverage["settled_contracts"] = len(outcomes)
    if not outcomes:
        return [], coverage
    # (snapshot, expiry) -> [(strike, USD call price)] for BTC calls.
    chains: dict[datetime, dict[datetime, list]] = {}
    for raw in wb._stream(
            store,
            "SELECT snapshot_at, expiry_at, strike, mark_price,"
            " underlying_price FROM rival_options_surface_snapshots"
            " WHERE currency=? AND option_type=?"
            " ORDER BY snapshot_at, expiry_at, strike",
            ("BTC", "call")):
        row = dict(raw)
        snap = wb._parse_time(row["snapshot_at"])
        expiry = wb._parse_time(row["expiry_at"])
        if snap is None or expiry is None \
                or row["mark_price"] is None \
                or row["underlying_price"] is None:
            continue
        usd = float(row["mark_price"]) * float(row["underlying_price"])
        chains.setdefault(snap, {}).setdefault(expiry, []).append(
            (float(row["strike"]), usd))
    observations: list[dict] = []
    for raw in wb._stream(
            store,
            "SELECT condition_id, snapshot_at, strike_low, end_date,"
            " yes_price, best_bid, best_ask, fee_rate"
            " FROM rival_threshold_market_snapshots"
            " WHERE asset=? AND contract_type=? AND series_id=?"
            " ORDER BY snapshot_at", ("BTC", "above", 45)):
        row = dict(raw)
        label = outcomes.get(str(row["condition_id"]))
        if label is None:
            continue
        coverage["snapshot_rows_seen"] += 1
        issued = wb._parse_time(row["snapshot_at"])
        end = wb._parse_time(row["end_date"])
        baseline = row["yes_price"]
        if issued is None or end is None or baseline is None \
                or not 0.0 < float(baseline) < 1.0 \
                or row["strike_low"] is None:
            coverage["baseline_unusable"] += 1
            continue
        snapshot_chains = chains.get(issued)
        if not snapshot_chains:
            coverage["surface_snapshot_missing"] += 1
            continue
        expiry = min(snapshot_chains,
                     key=lambda e: abs((e - end).total_seconds()))
        if abs(expiry - end) > GC_EXPIRY_TOLERANCE:
            coverage["expiry_unmatched"] += 1
            continue
        g = _implied_above_probability(
            snapshot_chains[expiry], float(row["strike_low"]))
        if g is None:
            coverage["chain_unusable"] += 1
            continue
        baseline = float(baseline)
        call = _gc_pm_call(
            g=g, baseline=baseline, best_bid=row["best_bid"],
            best_ask=row["best_ask"], fee_rate=row["fee_rate"],
            label=label)
        observations.append(_gc_observation(
            issued_at=issued, g=g, baseline=baseline, label=label,
            call=call, cluster=str(row["condition_id"]), asset="BTC",
            horizon="weekly", contract_type="above"))
    return observations, coverage


def _strike_match(pm_low, pm_high, k_low, k_high) -> float | None:
    """Strike distance of a PM/Kalshi pair, or None when not matched.

    Both bounds must be present on both sides or absent on both;
    distance is relative to the Polymarket strike.
    """
    if pm_low is None or k_low is None:
        return None
    if (pm_high is None) != (k_high is None):
        return None
    distance = abs(float(pm_low) - float(k_low)) / float(pm_low)
    if pm_high is not None:
        if float(pm_high) == 0:
            return None
        distance = max(
            distance,
            abs(float(pm_high) - float(k_high)) / float(pm_high))
    return distance if distance <= GC_STRIKE_TOLERANCE else None


def load_gc_venue_gap(store) -> tuple[list[dict], dict]:
    """Observations for thr-venue-gap.

    A matched pair is a Polymarket and a Kalshi threshold contract
    with the same asset and contract type, strikes within
    GC_STRIKE_TOLERANCE, expiries within GC_PAIR_EXPIRY_TOLERANCE,
    snapshotted in the same collection cycle. Each pair emits one
    observation per venue: that venue's price graded against the
    other venue's price as the candidate probability, settled by
    that venue's own outcome record (settlements are never crossed).
    Cluster = the matched pair.
    """
    coverage = {"pm_snapshot_rows_seen": 0, "pairs_matched": 0,
                "ambiguous_pairs": 0, "kalshi_outcome_missing": 0,
                "kalshi_price_missing": 0, "baseline_unusable": 0}
    pm_outcomes = _settled_outcomes(store, "polymarket")
    k_outcomes = _settled_outcomes(store, "kalshi")
    if not pm_outcomes:
        return [], coverage
    observations: list[dict] = []

    def kalshi_bucket(snapshot_at: str) -> list[dict]:
        with store.connect() as conn:
            rows = conn.execute(
                "SELECT market_ticker, asset, contract_type, strike_low,"
                " strike_high, yes_bid, yes_ask, no_ask, last_price,"
                " close_time FROM rival_kalshi_threshold_snapshots"
                " WHERE snapshot_at=?", (snapshot_at,)).fetchall()
        return [dict(r) for r in rows]

    current_stamp: str | None = None
    bucket: list[dict] = []
    for raw in wb._stream(
            store,
            "SELECT condition_id, snapshot_at, asset, contract_type,"
            " horizon, strike_low, strike_high, end_date, yes_price,"
            " best_bid, best_ask, fee_rate"
            " FROM rival_threshold_market_snapshots"
            " ORDER BY snapshot_at", ()):
        row = dict(raw)
        label = pm_outcomes.get(str(row["condition_id"]))
        if label is None:
            continue
        coverage["pm_snapshot_rows_seen"] += 1
        if row["snapshot_at"] != current_stamp:
            current_stamp = row["snapshot_at"]
            bucket = kalshi_bucket(current_stamp)
        issued = wb._parse_time(row["snapshot_at"])
        end = wb._parse_time(row["end_date"])
        baseline = row["yes_price"]
        if issued is None or end is None or baseline is None \
                or not 0.0 < float(baseline) < 1.0:
            coverage["baseline_unusable"] += 1
            continue
        matches = []
        for k in bucket:
            if k["asset"] != row["asset"] \
                    or k["contract_type"] != row["contract_type"]:
                continue
            k_end = wb._parse_time(k["close_time"])
            if k_end is None \
                    or abs(k_end - end) > GC_PAIR_EXPIRY_TOLERANCE:
                continue
            distance = _strike_match(
                row["strike_low"], row["strike_high"],
                k["strike_low"], k["strike_high"])
            if distance is not None:
                matches.append((distance, k))
        if not matches:
            continue
        matches.sort(key=lambda m: m[0])
        if len(matches) > 1 and matches[1][0] == matches[0][0]:
            coverage["ambiguous_pairs"] += 1
            continue
        kalshi = matches[0][1]
        coverage["pairs_matched"] += 1
        if kalshi["yes_bid"] is not None and kalshi["yes_ask"] is not None:
            k_mid = (float(kalshi["yes_bid"]) + float(kalshi["yes_ask"])) / 2
        elif kalshi["last_price"] is not None:
            k_mid = float(kalshi["last_price"])
        else:
            coverage["kalshi_price_missing"] += 1
            continue
        if not 0.0 < k_mid < 1.0:
            coverage["kalshi_price_missing"] += 1
            continue
        baseline = float(baseline)
        cluster = f"{row['condition_id']}|{kalshi['market_ticker']}"
        pm_call = _gc_pm_call(
            g=k_mid, baseline=baseline, best_bid=row["best_bid"],
            best_ask=row["best_ask"], fee_rate=row["fee_rate"],
            label=label)
        observations.append(_gc_observation(
            issued_at=issued, g=k_mid, baseline=baseline, label=label,
            call=pm_call, cluster=cluster, asset=str(row["asset"]),
            horizon=str(row["horizon"]),
            contract_type=str(row["contract_type"])))
        k_label = k_outcomes.get(str(kalshi["market_ticker"]))
        if k_label is None:
            coverage["kalshi_outcome_missing"] += 1
            continue
        k_call = _gc_kalshi_call(
            g=baseline, baseline=k_mid, yes_ask=kalshi["yes_ask"],
            no_ask=kalshi["no_ask"], label=k_label)
        observations.append(_gc_observation(
            issued_at=issued, g=baseline, baseline=k_mid, label=k_label,
            call=k_call, cluster=cluster, asset=str(row["asset"]),
            horizon=str(row["horizon"]),
            contract_type=str(row["contract_type"])))
    return observations, coverage


_GC_LOADERS = {
    "opt-implied-btc-weekly": load_gc_opt_implied,
    "thr-venue-gap": load_gc_venue_gap,
}


def screen_estate(store) -> dict:
    coverage = estate_coverage(store)
    settled = coverage["settled_threshold_outcomes"]
    candidates = {}
    for cand in ESTATE_CANDIDATES:
        observations, grade_coverage = _GC_LOADERS[cand["id"]](store)
        block = wb._paired_block(
            observations, family_size=len(ESTATE_CANDIDATES))
        verdict = wb._four_leg_verdict(
            block, min_rows=GC_FLOORS["min_rows"],
            min_days=GC_FLOORS["min_days"],
            min_calls=GC_FLOORS["min_calls"],
            min_clusters=GC_FLOORS["min_clusters"])
        floors = {
            "rows": {"have": block["rows"], "need": GC_FLOORS["min_rows"]},
            "days": {"have": block["days"], "need": GC_FLOORS["min_days"]},
            "calls": {"have": block["calls"]["n"],
                      "need": GC_FLOORS["min_calls"]},
            "clusters": {"have": block.get("clusters"),
                         "need": GC_FLOORS["min_clusters"]},
        }
        entry = {**verdict, "floors": floors, "score": block,
                 "grading_version": GC_GRADING_VERSION,
                 "coverage": grade_coverage}
        if settled == 0:
            entry.update({
                "status": "NOT_MEASURABLE", "verdict": None,
                "cause": cand["cause"],
                "reason": "no settled threshold outcome exists yet: "
                          "the outcome settler's due-scan defect (fixed "
                          "2026-11-25) left the record empty, and the "
                          "grader -- registered the same day, before "
                          "any outcome existed -- grades as soon as "
                          "settled contracts land",
            })
        candidates[cand["id"]] = entry
    return {"screen": "G-C", "grading_version": GC_GRADING_VERSION,
            "coverage": coverage, "candidates": candidates,
            "settlement_index_note": "loader coverage reported above; no "
            "candidate is registered on the settlement-index ticks -- "
            "they are the registered fair-value test's own record "
            "(design section 4.5)"}


# ---------------------------------------------------------------------------
# Screen G-D -- LLM-mask intake (amendment 6). Bounded at the stored
# proposal set; each proposal is re-verified from its stored fields
# (the recent_hypotheses integrity digest + the validate_proposal
# field rules) and deduplicated oldest-first by (asset, hypothesis,
# mask). The LLM proposes; this intake is the reviewer registration.
# ---------------------------------------------------------------------------

def _proposal_mask(row: dict) -> tuple[int, ...] | None:
    kind = row.get("hypothesis")
    if kind == "feature_subset_v2":
        try:
            indices = json.loads(row.get("feature_indices_json") or "null")
        except ValueError:
            return None
        from parlay_bot.perpetuals.research_hypotheses import (
            valid_feature_indices,
        )
        return tuple(indices) if valid_feature_indices(indices) else None
    return NAMED_MASKS.get(str(kind))


def load_llm_proposals(store) -> tuple[list[dict], dict]:
    """(verified proposals oldest-first, rejection counts by reason)."""
    from parlay_bot.perpetuals.research_hypotheses import (
        HYPOTHESES,
    )
    from parlay_bot.perpetuals.research_hypotheses import (
        MODEL as LLM_MODEL,
    )

    rejected = {"integrity": 0, "field_rules": 0, "unparseable": 0}
    proposals: list[dict] = []
    sql = (
        "SELECT asset, slot_utc, model, hypothesis, feature_indices_json,"
        " rationale, falsification, evidence_sha256, record_sha256,"
        " source_venues_json, attempted_at"
        " FROM rival_llm_feature_hypotheses WHERE status='proposed'"
        " ORDER BY attempted_at ASC"
    )
    for raw in wb._stream(store, sql, ()):
        row = dict(raw)
        try:
            venues = json.loads(row.get("source_venues_json") or "null")
            record = {
                "asset": row["asset"], "slot_utc": row["slot_utc"],
                "model": LLM_MODEL,
                "evidence_sha256": row["evidence_sha256"],
                "hypothesis": row["hypothesis"], "venues": venues,
            }
            indices = None
            if row["hypothesis"] == "feature_subset_v2":
                indices = json.loads(row.get("feature_indices_json")
                                     or "null")
                record["feature_indices"] = indices
            digest = hashlib.sha256(json.dumps(
                record, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        except (TypeError, ValueError, KeyError):
            rejected["unparseable"] += 1
            continue
        if digest != row.get("record_sha256") \
                or row.get("model") != LLM_MODEL:
            rejected["integrity"] += 1
            continue
        rationale = row.get("rationale")
        falsification = row.get("falsification")
        if row["hypothesis"] not in HYPOTHESES \
                or not (isinstance(rationale, str)
                        and 5 <= len(rationale.strip()) <= 120) \
                or not (isinstance(falsification, str)
                        and 5 <= len(falsification.strip()) <= 120) \
                or _proposal_mask(row) is None:
            rejected["field_rules"] += 1
            continue
        proposals.append({
            "asset": str(row["asset"]), "hypothesis": str(row["hypothesis"]),
            "mask": _proposal_mask(row),
            "rationale": rationale.strip(),
            "falsification": falsification.strip(),
            "attempted_at": str(row["attempted_at"]),
            "record_sha256": digest,
        })
    return proposals, rejected


def _stream_gd(store, sql, params):
    """Stream the G-D loader query with parallel workers disabled.

    The managed Postgres host's /dev/shm cannot grant the 16 MB POSIX
    dynamic-shared-memory resize this join's parallel hash plan needs
    (DiskFull, SQLSTATE 53100, reproduced live 2026-11-27; the serial
    plan completes with identical rows). SET LOCAL scopes the GUC to
    this checkout's transaction only: it evaporates at the commit on
    connection exit, before the pooled connection is handed back, so
    no other caller's plan changes. SQLite has no such GUC and no
    parallel planner, so the SET is Postgres-only.
    """
    with store.connect() as conn:
        if getattr(conn, "backend", None) == "postgres":
            conn.execute("SET LOCAL max_parallel_workers_per_gather = 0")
        cursor = conn.execute(sql, params)
        while True:
            chunk = cursor.fetchmany(wb.FETCH_CHUNK)
            if not chunk:
                return
            yield from chunk


def load_gd_record(store) -> tuple[list[dict], dict]:
    """Settled perp rows joined to their decision-time lineage vector.

    Join key: the lineage table's full primary key against
    perp_forward_audit -- (venue, contract, quote, horizon_minutes,
    model_version, reference_opened_at) -- plus issued_at equality
    (the exact join the live lineage readers use). Only settled audit
    rows (outcome_up 0/1) issued at or after GD_FEATURES_CUTOFF with a
    recorded (non-NULL) exchange feature vector are eligible; the
    audit row's baseline_up is the paired baseline probability and
    settled_at is the outcome-observed stamp for the walk-forward
    embargo. An asset/target clock is one measurement cluster across
    venues and horizons, matching XMarket's target-clock convention.
    These counts alone do not prove independence of overlapping returns;
    score uncertainty continues to use whole UTC-day blocks.
    """
    from parlay_bot.perpetuals.forward_audit import MODEL as PERP_MODEL

    sql = (
        "SELECT l.venue, l.contract, l.quote, l.horizon_minutes,"
        " l.reference_opened_at, l.issued_at, l.features_json,"
        " f.asset, f.target_opened_at, f.probability_up, f.baseline_up,"
        " f.outcome_up, f.settled_at"
        " FROM rival_forward_model_lineage l"
        " JOIN perp_forward_audit f"
        " ON f.venue=l.venue AND f.contract=l.contract"
        " AND f.quote=l.quote AND f.horizon_minutes=l.horizon_minutes"
        " AND f.model_version=l.model_version"
        " AND f.reference_opened_at=l.reference_opened_at"
        " AND f.issued_at=l.issued_at"
        " WHERE l.model_version=? AND l.features_json IS NOT NULL"
        " AND l.issued_at>=? AND f.settled_at IS NOT NULL"
        " AND f.outcome_up IN (0,1) AND f.settled_at>f.issued_at"
        " AND f.baseline_up>0 AND f.baseline_up<1"
        " ORDER BY l.issued_at, l.venue, l.contract, l.quote,"
        " l.horizon_minutes"
    )
    rows: list[dict] = []
    coverage = {"settled_rows_seen": 0, "unparseable_row": 0}
    for raw in _stream_gd(store, sql, (PERP_MODEL, GD_FEATURES_CUTOFF)):
        coverage["settled_rows_seen"] += 1
        issued = wb._parse_time(raw["issued_at"])
        settled = wb._parse_time(raw["settled_at"])
        target = wb._parse_time(raw["target_opened_at"])
        try:
            features = [float(v) for v in json.loads(raw["features_json"])]
        except (TypeError, ValueError):
            features = None
        if issued is None or settled is None or target is None \
                or target + timedelta(minutes=5) <= issued \
                or features is None \
                or len(features) != EXCHANGE_FEATURE_COUNT \
                or not all(math.isfinite(v) for v in features):
            coverage["unparseable_row"] += 1
            continue
        rows.append({
            "venue": str(raw["venue"]), "contract": str(raw["contract"]),
            "quote": str(raw["quote"]),
            "cluster": f"{raw['asset']}:{target.isoformat()}",
            "asset": str(raw["asset"]),
            "horizon": int(raw["horizon_minutes"]),
            "issued_at": issued, "day": issued.date().isoformat(),
            "probability_up": float(raw["probability_up"]),
            "market_baseline": float(raw["baseline_up"]),
            "label": int(raw["outcome_up"]),
            "direction": "UP" if int(raw["outcome_up"]) == 1 else "DOWN",
            "outcome_observed_at": settled,
            "features": tuple(features),
        })
    coverage["eligible_rows"] = len(rows)
    return rows, coverage


def _gd_call(row: dict, g: float) -> tuple[float, float] | None:
    """Implied-price call for a perp candidate probability.

    Perp rows carry no book and no recorded fee, so -- exactly as
    screen X1-X5 prices shadow-ledger calls -- the call is priced at
    the baseline-implied price (baseline_up for an UP call,
    1-baseline_up for DOWN), labelled IMPLIED-PRICE; nets are upper
    bounds. No call when g is exactly 0.5 or the implied price is not
    in (0, 1).
    """
    if g == 0.5:
        return None
    up = g > 0.5
    price = float(row["market_baseline"]) if up \
        else 1.0 - float(row["market_baseline"])
    if not 0.0 < price < 1.0:
        return None
    won = (int(row["label"]) == 1) == up
    return (1.0 if won else 0.0) - price, price


def screen_llm_masks(rows: list[dict],
                     registrations: list[dict]) -> dict:
    """Screen G-D: walk-forward mask refits on the lineage record.

    Same fit spec and leg machinery as G-A (batch logistic, L2 1.0,
    <=25 Newton iterations, 5,000-row fit window, full-day embargo,
    wb._paired_block + wb._four_leg_verdict with the corrected legs),
    graded per registered candidate over its own asset's rows, with
    GD_FLOORS (cluster = asset/target clock). A candidate whose asset has zero
    post-cutoff rows keeps the named cause; any other below-floor
    candidate is NOT_MEASURABLE with floor progress, never a stale
    cause.
    """
    out = {}
    for cand in registrations:
        mask = tuple(int(i) for i in cand["mask"])
        scope = [r for r in rows if r["asset"] == cand["asset"]]
        scope.sort(key=lambda r: r["issued_at"])
        days = sorted({r["day"] for r in scope})
        score_days = days[BURN_IN_DAYS:]
        observations: list[dict] = []
        weights = None
        scored_days = 0
        unscored_fit_floor = 0
        fit_sizes: list[int] = []
        for day in score_days:
            day_start = datetime.combine(
                datetime.fromisoformat(day).date(), time.min, tzinfo=UTC)
            fit_rows = [r for r in scope
                        if r["outcome_observed_at"] < day_start]
            fit_rows = fit_rows[-FIT_WINDOW:]
            if len(fit_rows) < GD_FIT_MIN_ROWS:
                unscored_fit_floor += 1
                continue
            scored_days += 1
            fit_sizes.append(len(fit_rows))
            weights = fit_logistic(
                [(r["features"], r["label"]) for r in fit_rows],
                mask, warm=weights)
            for row in (r for r in scope if r["day"] == day):
                g = wb._clamp(predict_logistic(
                    weights, mask, row["features"]))
                y = int(row["label"])
                baseline = float(row["market_baseline"])
                call = _gd_call(row, g)
                observations.append({
                    "issued_at": row["issued_at"], "day": row["day"],
                    "delta_log_loss": wb._log_loss(g, y)
                    - wb._log_loss(baseline, y),
                    "delta_brier": (g - y) ** 2 - (baseline - y) ** 2,
                    "call_net": call[0] if call else None,
                    "call_cost": call[1] if call else None,
                    "cluster": row["cluster"], "asset": row["asset"],
                    "horizon": row["horizon"],
                })
        block = wb._paired_block(observations,
                                 family_size=max(1, len(registrations)))
        verdict = wb._four_leg_verdict(
            block, min_rows=GD_FLOORS["min_rows"],
            min_days=GD_FLOORS["min_days"],
            min_calls=GD_FLOORS["min_calls"],
            min_clusters=GD_FLOORS["min_clusters"])
        floors = {
            "rows": {"have": block["rows"], "need": GD_FLOORS["min_rows"]},
            "days": {"have": block["days"], "need": GD_FLOORS["min_days"]},
            "calls": {"have": block["calls"]["n"],
                      "need": GD_FLOORS["min_calls"]},
            "clusters": {"have": block.get("clusters"),
                         "need": GD_FLOORS["min_clusters"]},
        }
        entry = {
            **verdict, "floors": floors, "score": block,
            "grading_version": GD_GRADING_VERSION,
            "coverage": {
                "scope_rows": len(scope), "scope_days": len(days),
                "scored_days": scored_days,
                "unscored_days_fit_floor": unscored_fit_floor,
                "fit_rows_last_day": fit_sizes[-1] if fit_sizes else 0,
                "pricing": "IMPLIED-PRICE (baseline-implied; no perp"
                           " fee recorded, call nets are upper bounds)",
                "features_cutoff": GD_FEATURES_CUTOFF,
                "cluster_unit": GD_CLUSTER_UNIT,
                "overlap_independence_proven": False,
            },
        }
        if not scope:
            entry.update({
                "status": "NOT_MEASURABLE", "verdict": None,
                "cause": LLM_CAUSE,
                "reason": "no settled lineage rows with a recorded "
                          "exchange feature vector exist for this "
                          "asset at or after the features column "
                          "landed (2026-11-20); earlier rows keep no "
                          "recorded vector and design section 4.1 "
                          "forbids reconstructing one",
            })
        out[cand["id"]] = entry
    return {"screen": "G-D", "grading_version": GD_GRADING_VERSION,
            "candidates": out}


def llm_registrations(proposals: list[dict]) -> list[dict]:
    """Deduplicated registry entries for the verified proposals.

    Registrations carry the owner-approved GD floors only; whether a
    candidate is measurable is the G-D grader's decision
    (screen_llm_masks over load_gd_record), never hard-coded here.
    """
    seen: set = set()
    out: list[dict] = []
    for proposal in proposals:
        kind = ("trend" if proposal["hypothesis"] == "trend_only"
                else _mask_slug(proposal["mask"]))
        key = (proposal["asset"], proposal["hypothesis"], proposal["mask"])
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "id": f"llm-mask-{proposal['asset'].lower()}-{kind}",
            "factory": "lab", "screen": "G-D",
            "display": f"AI-suggested mask · {proposal['asset']}",
            "asset": proposal["asset"], "mask": list(proposal["mask"]),
            "mechanism": "LLM-proposed (LFM3-1.7B, frozen proposer); "
                         "proposer's stated rationale: "
                         + proposal["rationale"],
            "falsification": proposal["falsification"],
            "proposed_at": proposal["attempted_at"],
            "record_sha256": proposal["record_sha256"],
            "floors": dict(GD_FLOORS),
        })
    return out


# ---------------------------------------------------------------------------
# The workbench results store. Written ONLY by the workbench CLI
# (record_results); the Models board reads it (read_results_for_board).
# ---------------------------------------------------------------------------

RESULTS_TABLE = "rival_replay_workbench_results"
RESULTS_DDL = f"""
CREATE TABLE IF NOT EXISTS {RESULTS_TABLE} (
    candidate_id TEXT NOT NULL, screen TEXT NOT NULL,
    factory TEXT NOT NULL, display TEXT NOT NULL,
    status TEXT NOT NULL, verdict TEXT,
    graded_rows INTEGER NOT NULL DEFAULT 0,
    calls INTEGER NOT NULL DEFAULT 0,
    days INTEGER NOT NULL DEFAULT 0,
    evaluated_at TEXT NOT NULL, detail_json TEXT NOT NULL DEFAULT '{{}}',
    PRIMARY KEY (candidate_id, evaluated_at)
);
"""

PHASE1_DISPLAY = {
    "recal-raw": "Live model · unmodified (control)",
    "recal-platt": "Recalibration · Platt",
    "recal-isotonic": "Recalibration · isotonic",
    "recal-shrink50": "Recalibration · shrink 50%",
    "recal-shrink25": "Recalibration · shrink 25%",
    "liq-cascade-v1": "Liquidation cascade screen",
    "cb-premium-v1": "Coinbase premium screen",
    "xmarket-btc-15m-6cf388": "XMarket fit · BTC 15m",
    "xmarket-btc-60m-425d49": "XMarket fit · BTC 60m",
    "xmarket-sol-15m-43e5cc": "XMarket fit · SOL 15m",
    "xmarket-xrp-15m-182bbf": "XMarket fit · XRP 15m",
    "xmarket-xrp-60m-b799ee": "XMarket fit · XRP 60m",
}


def ensure_results_table(store) -> None:
    with store.connect() as conn:
        conn.execute(RESULTS_DDL, ())


def record_results(store, entries: list[dict]) -> int:
    ensure_results_table(store)
    with store.connect() as conn:
        for entry in entries:
            conn.execute(
                f"DELETE FROM {RESULTS_TABLE}"
                " WHERE candidate_id=? AND evaluated_at=?",
                (entry["candidate_id"], entry["evaluated_at"]))
            conn.execute(
                f"INSERT INTO {RESULTS_TABLE} (candidate_id, screen,"
                " factory, display, status, verdict, graded_rows, calls,"
                " days, evaluated_at, detail_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (entry["candidate_id"], entry["screen"], entry["factory"],
                 entry["display"], entry["status"], entry["verdict"],
                 int(entry.get("graded_rows") or 0),
                 int(entry.get("calls") or 0),
                 int(entry.get("days") or 0), entry["evaluated_at"],
                 json.dumps(entry.get("detail") or {}, sort_keys=True)))
    return len(entries)


def _results_table_exists(store) -> bool:
    """SELECT-only existence check: the board renders through
    read-only connections that reject any other statement class (the
    performance probe enforces SELECT/WITH only), so the PRAGMA /
    information_schema column probe other readers use is not safe
    here."""
    if store.database.backend == "sqlite":
        sql = ("SELECT name FROM sqlite_master"
               " WHERE type='table' AND name=?")
    else:
        sql = ("SELECT table_name AS name FROM information_schema.tables"
               " WHERE table_schema = current_schema() AND table_name=?")
    with store.connect() as conn:
        return conn.execute(sql, (RESULTS_TABLE,)).fetchone() is not None


# Why a candidate cannot be measured, in the plain words the boards
# show. The owner (2026-11-24): a bare "not measurable yet" count hides
# exactly the thing he needs to see drain -- every blocked candidate
# carries its specific blocker and its progress toward its floors.
CAUSE_TEXT = {
    "exchange_features_not_recorded": (
        "correction: its exchange features ARE recorded on the live "
        "lineage -- the real blocker was that no grader read them. "
        "The G-D grader now reads the recorded vectors; it was built "
        "2026-11-25, and older snapshots carry the old registration"),
    "no_post_cutoff_lineage_rows": (
        "no settled lineage rows with a recorded exchange feature "
        "vector exist for its asset at or after the features column "
        "landed on 2026-11-20"),
    "no_settled_threshold_outcomes": (
        "the G-C grader is registered (floors set 2026-11-25, before "
        "any settled outcome existed); outcomes began recording on "
        "2026-11-25 once the settler's due-scan defect was fixed"),
}
DATA_BLOCKED_TEXT = {
    "liq-cascade-v1": (
        "no free archive of timestamped liquidation events exists "
        "for the record window (verified 2026-11-20); the remaining "
        "sources are paid or API-keyed"),
}
# Canonical SHORT blocker phrases for the board views. A board line
# must carry a complete phrase or nothing: the full explanation above
# appears only in the Full evidence view, and no board render may cut
# a long blocker mid-sentence to make it fit. A candidate whose
# recorded blocker has no registered short form falls back to
# GENERIC_BLOCKER_SHORT -- never to a slice of the long text.
CAUSE_SHORT_TEXT = {
    "exchange_features_not_recorded": (
        "grader built 2026-11-25; grades at the next workbench run"),
    "no_post_cutoff_lineage_rows": (
        "waiting for settled rows with recorded features"),
    "no_settled_threshold_outcomes": (
        "grader registered; waiting for settled outcomes"),
}
DATA_BLOCKED_SHORT_TEXT = {
    "liq-cascade-v1": "needs a paid data source; owner's decision",
}
FLOOR_BLOCKER_SHORT = "accruing toward its floors"
GENERIC_BLOCKER_SHORT = "blocked — full reason in Full evidence"
_REASON_SHORT_LIMIT = 72


def _burn_in_short(detail: dict) -> str | None:
    """Short blocker for a walk-forward candidate still in burn-in.

    The G-D grader scores nothing until BURN_IN_DAYS scope days have
    elapsed, so a candidate can sit on tens of thousands of recorded
    rows with every scored floor at zero -- "rows 0/500" reads as
    "no data", which is false. When the recorded coverage shows
    scope rows but zero scored days within the burn-in window, the
    blocker is the burn-in itself. The scoring-start date is derived
    from the recorded features cutoff: scope day 1 is the first full
    day after the cutoff (the column landed 2026-11-20T22:24Z), and
    the first scored day is scope day BURN_IN_DAYS + 1. Without a
    parseable cutoff the phrase carries no date rather than a
    guessed one.
    """
    coverage = detail.get("coverage")
    if not isinstance(coverage, dict):
        return None
    try:
        scope_rows = int(coverage.get("scope_rows") or 0)
        scope_days = int(coverage.get("scope_days") or 0)
        scored_days = int(coverage.get("scored_days") or 0)
    except (TypeError, ValueError):
        return None
    if scored_days != 0 or scope_rows <= 0 or scope_days > BURN_IN_DAYS:
        return None
    cutoff = coverage.get("features_cutoff")
    if isinstance(cutoff, str):
        try:
            start = (datetime.fromisoformat(cutoff).date()
                     + timedelta(days=1 + BURN_IN_DAYS))
        except ValueError:
            start = None
        if start is not None:
            return f"in walk-forward burn-in; scoring starts {start.isoformat()}"
    return "in walk-forward burn-in; no scored days yet"


def _scope_progress(detail: dict) -> str | None:
    """What a candidate's recorded scope actually holds, e.g.
    "scope 29,507 rows · 5 days · 0 scored days" -- the antidote to
    floor progress that shows only zero scored counts."""
    coverage = detail.get("coverage")
    if not isinstance(coverage, dict):
        return None
    try:
        scope_rows = int(coverage.get("scope_rows") or 0)
        scope_days = int(coverage.get("scope_days") or 0)
        scored_days = int(coverage.get("scored_days") or 0)
    except (TypeError, ValueError):
        return None
    if scope_rows <= 0:
        return None
    return (f"scope {scope_rows:,} rows · {scope_days} days · "
            f"{scored_days} scored days")
_FLOOR_LABELS = (("calls", "calls"), ("call_days", "days"),
                 ("days", "days"), ("rows", "rows"),
                 ("clusters", "clusters"))


def _floor_progress(floors: object) -> tuple[dict | None, str | None]:
    """Normalise a recorded floors dict to {label: {have, need}} plus
    a compact "calls 30/250 · days 5/7" progress string."""
    if not isinstance(floors, dict):
        return None, None
    out: dict[str, dict] = {}
    for key, label in _FLOOR_LABELS:
        entry = floors.get(key)
        if isinstance(entry, dict) and label not in out:
            try:
                out[label] = {"have": int(entry.get("have") or 0),
                              "need": int(entry.get("need") or 0)}
            except (TypeError, ValueError):
                continue
    if not out:
        return None, None
    text = " · ".join(f"{label} {v['have']:,}/{v['need']:,}"
                      for label, v in out.items())
    return out, text


def _candidate_blocker(candidate_id: str, status: str | None,
                       detail: dict) -> str | None:
    """The specific reason a candidate is not measured, or None."""
    if status not in ("NOT_MEASURABLE", "DATA-BLOCKED"):
        return None
    reason = detail.get("reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    cause = detail.get("cause")
    if isinstance(cause, str) and cause in CAUSE_TEXT:
        return CAUSE_TEXT[cause]
    if status == "DATA-BLOCKED":
        return DATA_BLOCKED_TEXT.get(candidate_id)
    _, progress = _floor_progress(detail.get("floors"))
    if progress:
        return "below its measurement floors"
    return None


def _candidate_blocker_short(candidate_id: str, status: str | None,
                             detail: dict) -> str | None:
    """The canonical short form of a candidate's blocker, or None.

    Mirrors :func:`_candidate_blocker`'s resolution order but only
    ever returns a complete registered phrase (or a recorded reason
    short enough to be complete as recorded) -- never a truncation.
    """
    if status not in ("NOT_MEASURABLE", "DATA-BLOCKED"):
        return None
    reason = detail.get("reason")
    if isinstance(reason, str) and reason.strip():
        reason = reason.strip()
        if len(reason) <= _REASON_SHORT_LIMIT:
            return reason
    cause = detail.get("cause")
    if isinstance(cause, str) and cause in CAUSE_SHORT_TEXT:
        return CAUSE_SHORT_TEXT[cause]
    if candidate_id in DATA_BLOCKED_SHORT_TEXT:
        return DATA_BLOCKED_SHORT_TEXT[candidate_id]
    burn_in = _burn_in_short(detail)
    if burn_in:
        return burn_in
    _, progress = _floor_progress(detail.get("floors"))
    if progress:
        return FLOOR_BLOCKER_SHORT
    if (isinstance(reason, str) and reason.strip()
            or isinstance(cause, str) and cause in CAUSE_TEXT
            or status == "DATA-BLOCKED"
            and candidate_id in DATA_BLOCKED_TEXT):
        return GENERIC_BLOCKER_SHORT
    return None


def read_results_for_board(store) -> dict:
    """Latest recorded result per candidate, newest first.

    A missing results table is a true "no results yet" (the workbench
    has not recorded a run); any other read failure raises, so the
    board shows "couldn't load" rather than a false zero. Each
    candidate carries its specific blocker and floor progress when it
    is not measured, resolved from the recorded detail -- the boards
    never show a bare aggregate count for the limbo pile.
    """
    if not _results_table_exists(store):
        return {"candidates": []}
    rows = list(wb._stream(
        store,
        f"SELECT candidate_id, screen, factory, display, status,"
        f" verdict, graded_rows, calls, days, evaluated_at, detail_json"
        f" FROM {RESULTS_TABLE} ORDER BY evaluated_at DESC", ()))
    latest: dict[str, dict] = {}
    for raw in rows:
        item = dict(raw)
        if item["candidate_id"] not in latest:
            try:
                detail = json.loads(item.get("detail_json") or "{}")
            except (TypeError, ValueError):
                detail = {}
            if not isinstance(detail, dict):
                detail = {}
            floors, progress = _floor_progress(detail.get("floors"))
            scope = _scope_progress(detail)
            if scope and _burn_in_short(detail):
                # In burn-in the scored floors are all zero by
                # construction; the scope it actually holds is the
                # honest progress display.
                progress = scope
            elif scope and progress:
                progress = f"{scope} · {progress}"
            elif scope:
                progress = scope
            latest[item["candidate_id"]] = {
                "candidate_id": item["candidate_id"],
                "screen": item["screen"], "factory": item["factory"],
                "display": item["display"], "status": item["status"],
                "verdict": item["verdict"],
                "graded_rows": int(item["graded_rows"]),
                "calls": int(item["calls"]), "days": int(item["days"]),
                "evaluated_at": item["evaluated_at"],
                "blocker": _candidate_blocker(
                    item["candidate_id"], item["status"], detail),
                "blocker_short": _candidate_blocker_short(
                    item["candidate_id"], item["status"], detail),
                "cause": (detail.get("cause")
                          if isinstance(detail.get("cause"), str)
                          else None),
                "grading_version": (detail.get("grading_version")
                                    if isinstance(detail.get("grading_version"), str)
                                    else None),
                "cluster_unit": ((detail.get("coverage") or {}).get("cluster_unit")
                                 if isinstance(detail.get("coverage"), dict)
                                 else None),
                "floors": floors,
                "progress": progress,
            }
    return {"candidates": list(latest.values())}


def import_phase1_reports(reports: list[dict]) -> list[dict]:
    """Results-table entries for phase-1 verdicts, from their report
    files. Import RECORDS the verdicts as computed once under design
    section 4.8; it recomputes nothing."""
    factories = {c["id"]: c["factory"] for c in wb.REGISTRY}
    by_screen = {c["screen"]: c["id"] for c in wb.REGISTRY
                 if c["factory"] == "xmarket"}
    entries: list[dict] = []
    for report in reports:
        evaluated_at = str(report.get("evaluated_at") or "")
        screen = str(report.get("screen") or "")
        if screen == "R1":
            for cid, cand in (report.get("candidates") or {}).items():
                score = cand.get("score") or {}
                entries.append({
                    "candidate_id": cid, "screen": "R1",
                    "factory": factories.get(cid, "lab"),
                    "display": PHASE1_DISPLAY.get(cid, cid),
                    "status": cand.get("status"), "verdict": cand.get("verdict"),
                    "graded_rows": score.get("rows") or 0,
                    "calls": (score.get("calls") or {}).get("n") or 0,
                    "days": score.get("days") or 0,
                    "evaluated_at": evaluated_at,
                    "detail": {"legs": cand.get("legs"),
                               "imported_from": "phase1-report"},
                })
        elif screen == "X1-X5":
            for scope in (report.get("scopes") or {}).values():
                cid = by_screen.get(str(scope.get("screen") or ""))
                if cid is None:
                    continue
                score = scope.get("score") or {}
                entries.append({
                    "candidate_id": cid, "screen": str(scope.get("screen")),
                    "factory": "xmarket",
                    "display": PHASE1_DISPLAY.get(cid, cid),
                    "status": scope.get("status"),
                    "verdict": scope.get("verdict"),
                    "graded_rows": score.get("rows") or 0,
                    "calls": (score.get("calls") or {}).get("n") or 0,
                    "days": score.get("days") or 0,
                    "evaluated_at": evaluated_at,
                    "detail": {"artifact_sha256": scope.get("artifact_sha256"),
                               "imported_from": "phase1-report"},
                })
        elif screen == "R3":
            pooled = report.get("pooled") or {}
            entries.append({
                "candidate_id": "cb-premium-v1", "screen": "R3",
                "factory": factories.get("cb-premium-v1", "screen"),
                "display": PHASE1_DISPLAY["cb-premium-v1"],
                "status": report.get("status"),
                "verdict": report.get("verdict"),
                "graded_rows": pooled.get("rows") or 0,
                "calls": pooled.get("calls") or 0,
                "days": pooled.get("days") or 0,
                "evaluated_at": evaluated_at,
                "detail": {"imported_from": "phase1-report"},
            })
        elif screen == "R2":
            detail = {"imported_from": "phase1-report"}
            if isinstance(report.get("reason"), str) and report["reason"]:
                detail["reason"] = report["reason"]
            entries.append({
                "candidate_id": "liq-cascade-v1", "screen": "R2",
                "factory": factories.get("liq-cascade-v1", "lab"),
                "display": PHASE1_DISPLAY["liq-cascade-v1"],
                "status": report.get("status") or "DATA-BLOCKED",
                "verdict": None, "graded_rows": 0, "calls": 0, "days": 0,
                "evaluated_at": evaluated_at,
                "detail": detail,
            })
    return entries


def generation_registry() -> dict:
    """The phase-2 registry: grid candidates + estate registrations.

    LLM intake entries are dynamic (they come from the stored proposal
    set) and are produced by llm_registrations() at run time.
    """
    candidates = []
    for cand in MASK_CANDIDATES + CONDITIONING_CANDIDATES + ESTATE_CANDIDATES:
        entry = {k: cand[k] for k in ("id", "factory", "screen", "display",
                                      "mechanism")}
        if "mask" in cand:
            entry["mask"] = list(cand["mask"])
        if "horizon" in cand:
            entry["horizon"] = cand["horizon"]
        if "rule" in cand:
            entry["rule"] = cand["rule"]
        candidates.append(entry)
    return {"design": wb.DESIGN, "amendment": AMENDMENT,
            "candidates": candidates}


def base_report(screen: str, *, now: datetime) -> dict:
    report = wb.base_report(screen, now=now)
    report["amendment"] = AMENDMENT
    return report
