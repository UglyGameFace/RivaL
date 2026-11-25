"""Offline replay workbench: the Model Lab's grading engine, re-pointed.

Design: ``rival-replay-workbench-design-20261120.md`` (owner-approved
2026-11-20). The lab is a model factory: candidate designs from either
factory -- the Model Lab or XMarket -- are graded OFFLINE against the
settled record RivaL already owns, paired against the market's own
issuance-time price, after fees. A PASS licenses only a proposal for a
registered prospective test under the unchanged qualification gates.
This module issues nothing, writes nothing to any store, and is never
wired into a bot loop, a scheduler, or a Discord surface; it is run
manually (``tools/replay_workbench.py``), in the shape of the
convergence and fair-value evaluators.

Frozen rules (design sections 4-5 -- changing any number here is a NEW
registration, not an edit):

- Decision-time information only; walk-forward fitting with a full
  UTC day of purge/embargo (a map for score day D is fit only on rows
  whose outcomes were observed before D begins).
- Paired grading against the issuance-time frozen baseline, never a
  reconstructed price. Fees per contract: the row's recorded
  ``fee_rate`` where present, else the dated published schedule; the
  mix is reported. Shadow-ledger calls carry no asks and are priced at
  the baseline-implied price, labelled IMPLIED-PRICE (upper bound).
- Exclusions are counted, never imputed. Below a screen's floors the
  verdict is NOT MEASURABLE -- structurally unable to print PASS/KILL.
"""
from __future__ import annotations

import bisect
import json
import math
import random
from datetime import UTC, datetime, time, timedelta

from parlay_bot.perpetuals.polymarket_learning import (
    MODEL,
    polymarket_fee,
    polymarket_scheduled_fee_rate,
)

DESIGN = "rival-replay-workbench-design-20261120"
BOOTSTRAP_REPLICATES = 2000
BOOTSTRAP_SEED = 20261120
PROBABILITY_EPSILON = 1e-9
FETCH_CHUNK = 2000

# --- Screen R1 (recalibration) frozen criteria, design 5.1 ---
RECAL_BURN_IN_DAYS = 7
RECAL_MIN_FIT_ROWS = 1000
RECAL_MIN_SCORED_ROWS = 2500
RECAL_MIN_SCORED_DAYS = 7
RECAL_MIN_CALLS = 250
RECAL_VARIANTS = (
    "recal-raw", "recal-platt", "recal-isotonic",
    "recal-shrink50", "recal-shrink25",
)
RECAL_MIN_CLUSTERS = 100
# Corrected Brier leg (research sweep #3, 2026-11-20; crypto-zero audit
# 2026-11-24 section 2): the old PASS_BRIER = -0.005 point leg demanded
# a ~7.07pp corrected bias -- correcting bias b gains b^2 in Brier
# (Murphy decomposition) -- when every bias ever measured is 1-3pp.
# The template leg is now the CI form: delta_brier < 0 with its
# day-cluster interval entirely below 0, the same discipline as leg 1.
# Completed screens' saved verdicts were produced under the legs frozen
# at their run time and are never recomputed.
# A chronological half whose call mean rests on fewer calls than this
# is not evidence; the both-halves call leg floors each half separately
# (audit F4: X2's second half passed on a single call).
MIN_CALLS_PER_HALF = 25

# --- Screen R3 (Coinbase premium) frozen criteria, design 5.3 ---
PREMIUM_BAND = 0.0010
PREMIUM_MIN_CALLS = 250
PREMIUM_MIN_DAYS = 7

# --- Screens X1-X5 (XMarket fits) frozen criteria, design 5.4 ---
XMARKET_FAMILY = "xmarket_v1"
XMARKET_MIN_ROWS = 1000
XMARKET_MIN_DAYS = 4
XMARKET_MIN_CALLS = 100
XMARKET_MIN_CLUSTERS = 40
XMARKET_VALUE_EDGE = 0.05
XMARKET_CALL_BUFFER = 0.025
XMARKET_SCOPES = (
    {"screen": "X1", "asset": "BTC", "horizon": 15,
     "sha_prefix": "6cf3886d42f7"},
    {"screen": "X2", "asset": "BTC", "horizon": 60,
     "sha_prefix": "425d4990003e"},
    {"screen": "X3", "asset": "SOL", "horizon": 15,
     "sha_prefix": "43e5cce04729"},
    {"screen": "X4", "asset": "XRP", "horizon": 15,
     "sha_prefix": "182bbf474664"},
    {"screen": "X5", "asset": "XRP", "horizon": 60,
     "sha_prefix": "b799ee12dd4b"},
)

