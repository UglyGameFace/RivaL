"""Prospective, source-specific model contest on one immutable contract snapshot.

Brier vs contemporaneous normalized CLOB midpoints; economics at recorded
10-share ask depth + exact published fee. Prospective betting e-processes
permit continuous evaluation without a calendar blackout. A fixed lifetime
error budget covers all models, twelve markets and settlement schemas.
No legacy rows become new-policy evidence. See POLYMARKET_MODEL_CONTEST.md.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from datetime import UTC, datetime, timedelta
from itertools import groupby
from urllib.parse import urlsplit

ALPHA = .05
MIN_CONTRACTS = 300
MIN_TRADES = 150
MIN_COVERAGE = .8
POLICY = "sequential-betting-v2"
BET_FRACTIONS = (.01, .03, .1, .3, .5, .8)
CHECKPOINT_ELAPSED = .20
CHECKPOINT_WIDTH = timedelta(seconds=20)
EDGE = .03
STRESS = .01  # additional adverse cents/share, not a claim of actual fills
BOOK = "polymarket-terminal-book-logistic-v1"
FORMULA = "digital-gbm-ewma-v2"
JUMP = "digital-merton-moments-v1"
PRESSURE = "digital-queue-microprice-v1"
MODELS = (BOOK, FORMULA, JUMP, PRESSURE)
# Contestants added after the four declared ones. They are NOT in MODELS:
# each takes a slot from the half of the error budget reserved for later
# identities (_test_alpha), so the declared models' tests are unchanged.
LAG = "digital-odds-lag-logistic-v1"
LATER_MODELS = (LAG,)
ASSETS = ("BTC", "ETH", "SOL", "XRP")
HORIZONS = (5, 15, 60)


def label(model: str) -> str:
    return {BOOK: "Book logistic", FORMULA: "GBM + EWMA", JUMP: "Jump diffusion",
            PRESSURE: "Queue pressure", LAG: "Odds-lag learner"}.get(model, "Pooled " + model[-8:] if model.startswith("pooled:") else model[:24])


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
                auto_id = self.store.database.auto_increment_primary_key
                conn.executescript(f"""
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
                    CREATE TABLE IF NOT EXISTS rival_poly_contest_policies (
                        policy TEXT PRIMARY KEY, started_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS rival_poly_contest_model_slots (
                        id {auto_id}, model TEXT NOT NULL UNIQUE
                    );
                    CREATE TABLE IF NOT EXISTS rival_poly_contest_markets (
                        asset TEXT NOT NULL, horizon INTEGER NOT NULL,
                        next_scope INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(asset,horizon)
                    );
                    CREATE TABLE IF NOT EXISTS rival_poly_contest_scopes (
                        asset TEXT NOT NULL, horizon INTEGER NOT NULL, schema_key TEXT NOT NULL,
                        test_index INTEGER NOT NULL, started_at TEXT NOT NULL,
                        PRIMARY KEY(asset,horizon,schema_key), UNIQUE(asset,horizon,test_index)
                    );
                    CREATE TABLE IF NOT EXISTS rival_poly_contest_display (
                        slug TEXT PRIMARY KEY, model TEXT NOT NULL, qualified INTEGER NOT NULL
                    );
                """)
                stamp = (now or datetime.now(UTC)).isoformat()
                conn.execute("INSERT INTO rival_poly_contest_policies VALUES (?,?) ON CONFLICT DO NOTHING",
                             (POLICY, stamp))
                for model in MODELS:
                    conn.execute("INSERT INTO rival_poly_contest_models VALUES (?,?) ON CONFLICT DO NOTHING",
                                 (model, stamp))
                for asset in ASSETS:
                    for horizon in HORIZONS:
                        conn.execute("INSERT INTO rival_poly_contest_markets(asset,horizon) VALUES (?,?) "
                                     "ON CONFLICT DO NOTHING", (asset, horizon))
                # Existing identities get slots before new-policy outcomes;
                # enrollment order, never performance, determines the budget.
                for row in conn.execute("SELECT model FROM rival_poly_contest_models "
                                        "ORDER BY enrolled_at,model").fetchall():
                    self._model_slot(conn, row["model"])
            self._initialized = True

    @staticmethod
    def _model_slot(conn, model):
        if model not in MODELS and conn.execute(
                "SELECT id FROM rival_poly_contest_model_slots WHERE model=?", (model,)).fetchone() is None:
            conn.execute("INSERT INTO rival_poly_contest_model_slots(model) VALUES (?) "
                         "ON CONFLICT DO NOTHING", (model,))

    @staticmethod
    def _scope_slot(conn, notice, schema, stamp):
        params = notice.asset, notice.horizon, schema
        if conn.execute("SELECT test_index FROM rival_poly_contest_scopes "
                        "WHERE asset=? AND horizon=? AND schema_key=?", params).fetchone() is None:
            # Atomic on both databases. Racing inserts can waste an index,
            # which only makes later thresholds more conservative.
            row = conn.execute("UPDATE rival_poly_contest_markets SET next_scope=next_scope+1 "
                               "WHERE asset=? AND horizon=? RETURNING next_scope", params[:2]).fetchone()
            conn.execute("INSERT INTO rival_poly_contest_scopes VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                         (*params, row["next_scope"], stamp.isoformat()))

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
                     and (model in MODELS or model in LATER_MODELS
                          or model.startswith("pooled:") and model.endswith("@" + sha))}
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
            self._scope_slot(conn, notice, schema, stamp)
            for model, (p, sha) in clean.items():
                self._model_slot(conn, model)
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

    def display_call(self, slug: str, preferred: str | None) -> dict | None:
        """Pin the first displayed model so a mid-contract promotion cannot flip it."""
        self.initialize()
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO rival_poly_contest_display(slug,model,qualified) "
                "SELECT slug,model,CASE WHEN model=? THEN 1 ELSE 0 END "
                "FROM rival_poly_contest_forecasts WHERE slug=? "
                "ORDER BY CASE model WHEN ? THEN 0 WHEN ? THEN 1 WHEN ? THEN 2 ELSE 3 END,model "
                "LIMIT 1 ON CONFLICT(slug) DO NOTHING", (preferred, slug, preferred, FORMULA, BOOK))
            row = conn.execute("SELECT model,qualified FROM rival_poly_contest_display WHERE slug=?", (slug,)).fetchone()
        result = self.call(slug, row["model"]) if row else None
        if result is not None:
            result["qualified_at_issue"] = bool(row["qualified"])
        return result


def _log_capital(logs: list[float]) -> float:
    peak = max(logs)
    return peak + math.log(sum(math.exp(x-peak) for x in logs)/len(logs))


def _update(logs: list[float], value: float, downside: float) -> None:
    """Fixed mixture of nonnegative betting supermartingales.

    Under E[value | information before this outcome] <= 0, each factor
    1 + fraction*value/downside has conditional expectation <= 1. The
    downside is known at issuance. Mixing capital, not picking the best
    fraction after outcomes, preserves validity at arbitrary stopping times.
    """
    if downside <= 0:
        return
    for i, fraction in enumerate(BET_FRACTIONS):
        logs[i] += math.log1p(fraction*value/downside)


def _test_alpha(model: str, model_slots: dict, scope_index: int) -> float:
    # Half the lifetime model budget is shared equally by the four declared
    # algorithms; half is summably reserved for future artifact identities.
    if model in MODELS:
        weight = .5/len(MODELS)
    else:
        index = model_slots.get(model)
        if not index:
            return 0.
        weight = .5/(index*(index+1))
    return ALPHA*weight/(len(ASSETS)*len(HORIZONS)*scope_index*(scope_index+1))


def _rounds(store, asset, horizon, start, end):
    """Keyset pages bound RAM, including when months of evidence accumulate."""
    cursor = start.isoformat(), ""
    while True:
        with store.connect() as conn:
            rows = conn.execute(
                "SELECT r.slug,r.schema_key,r.opens_at,r.issued_at,r.expires_at,r.market_up,"
                "r.up_cost,r.down_cost,o.direction,o.observed_at AS outcome_at,o.resolved_at,"
                "f.model,f.probability_up FROM (SELECT * FROM rival_poly_contest_rounds "
                "WHERE asset=? AND horizon=? AND issued_at>=? AND issued_at<expires_at "
                "AND expires_at<=? AND (expires_at>? OR (expires_at=? AND slug>?)) "
                "ORDER BY expires_at,slug LIMIT 500) r "
                "LEFT JOIN rival_polymarket_ai_outcomes o ON o.slug=r.slug AND o.condition_id=r.condition_id "
                "LEFT JOIN rival_poly_contest_forecasts f ON f.slug=r.slug "
                "ORDER BY r.expires_at,r.slug,f.model",
                (asset, horizon, start.isoformat(), end.isoformat(), cursor[0], cursor[0], cursor[1]),
            ).fetchall()
        if not rows:
            return
        for _, group in groupby(rows, key=lambda r: r["slug"]):
            group = list(group)
            row = group[0]
            yield row, {r["model"]: float(r["probability_up"]) for r in group if r["model"] is not None}
            cursor = row["expires_at"], row["slug"]


def league(store, *, now: datetime | None = None) -> dict:
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    Contest(store).initialize(now=stamp)
    result = {(a, h): {"models": [], "champion": None, "scopes": {}}
              for a in ASSETS for h in HORIZONS}
    with store.connect() as conn:
        start = datetime.fromisoformat(conn.execute(
            "SELECT started_at FROM rival_poly_contest_policies WHERE policy=?", (POLICY,)
        ).fetchone()["started_at"])
        enrolled = {r["model"]: datetime.fromisoformat(r["enrolled_at"]) for r in conn.execute(
            "SELECT model,enrolled_at FROM rival_poly_contest_models WHERE enrolled_at<=?",
            (stamp.isoformat(),)).fetchall()}
        slots = {r["model"]: int(r["id"]) for r in conn.execute(
            "SELECT id,model FROM rival_poly_contest_model_slots").fetchall()}
        scopes = {(r["asset"], int(r["horizon"]), r["schema_key"]): dict(r)
                  for r in conn.execute("SELECT * FROM rival_poly_contest_scopes").fetchall()}
    stats = {}
    for asset in ASSETS:
        for horizon in HORIZONS:
            for row, predictions in _rounds(store, asset, horizon, start, stamp):
                schema = row["schema_key"]
                for model, p in predictions.items():
                    if model not in enrolled or datetime.fromisoformat(row["issued_at"]) < enrolled[model]:
                        continue
                    key = asset, horizon, schema, model
                    state = stats.setdefault(key, {
                        "contracts": 0, "trades": 0, "priced": 0, "gain": 0., "profit": 0.,
                        "brier_logs": [0.]*len(BET_FRACTIONS), "net_logs": [0.]*len(BET_FRACTIONS),
                        "last_expiry": None, "blocked": None,
                    })
                    if state["blocked"]:
                        continue
                    # Consume only a chronological resolved prefix. Selecting
                    # the quickly resolved winners would invalidate the test.
                    if (row["direction"] not in ("UP", "DOWN") or row["outcome_at"] is None
                            or row["resolved_at"] is None or row["resolved_at"] < row["expires_at"]
                            or row["outcome_at"] < row["resolved_at"] or row["outcome_at"] > stamp.isoformat()):
                        state["blocked"] = "Waiting for an earlier contract settlement"
                        continue
                    if state["last_expiry"] and row["opens_at"] < state["last_expiry"]:
                        state["blocked"] = "Overlapping contracts need review"
                        continue
                    m = float(row["market_up"])
                    if not (math.isfinite(p) and math.isfinite(m) and 0 <= p <= 1 and 0 <= m <= 1):
                        state["blocked"] = "Invalid forecast receipt"
                        continue
                    y = float(row["direction"] == "UP")
                    gain = (m-y)**2 - (p-y)**2
                    # Exact Brier downside is known from the two probabilities.
                    # This avoids the unnecessary [-1,1] worst-case range.
                    downside = -min(m*m-p*p, (1-m)**2-(1-p)**2)
                    _update(state["brier_logs"], gain, downside)
                    bet = implied_bet(p, row["up_cost"], row["down_cost"])
                    profit = 0.
                    if bet is not None:
                        side, _, cost = bet
                        profit = float(row["direction"] == side)-cost-STRESS
                        _update(state["net_logs"], profit, cost+STRESS)
                        state["trades"] += 1
                    state["contracts"] += 1
                    state["priced"] += row["up_cost"] is not None and row["down_cost"] is not None
                    state["gain"] += gain
                    state["profit"] += profit
                    state["last_expiry"] = row["expires_at"]
    for (asset, horizon, schema, model), state in stats.items():
        n = state["contracts"]
        scope_meta = scopes.get((asset, horizon, schema))
        since = max(start, enrolled[model], datetime.fromisoformat(scope_meta["started_at"])) if scope_meta else start
        expected = max(1, int((stamp-since).total_seconds()/(horizon*60))-1)
        coverage = min(1., n/expected)
        cost_coverage = state["priced"]/n if n else 0.
        index = int(scope_meta["test_index"]) if scope_meta else None
        alpha = _test_alpha(model, slots, index) if index else 0.
        log_e = min(_log_capital(state["brier_logs"]), _log_capital(state["net_logs"]))
        fresh = (state["last_expiry"] is not None and stamp-datetime.fromisoformat(
            state["last_expiry"]) <= timedelta(minutes=max(120, 2*horizon)))
        valid = (not state["blocked"] and fresh and n >= MIN_CONTRACTS
                 and state["trades"] >= MIN_TRADES and coverage >= MIN_COVERAGE
                 and cost_coverage >= MIN_COVERAGE and state["gain"] > 0 and state["profit"] > 0)
        significant = bool(valid and alpha > 0 and log_e >= -math.log(alpha))
        item = {"model": model, "label": label(model), "contracts": n,
                "trades": state["trades"], "coverage": coverage, "cost_coverage": cost_coverage,
                "improvement": state["gain"]/n if n else 0.,
                "net_per_contract": state["profit"]/n if n else 0.,
                "log_e": log_e, "alpha": alpha, "significant": significant,
                "blocked": state["blocked"], "schema": schema}
        scope = result[asset, horizon]["scopes"].setdefault(schema, {"models": [], "champion": None})
        scope["models"].append(item)
        result[asset, horizon]["models"].append(item)
    for entry in result.values():
        for scope in entry["scopes"].values():
            scope["models"].sort(key=lambda x: x["model"])
            winners = sorted((x for x in scope["models"] if x["significant"]),
                             key=lambda x: (-x["improvement"], x["model"]))
            scope["champion"] = winners[0]["model"] if winners else None
    return {"markets": result, "tests": max(12*len(enrolled), len(stats)), "alpha": ALPHA,
            "computed_at": stamp, "started_at": start, "policy": POLICY,
            "valid_until": stamp+timedelta(minutes=20)}


def league_text(entry: dict | None, *, asset: str, horizon: int) -> str:
    """Plain words (Nov 11): who, if anyone, beat Polymarket's own price."""
    champion = entry.get("champion") if entry else None
    models = entry.get("models", []) if entry else []
    if champion:
        best = next((m for m in models if m["model"] == champion), None)
        counts = (f" on {best['contracts']:,} contracts ({best['trades']:,} practice entries)"
                  if best else "")
        return (f"**{label(champion)}** beat Polymarket's own price{counts} and stayed ahead "
                "after fees. Practice results; real fills aren't verified.")
    progress = max(models, key=lambda m: m["contracts"], default=None)
    if progress:
        detail = (f" Closest: {progress['contracts']:,}/{MIN_CONTRACTS} contracts graded, "
                  f"{progress['trades']:,}/{MIN_TRADES} practice entries priced, "
                  f"{progress['coverage']:.0%} with prices (needs {MIN_COVERAGE:.0%}). "
                  + (progress.get("blocked") or "It hasn't passed the test yet."))
    else:
        detail = " No graded contracts yet."
    return (f"**No model beats Polymarket's price here yet** ({asset} {horizon}m).{detail} "
            "Test calls stay visible; alerts start only after a model passes.")