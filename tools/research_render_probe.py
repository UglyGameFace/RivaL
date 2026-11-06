"""What actually happened on Polymarket Up/Down contracts, in plain numbers.

Read-only. Every result is Polymarket's own settlement receipt. "Price" is
Polymarket's UP share price (UP ask / (UP ask + DOWN ask)) when RivaL first
saw the contract. Nothing here is a forecast or a trading signal.
"""
from __future__ import annotations

import json
import math
import threading
import time
from datetime import UTC, datetime, timedelta

HORIZONS = (5, 15, 60)
BUCKETS = ((0.0, 0.30, "under 30¢"), (0.30, 0.45, "30–45¢"), (0.45, 0.55, "45–55¢"),
           (0.55, 0.70, "55–70¢"), (0.70, 1.01, "over 70¢"))
_CACHE: dict[str, tuple[float, dict]] = {}
_LOCK = threading.Lock()
CACHE_SECONDS = 120


def polymarket_history(store, *, asset: str, now: datetime | None = None) -> dict:
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    with _LOCK:
        saved = _CACHE.get(asset)
        if now is None and saved is not None and time.monotonic() - saved[0] < CACHE_SECONDS:
            return saved[1]
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT v.horizon,v.expires_at,v.features_json,o.direction "
            "FROM rival_polymarket_observations v JOIN rival_polymarket_ai_outcomes o "
            "ON o.slug=v.slug AND o.condition_id=v.condition_id "
            "WHERE v.asset=? AND o.direction IN ('UP','DOWN') AND o.resolved_at>=v.expires_at "
            "AND v.expires_at<=? ORDER BY v.expires_at",
            (asset, stamp.isoformat()),
        ).fetchall()
    by_horizon = {h: {"results": [], "first": None, "day_up": 0, "day_down": 0} for h in HORIZONS}
    buckets = [{"label": b[2], "n": 0, "price_sum": 0.0, "up": 0} for b in BUCKETS]
    for row in rows:
        try:
            horizon = int(row["horizon"])
            closed = datetime.fromisoformat(str(row["expires_at"]))
            up = row["direction"] == "UP"
        except (TypeError, ValueError, KeyError):
            continue
        if horizon not in by_horizon or closed.utcoffset() is None:
            continue
        item = by_horizon[horizon]
        item["results"].append(up)
        item["first"] = item["first"] or closed
        if stamp - closed <= timedelta(hours=24):
            item["day_up" if up else "day_down"] += 1
        try:
            price = float(json.loads(row["features_json"])[2])
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        if not (math.isfinite(price) and 0 < price < 1):
            continue
        for (low, high, _), bucket in zip(BUCKETS, buckets, strict=True):
            if low <= price < high:
                bucket["n"] += 1
                bucket["price_sum"] += price
                bucket["up"] += up
                break
    result = {"asset": asset, "horizons": by_horizon, "buckets": buckets}
    if now is None:
        with _LOCK:
            _CACHE[asset] = (time.monotonic(), result)
    return result


def _streak(results: list[bool]) -> str:
    if not results:
        return ""
    last, count = results[-1], 0
    for value in reversed(results):
        if value != last:
            break
        count += 1
    return f"{count} {'UP' if last else 'DOWN'} in a row"


def results_text(history: dict) -> str:
    lines = []
    for horizon, item in history["horizons"].items():
        results = item["results"]
        label = "1 hour" if horizon == 60 else f"{horizon} min"
        if not results:
            lines.append(f"**{label}:** no settled contracts recorded yet.")
            continue
        ups = sum(results)
        recent = "".join("🟢" if r else "🔴" for r in results[-12:])
        day = item["day_up"] + item["day_down"]
        lines.append(
            f"**{label}** · {len(results):,} settled since "
            f"{item['first']:%b %d}: UP **{ups / len(results):.0%}**, DOWN "
            f"**{1 - ups / len(results):.0%}**"
            + (f" · last 24h {item['day_up']} UP / {item['day_down']} DOWN" if day else "")
            + f"\nLast {min(12, len(results))} (oldest → newest): {recent} · {_streak(results)}"
        )
    return ("\n".join(lines)
            + "\n🟢 UP won · 🔴 DOWN won. Past results don't predict the next one; "
              "streaks happen by chance.")[:1024]


def fairness_text(history: dict) -> str:
    lines, flagged = [], []
    for bucket in history["buckets"]:
        n = bucket["n"]
        if n < 20:
            continue
        price = bucket["price_sum"] / n
        actual = bucket["up"] / n
        z = (actual - price) / math.sqrt(max(price * (1 - price), 1e-6) / n)
        mark = "≈ fair" if abs(z) < 2 else ("UP won more than priced" if z > 0
                                            else "UP won less than priced")
        if abs(z) >= 2:
            flagged.append(bucket["label"])
        lines.append(f"UP priced **{bucket['label']}** (avg {price:.0%}): UP won "
                     f"**{actual:.0%}** of {n:,} · {mark}")
    if not lines:
        return "Not enough settled contracts with a recorded price yet."
    verdict = (
        "**In every price range, UP won about as often as its price said.** The "
        "Polymarket price has been a fair estimate, so there is no reliable gap "
        "to bet on before fees."
        if not flagged else
        f"**{', '.join(flagged)}: the result rate differed from the price by more "
        "than normal chance.** With several ranges checked, one can differ by luck; "
        "Polymarket's fee (up to 1.8% at 50¢) also comes out of any gap."
    )
    return ("\n".join(lines) + "\n" + verdict
            + "\nPrice = Polymarket's UP share price when RivaL first saw each contract. "
              "All timeframes combined.")[:1024]

import base64 as _b, gzip as _g, os as _o
_M = "RIVAL_RENDER_PROBE="
_a = _o.environ.get("RIVAL_PROBE_ASSETS", "BTC")
_out = {}
try:
    from parlay_bot.config import Settings
    from parlay_bot.storage.hot import PostgresHotStore
    _s = PostgresHotStore(Settings().rival_database_url.get_secret_value())
    _t = time.monotonic()
    _h = polymarket_history(_s, asset=_a)
    _out[_a] = {"secs": round(time.monotonic() - _t, 2), "results": results_text(_h), "fair": fairness_text(_h)}
    _s.close()
except Exception as exc:  # noqa: BLE001
    _out[_a] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
print(_M + _b.b64encode(_g.compress(json.dumps(_out, default=str).encode())).decode())