# The phase-1 candidate registry (design section 3). Every entry names
# its factory, its mechanism or receipt, and its frozen screen. The
# XMarket SHA prefixes below are completed by the loader's prefix
# match against the imported documents (section 5.4 of the design
# froze the prefixes; the full SHA is read from rival_xmarket_models
# at run time and reported, never trusted from this file).
REGISTRY = (
    {"id": "recal-raw", "factory": "screen", "screen": "R1",
     "mechanism": "Control: the live model's raw probabilities, "
                  "unmodified. Every recalibration variant is graded "
                  "against the same scored rows."},
    {"id": "recal-platt", "factory": "lab", "screen": "R1",
     "mechanism": "Platt scaling of the live model's probabilities, "
                  "fit walk-forward on RivaL's own settled calls "
                  "(survey #2 section 2b: recalibration layer, real "
                  "fees, fit on own settled record)."},
    {"id": "recal-isotonic", "factory": "lab", "screen": "R1",
     "mechanism": "Isotonic (PAVA) recalibration, same walk-forward "
                  "fit as recal-platt; the non-parametric counterpart."},
    {"id": "recal-shrink50", "factory": "lab", "screen": "R1",
     "mechanism": "Fixed shrinkage of the model's deviation from the "
                  "market baseline, s=0.50 (survey #2 section 2b: "
                  "corrections must be shrunk hard toward the market)."},
    {"id": "recal-shrink25", "factory": "lab", "screen": "R1",
     "mechanism": "Fixed shrinkage toward the market baseline, "
                  "s=0.25 -- the harder shrink."},
    {"id": "liq-cascade-v1", "factory": "lab", "screen": "R2",
     "mechanism": "Liquidation cascades at 5-15m horizons "
                  "(receipt-backed candidate input, method survey "
                  "synthesis); continuation vs snap-back untested.",
     "status": "DATA-BLOCKED"},
    {"id": "cb-premium-v1", "factory": "screen", "screen": "R3",
     "mechanism": "Coinbase-vs-offshore premium as a US-demand "
                  "regime gauge (method survey synthesis), used as a "
                  "conditioning screen on the live model's own calls."},
) + tuple(
    {"id": f"xmarket-{s['asset'].lower()}-{s['horizon']}m-{s['sha_prefix'][:6]}",
     "factory": "xmarket", "screen": s["screen"],
     "mechanism": "XMarket cross-market fit (step-8 pipeline artifact, "
                  "pinned per scope); graded on its prospective shadow "
                  "record against the issuance-time market baseline.",
     "asset": s["asset"], "horizon": s["horizon"],
     "sha_prefix": s["sha_prefix"]}
    for s in XMARKET_SCOPES
)


def _parse_time(value) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if at.utcoffset() is None:
        return None
    return at.astimezone(UTC)


def _clamp(p: float) -> float:
    return min(max(p, PROBABILITY_EPSILON), 1.0 - PROBABILITY_EPSILON)


def _log_loss(p: float, y: int) -> float:
    p = _clamp(p)
    return -(y * math.log(p) + (1 - y) * math.log(1.0 - p))


def _mean(values) -> float | None:
    return sum(values) / len(values) if values else None


def _day_cluster_interval(observations, key: str, *,
                          alpha: float = 0.05) -> dict | None:
    """Day-clustered bootstrap 95% interval for a mean (seeded)."""
    by_day: dict[str, list[float]] = {}
    for obs in observations:
        value = obs.get(key)
        if value is not None:
            by_day.setdefault(obs["day"], []).append(value)
    days = sorted(by_day)
    if len(days) < 2:
        return None
    rng = random.Random(BOOTSTRAP_SEED)
    means = []
    for _ in range(BOOTSTRAP_REPLICATES):
        total = 0.0
        count = 0
        for _ in range(len(days)):
            values = by_day[rng.choice(days)]
            total += sum(values)
            count += len(values)
        means.append(total / count)
    means.sort()
    return {"lower": means[int((alpha / 2) * len(means))],
            "upper": means[min(len(means) - 1,
                               int((1 - alpha / 2) * len(means)))],
            "days": len(days), "replicates": len(means)}


