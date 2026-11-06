"""Per-market model contest with Polymarket's own price as the benchmark.

For each coin x timeframe, every model that issued frozen calls is compared
with the market's implied probability recorded at the SAME moment as each
call, on the same settled contracts, with the Brier score (a proper scoring
rule). The per-contract improvement is

    d_i = (market_i - y_i)^2 - (model_i - y_i)^2      (positive = model better)

A model may lead a market only if a one-sided paired z-test on mean(d) passes
the Holm-Bonferroni step-down procedure at ALPHA across EVERY model x market
test run in the same evaluation (many models and markets make a lucky
"winner" likely otherwise), with at least MIN_CONTRACTS settled contracts.
If nothing passes, the market price is the best available forecast and the
user is told so. The rule is fixed here and not tuned to results.
"""

import math
from datetime import UTC, datetime, timedelta

ALPHA = 0.05
MIN_CONTRACTS = 300
WINDOW_DAYS = 7
BOOK = "polymarket-terminal-book-logistic-v1"
FORMULA = "price-formula-checkpoint-v1"


def label(model: str) -> str:
    if model == BOOK:
        return "Main model"
    if model == FORMULA:
        return "Price formula"
    if model.startswith("pooled:"):
        return "Pooled model " + model.split("_")[-1][:6]
    return model[:24]


def _p_value(diffs: list[float]) -> tuple[float, float, float]:
    """(mean, standard error, one-sided p) for mean(d) > 0."""
    n = len(diffs)
    mean = sum(diffs) / n
    var = sum((x - mean) ** 2 for x in diffs) / (n - 1) if n > 1 else 0.0
    se = math.sqrt(var / n) if var > 0 else 0.0
    if se == 0:
        return mean, se, 0.0 if mean > 0 else 1.0
    z = mean / se
    return mean, se, 0.5 * math.erfc(z / math.sqrt(2))


def holm(pvalues: dict[str, float], alpha: float = ALPHA) -> set[str]:
    """Keys whose null hypothesis is rejected by Holm's step-down procedure."""
    ordered = sorted(pvalues.items(), key=lambda kv: kv[1])
    m, passed = len(ordered), set()
    for rank, (key, p) in enumerate(ordered):
        if p > alpha / (m - rank):
            break
        passed.add(key)
    return passed


def _rows(conn, since: str, now: str):
    forward = conn.execute(
        "SELECT v.asset,v.horizon,f.model_version AS model,f.probability_up AS p,"
        "f.market_baseline AS m,o.direction AS y FROM rival_polymarket_model_forward f "
        "JOIN rival_polymarket_observations v ON v.slug=f.slug "
        "JOIN rival_polymarket_ai_outcomes o ON o.slug=f.slug AND o.condition_id=v.condition_id "
        "WHERE f.issued_at>=? AND v.expires_at<=? AND o.direction IN ('UP','DOWN') "
        "AND o.resolved_at>=v.expires_at AND f.issued_at<v.expires_at",
        (since, now),
    ).fetchall()
    try:
        formula = conn.execute(
            "SELECT c.asset,c.horizon,? AS model,c.chance_up AS p,c.market_up AS m,"
            "o.direction AS y FROM rival_polymarket_formula_checkpoint c "
            "JOIN rival_polymarket_ai_outcomes o ON o.slug=c.slug AND o.condition_id=c.condition_id "
            "WHERE c.issued_at>=? AND c.expires_at<=? AND o.direction IN ('UP','DOWN') "
            "AND o.resolved_at>=c.expires_at",
            (FORMULA, since, now),
        ).fetchall()
    except Exception:  # noqa: BLE001 - checkpoint table appears with the formula test
        formula = []
    return list(forward) + list(formula)


def league(store, *, now: datetime | None = None) -> dict:
    """{(asset, horizon): {"models": [...], "champion": model | None}}."""
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    with store.connect() as conn:
        rows = _rows(conn, (stamp - timedelta(days=WINDOW_DAYS)).isoformat(), stamp.isoformat())
    diffs: dict[tuple[str, int, str], list[float]] = {}
    for row in rows:
        try:
            p, m = float(row["p"]), float(row["m"])
            y = 1.0 if row["y"] == "UP" else 0.0
        except (TypeError, ValueError):
            continue
        if not (0 <= p <= 1 and 0 <= m <= 1):
            continue
        key = (str(row["asset"]), int(row["horizon"]), str(row["model"]))
        diffs.setdefault(key, []).append((m - y) ** 2 - (p - y) ** 2)
    stats, tests = {}, {}
    for key, values in diffs.items():
        mean, se, p = _p_value(values)
        stats[key] = {"model": key[2], "label": label(key[2]), "contracts": len(values),
                      "improvement": mean, "se": se, "p": p}
        if len(values) >= MIN_CONTRACTS:
            tests["|".join(map(str, key))] = p
    passed = holm(tests)
    result: dict[tuple[str, int], dict] = {}
    for (asset, horizon, model), item in stats.items():
        entry = result.setdefault((asset, horizon), {"models": [], "champion": None})
        item["significant"] = f"{asset}|{horizon}|{model}" in passed and item["improvement"] > 0
        entry["models"].append(item)
    for entry in result.values():
        entry["models"].sort(key=lambda x: -x["improvement"])
        winners = [x for x in entry["models"] if x["significant"]]
        entry["champion"] = winners[0]["model"] if winners else None
    return {"markets": result, "tests": len(tests), "window_days": WINDOW_DAYS,
            "computed_at": stamp}


def league_text(entry: dict | None, *, asset: str, horizon: int) -> str:
    tf = "1h" if horizon == 60 else f"{horizon}m"
    if not entry or not entry["models"]:
        return f"No settled model calls for {asset} {tf} yet. Polymarket's price is the benchmark."
    lines = []
    if entry["champion"]:
        best = next(x for x in entry["models"] if x["model"] == entry["champion"])
        lines.append(f"🏆 **{best['label']}** beats Polymarket's price here "
                     f"({best['contracts']:,} contracts, passed the many-models test).")
    else:
        lines.append(f"**No model beats Polymarket's price for {asset} {tf}** "
                     f"(last {WINDOW_DAYS} days). The price itself is the best forecast.")
    for item in entry["models"][:3]:
        sign = "better" if item["improvement"] > 0 else "worse"
        lines.append(f"• {item['label']}: {sign} than the price by "
                     f"{abs(item['improvement']):.4f} ({item['contracts']:,} contracts)")
    return "\n".join(lines)[:1024]

import base64 as _b, gzip as _g, json as _j, os as _o, time as _t
if _o.environ.get("RIVAL_PROBE_ASSETS", "BTC") != "BTC":
    print("RIVAL_RENDER_PROBE=" + _b.b64encode(_g.compress(b"{}")).decode()); raise SystemExit(0)
try:
    from parlay_bot.config import Settings
    from parlay_bot.storage.hot import PostgresHotStore
    _s = PostgresHotStore(Settings().rival_database_url.get_secret_value())
    _t0 = _t.monotonic(); _r = league(_s); _el = round(_t.monotonic() - _t0, 2); _s.close()
    _out = {"secs": _el, "tests": _r["tests"], "markets": {f"{a}|{h}": {"champion": e["champion"], "models": [[m["label"], m["contracts"], round(m["improvement"], 4), round(m["p"], 4), m["significant"]] for m in e["models"]]} for (a, h), e in _r["markets"].items()}}
except Exception as exc:  # noqa: BLE001
    _out = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
print("RIVAL_RENDER_PROBE=" + _b.b64encode(_g.compress(_j.dumps(_out, default=str).encode())).decode())
