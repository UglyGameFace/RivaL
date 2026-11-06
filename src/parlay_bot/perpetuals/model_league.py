"""Prospective, source-specific model contest on one immutable contract snapshot.

Brier vs contemporaneous normalized CLOB midpoints; economics at recorded
10-share ask depth + exact published fee. Fixed disjoint 28-day tests, daily
Newey-West loss differences, Holm across ALL enrolled models x 12 markets x
settlement schemas, and summable alpha spending across batches. No legacy
rows can be upgraded into matched evidence. See POLYMARKET_MODEL_CONTEST.md.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

ALPHA = .05
MIN_CONTRACTS = 300
MIN_DAYS = 21
MIN_TRADES = 150
MIN_COVERAGE = .8
WINDOW_DAYS = 28
# Prospective policy anchor, fixed before any eligible outcome is observed.
POLICY_START = datetime(2026, 11, 7, tzinfo=UTC)
SETTLEMENT_GRACE = timedelta(days=2)
CHECKPOINT_ELAPSED = .20
CHECKPOINT_WIDTH = timedelta(seconds=20)
EDGE = .03
STRESS = .01  # additional adverse cents/share, not a claim of actual fills
BOOK = "polymarket-terminal-book-logistic-v1"
FORMULA = "digital-gbm-ewma-v2"
JUMP = "digital-merton-moments-v1"
PRESSURE = "digital-queue-microprice-v1"
MODELS = (BOOK, FORMULA, JUMP, PRESSURE)
ASSETS = ("BTC", "ETH", "SOL", "XRP")
HORIZONS = (5, 15, 60)


def label(model: str) -> str:
    return {BOOK: "Book logistic", FORMULA: "GBM + EWMA", JUMP: "Jump diffusion",
            PRESSURE: "Queue pressure"}.get(model, "Pooled " + model[-8:] if model.startswith("pooled:") else model[:24])


def settlement_schema(notice) -> str | None:
    """Source identity + payoff, kept separate even within one coin/timeframe."""
    source = str(getattr(notice, "resolution_source", "") or "").lower()
    rules = str(getattr(notice, "rules", "") or "").lower()
    host = urlsplit(source).hostname
    twap = "twap" in rules or "time-weighted average" in rules
    if host == "data.chain.link" and notice.asset.lower() + "-usd" in source:
        if twap:
            if "twap-60s" not in source or re.search(r"60[ -]?(?:second|sec|s)", rules) is None:
                return None
            kind = "twap60"
        elif "twap" not in source and "chainlink" in rules:
            kind = "terminal"
        else:
            return None
    elif (host in ("www.binance.com", "binance.com") and not twap and "binance" in rules
          and notice.horizon == 60 and f"/{notice.asset.lower()}_usdt" in source
          and f"{notice.asset.lower()}/usdt" in rules):
        kind = "terminal"
    else:
        return None
    return source + "|" + kind


def checkpoint(notice, now: datetime) -> bool:
    target = notice.issued + timedelta(minutes=notice.horizon * CHECKPOINT_ELAPSED)
    return (target <= now <= target + CHECKPOINT_WIDTH
            and target <= notice.quote_at <= now < notice.expires
            and now - notice.quote_at <= timedelta(seconds=25))


def implied_bet(p: float, up_cost: float | None, down_cost: float | None):
    """One predetermined side/WAIT; no later search for a cheaper entry."""
    choices = [(side, chance-cost, cost) for side, chance, cost in
               (("UP", p, up_cost), ("DOWN", 1-p, down_cost))
               if cost is not None and math.isfinite(cost) and 0 < cost < 1
               and chance-cost >= EDGE]
    return max(choices, key=lambda x: x[1], default=None)


class Contest:
    def __init__(self, store):
        self.store = store
        self._lock = threading.RLock()
        self._initialized = False

    def initialize(self, *, now: datetime | None = None):
        with self._lock:
            if self._initialized:
                return
            with self.store.connect() as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS rival_poly_contest_models (
                        model TEXT PRIMARY KEY, enrolled_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS rival_poly_contest_rounds (
                        slug TEXT PRIMARY KEY, condition_id TEXT NOT NULL,
                        asset TEXT NOT NULL, horizon INTEGER NOT NULL, schema_key TEXT NOT NULL,
                        opens_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                        issued_at TEXT NOT NULL, quote_at TEXT NOT NULL,
                        market_up DOUBLE PRECISION NOT NULL,
                        up_cost DOUBLE PRECISION, down_cost DOUBLE PRECISION,
                        evidence_sha256 TEXT NOT NULL, evidence_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_poly_contest_expiry
                        ON rival_poly_contest_rounds(asset,horizon,expires_at);
                    CREATE TABLE IF NOT EXISTS rival_poly_contest_forecasts (
                        slug TEXT NOT NULL, model TEXT NOT NULL,
                        probability_up DOUBLE PRECISION NOT NULL,
                        weights_sha256 TEXT NOT NULL, PRIMARY KEY(slug,model)
                    );
                """)
                stamp = (now or datetime.now(UTC)).isoformat()
                for model in MODELS:
                    conn.execute("INSERT INTO rival_poly_contest_models VALUES (?,?) ON CONFLICT DO NOTHING",
                                 (model, stamp))
            self._initialized = True

    def capture(self, notice, forecasts: dict, *, evidence: dict | None = None,
                now: datetime | None = None) -> bool:
        stamp = (now or datetime.now(UTC)).astimezone(UTC)
        self.initialize(now=stamp)
        schema = settlement_schema(notice)
        if (schema is None or notice.asset not in ASSETS or notice.horizon not in HORIZONS
                or not checkpoint(notice, stamp)
                or not isinstance(notice.condition_id, str)
                or re.fullmatch(r"0x[0-9a-fA-F]{64}", notice.condition_id) is None):
            return False
        try:
            ub, db = float(notice.up_bid), float(notice.down_bid)
            ua, da = float(notice.up_ask), float(notice.down_ask)
            if not (0 < ub <= ua < 1 and 0 < db <= da < 1):
                return False
            market = (ub + ua) / (ub + ua + db + da)
            clean = {model: (float(p), sha) for model, (p, sha) in forecasts.items()
                     if math.isfinite(float(p)) and 0 <= float(p) <= 1
                     and isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha)
                     and (model in MODELS or model.startswith("pooled:") and model.endswith("@" + sha))}
        except (TypeError, ValueError, AttributeError):
            return False
        rate = getattr(notice, "fee_rate", None)
        def known_cost(value, ask):
            if (rate is None or not math.isfinite(rate) or not 0 <= rate <= 1
                    or value is None or not math.isfinite(value) or not ask <= value < 2):
                return None
            return value
        up_cost = known_cost(notice.up_cost, ua)
        down_cost = known_cost(notice.down_cost, da)
        raw = json.dumps({"slug": notice.slug, "condition_id": notice.condition_id,
                          "quote_at": notice.quote_at.isoformat(), "up_bid": ub, "up_ask": ua,
                          "down_bid": db, "down_ask": da, "schema": schema,
                          "fee_rate": rate, "up_cost": up_cost,
                          "down_cost": down_cost, "inputs": evidence or {}},
                         sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self._lock, self.store.connect() as conn:
            for model in clean:
                conn.execute("INSERT INTO rival_poly_contest_models VALUES (?,?) ON CONFLICT DO NOTHING",
                             (model, stamp.isoformat()))
            inserted = conn.execute(
                "INSERT INTO rival_poly_contest_rounds VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(slug) DO NOTHING",
                (notice.slug, notice.condition_id, notice.asset, notice.horizon, schema,
                 notice.issued.isoformat(), notice.expires.isoformat(), stamp.isoformat(),
                 notice.quote_at.isoformat(), market, up_cost, down_cost,
                 hashlib.sha256(raw.encode()).hexdigest(), raw),
            ).rowcount
            if not inserted:
                return False  # no late model additions, even after a restart
            for model, (p, sha) in clean.items():
                conn.execute("INSERT INTO rival_poly_contest_forecasts VALUES (?,?,?,?)",
                             (notice.slug, model, p, sha))
        return True

    def call(self, slug: str, model: str) -> dict | None:
        self.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT r.*,f.model,f.probability_up FROM rival_poly_contest_rounds r "
                "JOIN rival_poly_contest_forecasts f ON f.slug=r.slug WHERE r.slug=? AND f.model=?",
                (slug, model),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        bet = implied_bet(float(row["probability_up"]), row["up_cost"], row["down_cost"])
        result["direction"] = bet[0] if bet else "WAIT"
        return result


def _beta_fraction(a: float, b: float, x: float) -> float:
    """Continued fraction for regularized incomplete beta (Student t tail)."""
    tiny = 1e-300
    c, d = 1., 1 - (a+b)*x/(a+1)
    d = 1 / (d if abs(d) > tiny else tiny)
    value = d
    for m in range(1, 201):
        for numerator in (m*(b-m)*x/((a+2*m-1)*(a+2*m)),
                          -(a+m)*(a+b+m)*x/((a+2*m)*(a+2*m+1))):
            d = 1 + numerator*d
            c = 1 + numerator/c
            d = 1 / (d if abs(d) > tiny else tiny)
            c = c if abs(c) > tiny else tiny
            delta = d*c
            value *= delta
        if abs(delta-1) < 1e-13:
            return value
    return math.nan


def _t_tail(t: float, df: int) -> float:
    if t <= 0 or df < 1:
        return 1.
    x = df / (df + t*t)
    a, b = df/2, .5
    if x >= 1:
        return .5
    if x == 0:
        return 0.
    front = math.exp(math.lgamma(a+b)-math.lgamma(a)-math.lgamma(b)
                     + a*math.log(x) + b*math.log1p(-x))
    beta = (front*_beta_fraction(a, b, x)/a if x < (a+1)/(a+b+2)
            else 1-front*_beta_fraction(b, a, 1-x)/b)
    return max(0., min(1., .5*beta)) if math.isfinite(beta) else 1.


def _p_value(diffs: list[float]) -> tuple[float, float, float]:
    """Daily paired DM-style mean test, Newey-West/Bartlett 3-day bandwidth.

    Daily clustering avoids treating hundreds of nearby contracts as iid.
    Student-t small-sample reference is approximate under short-memory
    stationarity, not a distribution-free guarantee. Degenerate data abstain.
    """
    n = len(diffs)
    if n < MIN_DAYS:
        return sum(diffs)/n if n else 0., 0., 1.
    mean = sum(diffs)/n
    residual = [x-mean for x in diffs]
    var = sum(x*x for x in residual)/n
    for lag in range(1, 4):
        cov = sum(residual[i]*residual[i-lag] for i in range(lag, n))/n
        var += 2*(1-lag/4)*cov
    se = math.sqrt(max(0., var)/(n-1))
    return mean, se, _t_tail(mean/se, n-1) if se > 1e-12 else 1.


def holm(pvalues: dict, alpha: float = ALPHA) -> set:
    ordered = sorted(pvalues.items(), key=lambda kv: kv[1])
    passed = set()
    for rank, (key, p) in enumerate(ordered):
        if not math.isfinite(p) or not 0 <= p <= alpha/(len(ordered)-rank):
            break
        passed.add(key)
    return passed


def batch(now: datetime):
    number = (now - SETTLEMENT_GRACE - POLICY_START).days // WINDOW_DAYS
    if number < 1:
        return None
    end = POLICY_START + timedelta(days=number*WINDOW_DAYS)
    return number, end-timedelta(days=WINDOW_DAYS), end


def _cohorts(store, start, end):
    """Bound peak RAM by one market; never fetch raw evidence blobs for scoring."""
    for asset in ASSETS:
        for horizon in HORIZONS:
            params = (asset, horizon, start.isoformat(), end.isoformat(), start.isoformat())
            with store.connect() as conn:
                rows = conn.execute(
                    "SELECT r.slug,r.schema_key,r.issued_at,r.expires_at,r.market_up,r.up_cost,r.down_cost,"
                    "o.direction,o.observed_at AS outcome_at,o.resolved_at "
                    "FROM rival_poly_contest_rounds r LEFT JOIN rival_polymarket_ai_outcomes o "
                    "ON o.slug=r.slug AND o.condition_id=r.condition_id "
                    "WHERE r.asset=? AND r.horizon=? AND r.expires_at>=? AND r.expires_at<? "
                    "AND r.issued_at>=? AND r.issued_at<r.expires_at ORDER BY r.issued_at", params,
                ).fetchall()
                forecasts = conn.execute(
                    "SELECT f.slug,f.model,f.probability_up FROM rival_poly_contest_forecasts f "
                    "JOIN rival_poly_contest_rounds r ON r.slug=f.slug "
                    "WHERE r.asset=? AND r.horizon=? AND r.expires_at>=? AND r.expires_at<? "
                    "AND r.issued_at>=?", params,
                ).fetchall()
            predictions = {(r["slug"], r["model"]): float(r["probability_up"]) for r in forecasts}
            grouped = defaultdict(list)
            for row in rows:
                grouped[row["schema_key"]].append(row)
            for schema, cohort in grouped.items():
                yield (asset, horizon, schema), cohort, predictions


def league(store, *, now: datetime | None = None) -> dict:
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    Contest(store).initialize(now=stamp)
    result = {(a, h): {"models": [], "champion": None, "scopes": {}}
              for a in ASSETS for h in HORIZONS}
    period = batch(stamp)
    if period is None:
        return {"markets": result, "tests": 0, "computed_at": stamp,
                "window_days": WINDOW_DAYS, "batch": None}
    number, start, end = period
    cutoff = end + SETTLEMENT_GRACE
    with store.connect() as conn:
        enrolled = [r["model"] for r in conn.execute(
            "SELECT model FROM rival_poly_contest_models WHERE enrolled_at<=? ORDER BY model",
            (start.isoformat(),)).fetchall()]
    # Even absent/insufficient models and markets consume a hypothesis slot.
    tests = {(a, h, "missing", m): 1. for a in ASSETS for h in HORIZONS for m in enrolled}
    stats = {}
    for (asset, horizon, schema), cohort, predictions in _cohorts(store, start, end):
        settled = [r for r in cohort if r["direction"] in ("UP", "DOWN")
                   and r["resolved_at"] >= r["expires_at"] and r["outcome_at"] <= cutoff.isoformat()]
        eligible = [m for m in enrolled if cohort and sum((r["slug"], m) in predictions for r in settled)/len(cohort) >= MIN_COVERAGE]
        # Every eligible contender and the price use the identical contract set.
        common = [r for r in settled if all((r["slug"], m) in predictions for m in eligible)]
        # Count every expected contract window, including process downtime.
        # A few conveniently observed contracts cannot stand in for coverage.
        expected = WINDOW_DAYS*24*60/horizon
        coverage = min(len(common)/expected, len(common)/len(cohort)) if cohort else 0.
        for model in enrolled:
            tests.pop((asset, horizon, "missing", model), None)
            key = asset, horizon, schema, model
            daily, gains, profits, trades = defaultdict(list), [], [], 0
            priced = sum(r["up_cost"] is not None and r["down_cost"] is not None for r in common)
            if model in eligible:
                for row in common:
                    p, m = predictions[row["slug"], model], float(row["market_up"])
                    if not (math.isfinite(p) and math.isfinite(m) and 0 <= p <= 1 and 0 <= m <= 1):
                        continue
                    y = float(row["direction"] == "UP")
                    gain = (m-y)**2 - (p-y)**2
                    bet = implied_bet(p, row["up_cost"], row["down_cost"])
                    profit = 0.
                    if bet is not None:
                        side, _, cost = bet
                        profit = float(row["direction"] == side) - cost - STRESS
                        trades += 1
                    gains.append(gain)
                    profits.append(profit)
                    daily[row["issued_at"][:10]].append((gain, profit))
            days = sorted(daily)
            # Include missing days as zeros so adjacent covariance lags remain daily.
            grid = [(start+timedelta(days=i)).date().isoformat() for i in range(WINDOW_DAYS)]
            dg = [sum(x[0] for x in daily[d])/len(daily[d]) if daily[d] else 0. for d in grid]
            dp = [sum(x[1] for x in daily[d])/len(daily[d]) if daily[d] else 0. for d in grid]
            _, se, p_brier = _p_value(dg)
            net, _, p_net = _p_value(dp)
            valid = (len(gains) >= MIN_CONTRACTS and len(days) >= MIN_DAYS
                     and coverage >= MIN_COVERAGE and trades >= MIN_TRADES
                     and common and priced/len(common) >= MIN_COVERAGE
                     and sum(gains) > 0 and net > 0)
            tests[key] = max(p_brier, p_net) if valid else 1.
            stats[key] = {"model": model, "label": label(model), "contracts": len(gains),
                          "days": len(days), "coverage": coverage, "trades": trades,
                          "improvement": sum(gains)/len(gains) if gains else 0., "se": se,
                          "net_per_contract": sum(profits)/len(profits) if profits else 0.,
                          "p": tests[key], "schema": schema}
    # Sum_{k>=1} 1/(k(k+1)) = 1; restarts/repeated refreshes cannot reset alpha.
    spent_alpha = ALPHA/(number*(number+1))
    passed = holm(tests, spent_alpha)
    for (asset, horizon, schema, model), item in stats.items():
        scope = result[asset, horizon]["scopes"].setdefault(schema, {"models": [], "champion": None})
        item["significant"] = (asset, horizon, schema, model) in passed
        scope["models"].append(item)
        result[asset, horizon]["models"].append(item)
    for entry in result.values():
        for scope in entry["scopes"].values():
            winners = sorted((x for x in scope["models"] if x["significant"]), key=lambda x: -x["improvement"])
            scope["champion"] = winners[0]["model"] if winners else None
    return {"markets": result, "tests": len(tests), "window_days": WINDOW_DAYS,
            "computed_at": stamp, "batch": number, "alpha": spent_alpha,
            "test_end": end, "valid_until": cutoff+timedelta(days=WINDOW_DAYS)}


def league_text(entry: dict | None, *, asset: str, horizon: int) -> str:
    champion = entry.get("champion") if entry else None
    if champion:
        best = next((m for m in entry.get("models", []) if m["model"] == champion), None)
        counts = (f" {best['contracts']:,} matched contracts; {best['trades']:,} paper entries."
                  if best else "")
        return (f"**{label(champion)}** passed the fixed contest against Polymarket's price "
                f"and the after-fee stress test on matched contracts.{counts} Paper evidence; fills are unverified.")
    return (f"**No model beats Polymarket's price here** ({asset} {horizon}m). "
            "The price remains the benchmark. No model pings until a completed "
            "prospective test passes; insufficient evidence does not prove the price is optimal.")