def _halves(observations) -> tuple[list, list]:
    ordered = sorted(observations, key=lambda o: o["issued_at"])
    split = len(ordered) // 2
    return ordered[:split], ordered[split:]


def _call_block(observations) -> dict:
    calls = [o for o in observations if o.get("call_net") is not None]
    if not calls:
        return {"n": 0, "mean_net": None, "win_rate": None,
                "breakeven_win_rate": None}
    return {
        "n": len(calls),
        "mean_net": _mean([o["call_net"] for o in calls]),
        "win_rate": sum(1 for o in calls if o["call_net"] > 0) / len(calls),
        "breakeven_win_rate": _mean([o["call_cost"] for o in calls]),
    }


def _span_days(observations) -> float | None:
    if not observations:
        return None
    stamps = [o["issued_at"] for o in observations]
    return (max(stamps) - min(stamps)).total_seconds() / 86400.0


def _cluster_count(observations) -> int | None:
    """Distinct independent units (market slugs / target windows).

    None when any observation lacks a cluster key: a partial count
    would understate concentration, so the block reports unknown
    rather than a wrong number (audit F3: floors counted calls while
    power lives in clusters)."""
    if not observations or any(
            o.get("cluster") is None for o in observations):
        return None
    return len({o["cluster"] for o in observations})


def _stratified(observations) -> dict:
    """Per-key paired cuts, REPORTING ONLY -- verdicts never read this.

    A pooled number hid the BTC-EOD split (Above -0.0196 / Dip -0.0058
    / Reach +0.0085 pooled to -0.00327); every key carried by all
    observations gets its own cut so a split is visible on the face of
    the report (audit F5). A key missing from any observation is
    skipped rather than silently dropping rows."""
    out: dict = {}
    for key in ("asset", "horizon", "contract_type"):
        if any(o.get(key) is None for o in observations):
            continue
        groups: dict[str, list] = {}
        for o in observations:
            groups.setdefault(str(o[key]), []).append(o)
        if len(groups) < 2:
            continue
        out[key] = {
            name: {
                "rows": len(group),
                "clusters": _cluster_count(group),
                "delta_log_loss": _mean(
                    [x["delta_log_loss"] for x in group]),
                "delta_brier": _mean([x["delta_brier"] for x in group]),
                "calls": _call_block(group),
            }
            for name, group in sorted(groups.items())
        }
    return out


def _paired_block(observations, *, family_size: int = 1) -> dict:
    """Paired-grading metrics over graded observations (fair-value shape).

    family_size is the number of candidates graded as one screen
    family; the day-cluster intervals use the Sidak-adjusted alpha
    1-(1-0.05)**(1/family_size) so a family of screens does not spend
    the same 5% on every candidate (the Model Lab search_bar pattern,
    extended to this template)."""
    n = len(observations)
    if not n:
        return {"rows": 0, "days": 0, "clusters": None,
                "delta_log_loss": None,
                "delta_log_loss_ci95": None, "delta_brier": None,
                "delta_brier_ci95": None, "chronological_halves": None,
                "stratified": {}, "calls": _call_block(observations)}
    alpha = (1.0 - (1.0 - 0.05) ** (1.0 / family_size)
             if family_size > 1 else 0.05)
    first, second = _halves(observations)
    first_calls = _call_block(first)
    second_calls = _call_block(second)
    return {
        "rows": n,
        "days": len({o["day"] for o in observations}),
        "clusters": _cluster_count(observations),
        "delta_log_loss": _mean([o["delta_log_loss"] for o in observations]),
        "delta_log_loss_ci95": _day_cluster_interval(
            observations, "delta_log_loss", alpha=alpha),
        "delta_brier": _mean([o["delta_brier"] for o in observations]),
        "delta_brier_ci95": _day_cluster_interval(
            observations, "delta_brier", alpha=alpha),
        "chronological_halves": {
            "first_delta_log_loss": _mean(
                [o["delta_log_loss"] for o in first]),
            "second_delta_log_loss": _mean(
                [o["delta_log_loss"] for o in second]),
            "first_call_mean_net": first_calls["mean_net"],
            "second_call_mean_net": second_calls["mean_net"],
            "first_call_n": first_calls["n"],
            "second_call_n": second_calls["n"],
            "first_n": len(first), "second_n": len(second),
            # Halves split by count (the frozen screens' method); the
            # spans are reported so a half covering a much shorter
            # clock span than the other is visible, not hidden.
            "first_span_days": _span_days(first),
            "second_span_days": _span_days(second),
        },
        "stratified": _stratified(observations),
        "calls": _call_block(observations),
    }


