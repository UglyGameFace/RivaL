"""Bounded, immutable, read-only RivaL forward-forecast dashboard evidence.

Reads previously issued incumbent probabilities only. Does not generate a forecast,
initialize schema, mutate a table, expose raw contract IDs or reveal model weights.
The output is research evidence, NOT an actionable trading signal.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

ASSETS = ("BTC", "ETH", "SOL", "XRP")
HORIZONS = (5, 15, 60)
MODEL_VERSION = "prospective-logistic-persistent-v3"
WINDOW_HOURS = 6
PER_SCOPE = 3
MAX_ITEMS = len(ASSETS) * len(HORIZONS) * PER_SCOPE


def _utc(value: object) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value))
        return stamp.astimezone(UTC) if stamp.utcoffset() is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _prob(value: object) -> float | None:
    try:
        n = float(value)
        if 0 <= n <= 1:
            return round(n, 6)
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def _row(item: dict, *, asset: str, horizon: int, now: datetime) -> dict | None:
    if item.get("asset") != asset or item.get("horizon_minutes") != horizon:
        return None
    issued = _utc(item.get("issued_at"))
    opened = _utc(item.get("target_opened_at"))
    if issued is None or opened is None or issued > now:
        return None
    close = opened + timedelta(minutes=5)
    if issued >= close or issued < now - timedelta(hours=WINDOW_HOURS):
        return None
    prob = _prob(item.get("probability_up"))
    baseline = _prob(item.get("baseline_up"))
    if prob is None or baseline is None:
        return None
    venue = str(item.get("venue", ""))
    quote = str(item.get("quote", ""))
    if not re.fullmatch(r"[a-z0-9_-]{2,28}", venue) or not re.fullmatch(r"[A-Z]{2,8}", quote):
        return None
    settled = _utc(item.get("settled_at")) if item.get("settled_at") is not None else None
    outcome = item.get("outcome_up")
    if settled is not None:
        if settled < close or settled > now:
            return None
        # A market with no price trade is explicitly settled but ungraded.
        # It is not a losing bet, model error, or unobserved forecast.
        if outcome is None:
            status = "ungraded"
        elif str(outcome) in ("0", "1"):
            status = "settled"
        else:
            return None
    else:
        if outcome is not None:
            return None
        status = "awaiting_settlement" if now >= close else "open"
    return {
        "asset": asset, "horizon": horizon, "venue": venue, "quote": quote,
        "issued_at": issued.isoformat(),
        "target_opened_at": opened.isoformat(),
        "target_close_at": close.isoformat(),
        "settled_at": settled.isoformat() if settled is not None else None,
        "probability_up": prob, "baseline_up": baseline,
        "direction": "UP" if prob > 0.5 else "DOWN" if prob < 0.5 else "NEUTRAL",
        "lead_seconds": round((close - issued).total_seconds(), 2),
        "status": status, "outcome_up": int(outcome) if status == "settled" else None,
        "model_version": MODEL_VERSION,
    }


def read_forward_calls(store, now: datetime) -> dict:
    """Get at most the newest PER_SCOPE issued incumbent forecasts per scope.

    Twelve scopes times three recent rows is the bounded, paginable history
    the signed receipt can carry: enough to show issuance, settlement and
    next-target readiness per scope without pretending to be the full
    ledger. The receipt as a whole stays under the collector's 80KB cap
    only with the Delivery 4 section budget (20 RivaL PRs, 12 runs,
    80-char gate failures); a longer history
    needs a separate history endpoint, not a bigger receipt. A missing
    table, invalid source, or database failure remains unavailable,
    never an observed zero. Connection is read-only at the application layer:
    SELECT only, no DDL or learner construction.
    """
    now = now.astimezone(UTC)
    if now.utcoffset() != timedelta(0):
        raise ValueError("not_utc")
    since = (now - timedelta(hours=WINDOW_HOURS)).isoformat()
    rows: list[dict] = []
    try:
        with store.connect() as conn:
            for asset in ASSETS:
                for horizon in HORIZONS:
                    selected = conn.execute(
                        """SELECT asset,horizon_minutes,venue,quote,issued_at,
                                  target_opened_at,probability_up,baseline_up,
                                  settled_at,outcome_up
                           FROM perp_forward_audit
                           WHERE asset=? AND horizon_minutes=? AND model_version=?
                             AND issued_at>=? AND issued_at<=?
                           ORDER BY issued_at DESC LIMIT ?""",
                        (asset, horizon, MODEL_VERSION, since, now.isoformat(),
                         PER_SCOPE),
                    ).fetchall()
                    for item in selected:
                        clean = _row(dict(item), asset=asset, horizon=horizon, now=now)
                        if clean is not None:
                            rows.append(clean)
    except Exception:  # noqa: BLE001 - optional diagnostics cannot break core health
        return {"status": "read_unavailable", "captured_at": now.isoformat(),
                "window_hours": WINDOW_HOURS, "rows": [], "not_trading_calls": True}
    rows.sort(key=lambda r: (r["issued_at"], r["asset"], r["horizon"]), reverse=True)
    return {
        "status": "observed" if rows else "no_recent_forecasts",
        "captured_at": now.isoformat(), "window_hours": WINDOW_HOURS,
        "rows_per_scope": PER_SCOPE,
        "rows": rows[:MAX_ITEMS],
        "not_trading_calls": True,
    }
