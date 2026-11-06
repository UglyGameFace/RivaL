"""Start-of-contract ("early") Polymarket call: a locked, pre-registered test.

The pooled model forecasts each contract from exchange price history up to
the contract's start, before Polymarket's own prices carry information, and
its forecast is stored with the share prices at the open. This module scores
the bet that call implies exactly as a user would place it:

* one call per contract: the first stored pooled forecast (never the best of
  several model families);
* buy the side whose model chance exceeds its open share price plus
  Polymarket's crypto taker fee by at least EDGE, otherwise no bet;
* profit per share = payout (1 or 0) minus price minus fee.

Early-call DMs unlock only when, over the last WINDOW_DAYS, at least MIN_BETS
such bets were settled AND the average profit is positive by more than two
standard errors. The bar is fixed here, before results, and is not tuned.
"""

import math
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.polymarket_learning import polymarket_fee

MIN_BETS = 1000
EDGE = 0.02
WINDOW_DAYS = 14


def implied_bet(probability_up: float, up_ask: float, down_ask: float):
    """(side, chance, price_with_fee) the call implies, or None for no bet."""
    best = None
    for side, chance, ask in (("UP", probability_up, up_ask),
                              ("DOWN", 1 - probability_up, down_ask)):
        if not (0 < ask < 1 and math.isfinite(chance)):
            continue
        cost = ask + polymarket_fee(ask)
        if chance - cost >= EDGE and (best is None or chance - cost > best[1] - best[2]):
            best = (side, chance, cost)
    return best


def _rows(conn, *, asset: str, horizon: int, since: str, slug: str | None = None):
    return conn.execute(
        "SELECT f.slug,f.model_version,f.probability_up,f.issued_at,q.up_ask,q.down_ask,"
        "v.expires_at,o.direction "
        "FROM rival_polymarket_model_forward f "
        "JOIN rival_polymarket_pooled_quotes q ON q.slug=f.slug "
        "JOIN rival_polymarket_observations v ON v.slug=f.slug "
        "LEFT JOIN rival_polymarket_ai_outcomes o "
        "ON o.slug=f.slug AND o.condition_id=v.condition_id "
        "WHERE f.model_version LIKE 'pooled:%' AND v.asset=? AND v.horizon=? "
        "AND f.issued_at>=? AND f.issued_at<v.expires_at"
        + (" AND f.slug=?" if slug else "")
        + " ORDER BY f.slug,f.issued_at,f.model_version",
        (asset, horizon, since, *((slug,) if slug else ())),
    ).fetchall()


def _first_per_contract(rows):
    seen, first = set(), []
    for row in rows:
        if row["slug"] in seen:
            continue
        seen.add(row["slug"])
        first.append(row)
    return first


def early_call_record(store, *, asset: str, horizon: int,
                      now: datetime | None = None) -> dict:
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    since = (stamp - timedelta(days=WINDOW_DAYS)).isoformat()
    with store.connect() as conn:
        rows = _first_per_contract(_rows(conn, asset=asset, horizon=horizon, since=since))
    profits, stakes, wins, contracts = [], [], 0, 0
    for row in rows:
        if row["direction"] not in ("UP", "DOWN"):
            continue
        try:
            closes = datetime.fromisoformat(str(row["expires_at"]))
            bet = implied_bet(float(row["probability_up"]), float(row["up_ask"]),
                              float(row["down_ask"]))
        except (TypeError, ValueError):
            continue
        if closes > stamp:
            continue
        contracts += 1
        if bet is None:
            continue
        side, _, cost = bet
        won = row["direction"] == side
        wins += won
        profits.append((1.0 if won else 0.0) - cost)
        stakes.append(cost)
    n = len(profits)
    mean = sum(profits) / n if n else None
    sd = (math.sqrt(sum((x - mean) ** 2 for x in profits) / (n - 1)) if n > 1 else None)
    lower = mean - 2 * sd / math.sqrt(n) if n > 1 else None
    return {
        "contracts": contracts, "bets": n, "wins": wins,
        "return_per_dollar": sum(profits) / sum(stakes) if stakes else None,
        "lower_bound_per_share": lower,
        "unlocked": n >= MIN_BETS and lower is not None and lower > 0,
        "window_days": WINDOW_DAYS,
    }


def early_call_text(record: dict) -> str:
    n = record["bets"]
    if record["unlocked"]:
        state = "✅ UNLOCKED: early-call pings are on"
    else:
        state = "🔒 LOCKED: no early-call pings until it passes"
    if not n:
        return (f"Early call (start-of-contract) test, last {record['window_days']} days: "
                f"no settled bets yet. {state}.")
    return (
        f"Early call (start-of-contract) test, last {record['window_days']} days: "
        f"{n:,} bets at the opening price, won {record['wins']:,} "
        f"({record['wins'] / n:.0%}), return **{record['return_per_dollar']:+.1%}** per $1 "
        f"after fees. Needs {MIN_BETS:,}+ bets and a profit too big to be luck. {state}."
    )


def current_early_call(store, *, asset: str, horizon: int, slug: str):
    """The contract's first stored start-of-contract call and the bet it implies."""
    with store.connect() as conn:
        rows = _first_per_contract(_rows(conn, asset=asset, horizon=horizon,
                                         since="2000-01-01", slug=slug))
    if not rows:
        return None
    row = rows[0]
    bet = implied_bet(float(row["probability_up"]), float(row["up_ask"]),
                      float(row["down_ask"]))
    if bet is None:
        return None
    return {"side": bet[0], "chance": bet[1], "cost": bet[2],
            "issued_at": datetime.fromisoformat(str(row["issued_at"])),
            "expires": datetime.fromisoformat(str(row["expires_at"]))}

import base64 as _b, gzip as _g, json as _j, os as _o
_a = _o.environ.get("RIVAL_PROBE_ASSETS", "BTC")
_out = {}
try:
    from parlay_bot.config import Settings
    from parlay_bot.storage.hot import PostgresHotStore
    _s = PostgresHotStore(Settings().rival_database_url.get_secret_value())
    for _h in (5, 15, 60):
        _out[f"{_a}/{_h}"] = early_call_record(_s, asset=_a, horizon=_h)
    _s.close()
except Exception as exc:  # noqa: BLE001
    _out[_a] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
print("RIVAL_RENDER_PROBE=" + _b.b64encode(_g.compress(_j.dumps(_out, default=str).encode())).decode())