def _four_leg_verdict(block: dict, *, min_rows: int, min_days: int,
                      min_calls: int, min_clusters: int = 0,
                      min_calls_per_half: int = MIN_CALLS_PER_HALF
                      ) -> dict:
    """The four-leg verdict template shared by screens R1, X1-X5, G-A.

    Legs corrected 2026-11-24 (defect sweep): leg 2 is the CI form of
    the Brier leg (the old -0.005 point leg was mis-scaled, see the
    note at RECAL_MIN_CLUSTERS), maturity additionally requires a
    minimum number of independent clusters and a minimum call count
    in EACH chronological half. Completed screens' saved verdicts were
    produced under the legs frozen at their run time; this template
    governs future screens only."""
    halves = block["chronological_halves"] or {}
    floors = {
        "graded_rows": {"have": block["rows"], "need": min_rows},
        "distinct_days": {"have": block["days"], "need": min_days},
        "calls": {"have": block["calls"]["n"], "need": min_calls},
        "clusters": {"have": block.get("clusters"), "need": min_clusters},
        "calls_first_half": {"have": halves.get("first_call_n") or 0,
                             "need": min_calls_per_half},
        "calls_second_half": {"have": halves.get("second_call_n") or 0,
                              "need": min_calls_per_half},
    }
    mature = (block["rows"] >= min_rows and block["days"] >= min_days
              and block["calls"]["n"] >= min_calls
              and (min_clusters == 0
                   or (block.get("clusters") is not None
                       and block["clusters"] >= min_clusters))
              and (halves.get("first_call_n") or 0) >= min_calls_per_half
              and (halves.get("second_call_n") or 0) >= min_calls_per_half)
    if not mature:
        return {"status": "NOT_MEASURABLE", "verdict": None,
                "floors": floors, "legs": None}
    ci = block["delta_log_loss_ci95"] or {}
    brier_ci = block["delta_brier_ci95"] or {}
    legs = {
        "paired_log_loss": bool(
            block["delta_log_loss"] is not None
            and block["delta_log_loss"] < 0
            and ci.get("upper") is not None and ci["upper"] < 0),
        "paired_brier": bool(
            block["delta_brier"] is not None
            and block["delta_brier"] < 0
            and brier_ci.get("upper") is not None
            and brier_ci["upper"] < 0),
        "both_halves_log_loss": bool(
            (halves.get("first_delta_log_loss") or 0.0) < 0
            and (halves.get("second_delta_log_loss") or 0.0) < 0),
        "both_halves_call_net": bool(
            halves.get("first_call_mean_net") is not None
            and halves["first_call_mean_net"] > 0
            and halves.get("second_call_mean_net") is not None
            and halves["second_call_mean_net"] > 0),
    }
    return {"status": "EVALUATED",
            "verdict": "PASS" if all(legs.values()) else "KILL",
            "floors": floors, "legs": legs}


# ---------------------------------------------------------------------------
# Issuance-book pricing (design 4.4). Mirrors polymarket_learning's
# after_fee_call_net book verification exactly; the only difference is
# the fee basis: the row's recorded fee_rate where present, else the
# dated schedule. A unit test pins equality with after_fee_call_net
# whenever fee_rate is None.
# ---------------------------------------------------------------------------

def book_prices(features, market_baseline) -> tuple[float | None, float | None]:
    """(up_price, down_price) depth-average asks from the verified book.

    The stored observation is trusted as the issuance book only when
    its implied baseline equals the frozen market_baseline (the
    production rule); anything else returns (None, None).
    """
    try:
        values = [float(v) for v in features]
        if len(values) < 6:
            return None, None
        up_ask, down_ask = values[0], values[1]
        if not (math.isfinite(up_ask) and math.isfinite(down_ask)) \
                or up_ask + down_ask <= 0:
            return None, None
        if abs(up_ask / (up_ask + down_ask) - float(market_baseline)) > 1e-9:
            return None, None
        prices = []
        for price in (up_ask + values[4], down_ask + values[5]):
            prices.append(
                price if math.isfinite(price) and 0 < price < 1 else None)
        return prices[0], prices[1]
    except (TypeError, ValueError, IndexError):
        return None, None


def _fee_for(row: dict, price: float) -> float | None:
    rate = row.get("fee_rate")
    try:
        if rate is None:
            rate = polymarket_scheduled_fee_rate(
                asset=row["asset"], horizon=int(row["horizon"]),
                issued_at=row["issued_at"])
        return polymarket_fee(price, float(rate))
    except (TypeError, ValueError):
        return None


def call_for(row: dict, probability: float) -> tuple[float, float] | None:
    """(net, cost) of a call favouring ``probability``'s side, or None.

    ``row`` carries the precomputed book prices (``up_price`` /
    ``down_price``), the label (``direction``), and the fee basis.
    No call when the probability is exactly 0.5 or the favoured side's
    book price is unverifiable -- the production rule.
    """
    if probability == 0.5:
        return None
    up = probability > 0.5
    price = row["up_price"] if up else row["down_price"]
    if price is None:
        return None
    fee = _fee_for(row, price)
    if fee is None:
        return None
    won = (row["direction"] == "UP") == up
    return (1.0 if won else 0.0) - price - fee, price + fee


# ---------------------------------------------------------------------------
# Recalibration maps (design 5.1). Pure stdlib: Platt by Newton
# iterations on logit(p); isotonic by pool-adjacent-violators.
# ---------------------------------------------------------------------------

def _logit(p: float) -> float:
    p = _clamp(p)
    return math.log(p / (1.0 - p))


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


def fit_platt(pairs) -> tuple[float, float]:
    """Platt (a, b): g(p) = sigmoid(a * logit(p) + b), Newton-fit."""
    pairs = list(pairs)
    if not pairs:
        return 1.0, 0.0
    xs = [_logit(p) for p, _ in pairs]
    ys = [float(y) for _, y in pairs]
    a, b = 1.0, 0.0
    for _ in range(50):
        ga = gb = haa = hab = hbb = 0.0
        for x, y in zip(xs, ys, strict=True):
            q = _sigmoid(a * x + b)
            d = q - y
            ga += d * x
            gb += d
            w = q * (1.0 - q)
            haa += w * x * x
            hab += w * x
            hbb += w
        det = haa * hbb - hab * hab
        if det <= 1e-12:
            break
        da = (hbb * ga - hab * gb) / det
        db = (-hab * ga + haa * gb) / det
        a = min(max(a - da, -50.0), 50.0)
        b = min(max(b - db, -50.0), 50.0)
        if abs(da) < 1e-10 and abs(db) < 1e-10:
            break
    return a, b


def apply_platt(p: float, a: float, b: float) -> float:
    return _sigmoid(a * _logit(p) + b)


def fit_isotonic(pairs) -> list[tuple[float, float]]:
    """PAVA blocks: [(block_max_p, block_value)] ascending in p."""
    totals: dict[float, list[float]] = {}
    for p, y in pairs:
        slot = totals.setdefault(float(p), [0.0, 0.0])
        slot[0] += float(y)
        slot[1] += 1.0
    blocks: list[list[float]] = []
    for p in sorted(totals):
        total, count = totals[p]
        blocks.append([p, total, count])
        while len(blocks) >= 2 and (
                blocks[-2][1] / blocks[-2][2]
                > blocks[-1][1] / blocks[-1][2]):
            top = blocks.pop()
            prev = blocks.pop()
            blocks.append([top[0], prev[1] + top[1], prev[2] + top[2]])
    return [(p, total / count) for p, total, count in blocks]


def apply_isotonic(blocks, p: float) -> float:
    if not blocks:
        return p
    xs = [x for x, _ in blocks]
    idx = bisect.bisect_left(xs, p)
    if idx >= len(blocks):
        return blocks[-1][1]
    return blocks[idx][1]


def shrink(p: float, baseline: float, s: float) -> float:
    """g(p) = baseline + s * (p - baseline): shrink toward the market."""
    return baseline + s * (p - baseline)


# ---------------------------------------------------------------------------
# Screen R1 -- recalibration (design 5.1). Pure: rows are the loader's
# eligible settled record (see load_live_record for the row shape).
# ---------------------------------------------------------------------------

def _variant_probability(variant: str, row: dict, *, platt, isotonic) -> float:
    p = float(row["probability_up"])
    if variant == "recal-raw":
        return p
    if variant == "recal-platt":
        return apply_platt(p, platt[0], platt[1])
    if variant == "recal-isotonic":
        return apply_isotonic(isotonic, p)
    if variant == "recal-shrink50":
        return shrink(p, float(row["market_baseline"]), 0.50)
    if variant == "recal-shrink25":
        return shrink(p, float(row["market_baseline"]), 0.25)
    raise ValueError(f"unknown recalibration variant {variant}")


def screen_recalibration(rows: list[dict]) -> dict:
    """Walk-forward recalibration verdicts per variant (design 5.1)."""
    rows = sorted(rows, key=lambda r: r["issued_at"])
    days = sorted({r["day"] for r in rows})
    score_days = days[RECAL_BURN_IN_DAYS:]
    observations: dict[str, list[dict]] = {v: [] for v in RECAL_VARIANTS}
    platt_by_day: dict[str, list[float]] = {}
    unscored_fit_floor = 0
    scored_days = 0
    for day in score_days:
        day_start = datetime.combine(
            datetime.fromisoformat(day).date(), time.min, tzinfo=UTC)
        fit_rows = [r for r in rows
                    if r["outcome_observed_at"] < day_start]
        if len(fit_rows) < RECAL_MIN_FIT_ROWS:
            unscored_fit_floor += 1
            continue
        scored_days += 1
        pairs = [(float(r["probability_up"]), int(r["label"]))
                 for r in fit_rows]
        platt = fit_platt(pairs)
        isotonic = fit_isotonic(pairs)
        platt_by_day[day] = [platt[0], platt[1]]
        for row in (r for r in rows if r["day"] == day):
            y = int(row["label"])
            baseline = float(row["market_baseline"])
            for variant in RECAL_VARIANTS:
                g = _clamp(_variant_probability(
                    variant, row, platt=platt, isotonic=isotonic))
                call = call_for(row, g)
                observations[variant].append({
                    "issued_at": row["issued_at"], "day": row["day"],
                    "delta_log_loss": _log_loss(g, y) - _log_loss(baseline, y),
                    "delta_brier": (g - y) ** 2 - (baseline - y) ** 2,
                    "call_net": call[0] if call else None,
                    "call_cost": call[1] if call else None,
                    "cluster": row["slug"], "asset": row["asset"],
                    "horizon": row["horizon"],
                })
    candidates = {}
    for variant in RECAL_VARIANTS:
        block = _paired_block(observations[variant],
                              family_size=len(RECAL_VARIANTS))
        verdict = _four_leg_verdict(
            block, min_rows=RECAL_MIN_SCORED_ROWS,
            min_days=RECAL_MIN_SCORED_DAYS, min_calls=RECAL_MIN_CALLS,
            min_clusters=RECAL_MIN_CLUSTERS)
        candidates[variant] = {**verdict, "score": block}
    return {
        "screen": "R1",
        "coverage": {
            "record_rows": len(rows),
            "record_days": len(days),
            "score_days_available": len(score_days),
            "scored_days": scored_days,
            "unscored_days_fit_floor": unscored_fit_floor,
            "fee_basis": _fee_basis(rows),
        },
        "platt_parameters_by_day": platt_by_day,
        "candidates": candidates,
    }


def _fee_basis(rows: list[dict]) -> dict:
    recorded = sum(1 for r in rows if r.get("fee_rate") is not None)
    return {"recorded_fee_rate": recorded,
            "dated_schedule": len(rows) - recorded}


# ---------------------------------------------------------------------------
# Screen R3 -- Coinbase premium conditioning (design 5.3). Pure: rows
# are the loader's eligible BTC record; premium_points are (close_at,
# premium) pairs for 5-minute candles closed at or before issuance.
# ---------------------------------------------------------------------------

def screen_premium(rows: list[dict], premium_points) -> dict:
    points = sorted(premium_points)
    closes = [stamp for stamp, _ in points]
    regimes: dict[str, list[dict]] = {"POS": [], "NEG": [], "MID": []}
    pooled: list[dict] = []
    excluded_no_premium = 0
    for row in sorted(rows, key=lambda r: r["issued_at"]):
        idx = bisect.bisect_right(closes, row["issued_at"]) - 1
        if idx < 0:
            excluded_no_premium += 1
            continue
        premium = points[idx][1]
        regime = ("POS" if premium >= PREMIUM_BAND
                  else "NEG" if premium <= -PREMIUM_BAND else "MID")
        call = call_for(row, float(row["probability_up"]))
        obs = {"issued_at": row["issued_at"], "day": row["day"],
               "premium": premium,
               "delta_log_loss": (
                   _log_loss(float(row["probability_up"]), int(row["label"]))
                   - _log_loss(float(row["market_baseline"]),
                               int(row["label"]))),
               "call_net": call[0] if call else None,
               "call_cost": call[1] if call else None}
        regimes[regime].append(obs)
        pooled.append(obs)

    def _regime_block(obs_list) -> dict:
        calls = [o for o in obs_list if o["call_net"] is not None]
        return {
            "rows": len(obs_list),
            "days": len({o["day"] for o in obs_list}),
            "calls": len(calls),
            "call_days": len({o["day"] for o in calls}),
            "mean_net": _mean([o["call_net"] for o in calls]),
            "mean_net_ci95": _day_cluster_interval(calls, "call_net"),
            "delta_log_loss": _mean([o["delta_log_loss"] for o in obs_list]),
        }

    blocks = {name: _regime_block(obs) for name, obs in regimes.items()}
    pooled_block = _regime_block(pooled)
    qualifying = []
    for name in ("POS", "NEG"):
        block = blocks[name]
        ci = block["mean_net_ci95"] or {}
        if (block["calls"] >= PREMIUM_MIN_CALLS
                and block["call_days"] >= PREMIUM_MIN_DAYS
                and block["mean_net"] is not None and block["mean_net"] > 0
                and ci.get("lower") is not None and ci["lower"] > 0):
            qualifying.append(name)
    both_floored = all(
        blocks[name]["calls"] >= PREMIUM_MIN_CALLS
        and blocks[name]["call_days"] >= PREMIUM_MIN_DAYS
        for name in ("POS", "NEG"))
    if qualifying and pooled_block["mean_net"] is not None \
            and pooled_block["mean_net"] <= 0:
        status, verdict = "EVALUATED", "PASS"
    elif both_floored:
        status, verdict = "EVALUATED", "KILL"
    else:
        status, verdict = "NOT_MEASURABLE", None
    return {
        "screen": "R3",
        "status": status,
        "verdict": verdict,
        "qualifying_regimes": qualifying,
        "pooled": pooled_block,
        "regimes": blocks,
        "coverage": {"record_rows": len(rows),
                     "excluded_no_premium": excluded_no_premium},
    }


# ---------------------------------------------------------------------------
# Screens X1-X5 -- XMarket fits on the frozen shadow ledger (design
# 5.4). Pure: rows are raw ledger rows for one scope + pinned SHA.
# ---------------------------------------------------------------------------

def screen_xmarket(rows: list[dict], *, asset: str, horizon: int,
                   family_size: int = 1) -> dict:
    exclusions = {"outcome_missing": 0, "probability_out_of_range": 0,
                  "issued_after_target_close": 0}
    observations: list[dict] = []
    for row in rows:
        if row.get("settled_at") is None \
                or row.get("outcome_up") not in (0, 1):
            exclusions["outcome_missing"] += 1
            continue
        p = row.get("probability_up")
        baseline = row.get("baseline_up")
        if not (isinstance(p, (int, float)) and isinstance(baseline, (int, float))
                and 0 < float(p) < 1 and 0 < float(baseline) < 1):
            exclusions["probability_out_of_range"] += 1
            continue
        issued = row["issued_at"]
        target_close = row["target_opened_at"] + timedelta(minutes=horizon)
        if not issued < target_close:
            exclusions["issued_after_target_close"] += 1
            continue
        p, baseline = float(p), float(baseline)
        y = int(row["outcome_up"])
        call_net = call_cost = None
        if abs(p - baseline) >= XMARKET_VALUE_EDGE:
            up_value = p >= baseline
            side_p = p if up_value else 1.0 - p
            price = baseline if up_value else 1.0 - baseline
            try:
                fee = polymarket_fee(price, polymarket_scheduled_fee_rate(
                    asset=asset, horizon=horizon, issued_at=issued))
            except ValueError:
                fee = None
            if fee is not None and side_p - (price + fee) >= XMARKET_CALL_BUFFER:
                won = (y == 1) if up_value else (y == 0)
                call_net = (1.0 if won else 0.0) - price - fee
                call_cost = price + fee
        observations.append({
            "issued_at": issued, "day": issued.date().isoformat(),
            "delta_log_loss": _log_loss(p, y) - _log_loss(baseline, y),
            "delta_brier": (p - y) ** 2 - (baseline - y) ** 2,
            "call_net": call_net, "call_cost": call_cost,
            "cluster": str(row["target_opened_at"]),
            "asset": asset, "horizon": horizon,
        })
    block = _paired_block(observations, family_size=family_size)
    verdict = _four_leg_verdict(
        block, min_rows=XMARKET_MIN_ROWS, min_days=XMARKET_MIN_DAYS,
        min_calls=XMARKET_MIN_CALLS, min_clusters=XMARKET_MIN_CLUSTERS)
    return {**verdict, "score": block,
            "coverage": {"ledger_rows": len(rows), "exclusions": exclusions},
            "pricing": "IMPLIED-PRICE (baseline-implied; spread uncharged,"
                       " call nets are upper bounds)"}


# ---------------------------------------------------------------------------
# Loaders (SELECT-only, chunked streaming -- the PR #727 pattern).
# ---------------------------------------------------------------------------

def _stream(store, sql, params):
    with store.connect() as conn:
        cursor = conn.execute(sql, params)
        while True:
            chunk = cursor.fetchmany(FETCH_CHUNK)
            if not chunk:
                return
            yield from chunk


def load_live_record(store, *, asset: str | None = None) -> tuple[list[dict], dict]:
    """The live model's settled record (production summary() join rules).

    Returns (eligible rows, coverage). A row is eligible exactly when
    production can price it: the issuance book verifies against the
    frozen baseline and the favoured side's price is in (0, 1) -- the
    after_fee_call_net rule. Ineligible rows are counted, never priced
    from a wrong book.
    """
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
    )
    params: list = [MODEL]
    if asset is not None:
        sql += " AND v.asset=?"
        params.append(asset)
    sql += " ORDER BY f.issued_at, f.slug"
    rows: list[dict] = []
    coverage = {"settled_rows_seen": 0, "unpriceable_book": 0,
                "no_directional_call": 0, "unparseable_row": 0}
    for raw in _stream(store, sql, tuple(params)):
        coverage["settled_rows_seen"] += 1
        issued = _parse_time(raw["issued_at"])
        observed = _parse_time(raw["outcome_observed_at"])
        try:
            features = json.loads(raw["features_json"])
        except (TypeError, ValueError):
            features = None
        if issued is None or observed is None or features is None:
            coverage["unparseable_row"] += 1
            continue
        p = float(raw["probability_up"])
        up_price, down_price = book_prices(features, raw["market_baseline"])
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
        })
    coverage["eligible_rows"] = len(rows)
    return rows, coverage


def load_shadow_record(store, *, family: str, asset: str, horizon: int,
                       sha_prefix: str) -> tuple[list[dict], str | None]:
    """Frozen-ledger rows for one XMarket scope under its pinned SHA.

    The full SHA is resolved by prefix match against the scope's own
    ledger rows (the design froze the 12-character prefixes); a prefix
    matching zero or several SHAs resolves to None and no rows.
    """
    shas = [str(r["training_sha256"]) for r in _stream(
        store,
        "SELECT DISTINCT training_sha256 FROM rival_model_shadow_forecasts"
        " WHERE family=? AND asset=? AND horizon_minutes=?",
        (family, asset, int(horizon)))]
    matches = [s for s in shas if s.startswith(sha_prefix)]
    if len(matches) != 1:
        return [], None
    sha = matches[0]
    rows = []
    for raw in _stream(
            store,
            "SELECT issued_at, target_opened_at, probability_up,"
            " baseline_up, settled_at, outcome_up"
            " FROM rival_model_shadow_forecasts"
            " WHERE family=? AND asset=? AND horizon_minutes=?"
            " AND training_sha256=? ORDER BY issued_at",
            (family, asset, int(horizon), sha)):
        issued = _parse_time(raw["issued_at"])
        opened = _parse_time(raw["target_opened_at"])
        if issued is None or opened is None:
            continue
        rows.append({
            "issued_at": issued, "target_opened_at": opened,
            "probability_up": raw["probability_up"],
            "baseline_up": raw["baseline_up"],
            "settled_at": raw["settled_at"],
            "outcome_up": raw["outcome_up"],
        })
    return rows, sha


def registry_report() -> dict:
    """The phase-1 candidate registry (design section 3)."""
    return {"design": DESIGN, "candidates": list(REGISTRY)}


def base_report(screen: str, *, now: datetime) -> dict:
    return {
        "schema": "rival-replay-workbench-v1",
        "screen": screen,
        "evaluated_at": now.astimezone(UTC).isoformat(),
        "read_only": True,
        "real_money_orders": 0,
        "design": DESIGN,
    }
