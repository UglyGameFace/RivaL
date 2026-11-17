"""Read-only XMarket lifecycle; independent of the hourly promotion ranking."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from parlay_bot.research.xmarket_training import usable
from parlay_bot.storage.optional_reads import optional_rows


def read(store, *, asset: str, now: datetime | None = None) -> dict:
    if asset not in ("BTC", "ETH", "SOL", "XRP"):
        raise ValueError("unsupported XMarket asset")
    clock = now or datetime.now(UTC)
    if clock.utcoffset() is None:
        raise ValueError("XMarket status requires an aware clock")
    clock = clock.astimezone(UTC)
    # The old renderer issued three separate imported-fit SELECTs for every
    # asset (12 for four coins) before it could display a single lifecycle.
    # Keep the same 20-newest-per-horizon gate with one source-authentic,
    # bounded query; never load all historical model documents into Python.
    fits = optional_rows(store,
        "SELECT horizon_minutes,document FROM ("
        "SELECT horizon_minutes,document,ROW_NUMBER() OVER ("
        "PARTITION BY horizon_minutes ORDER BY trained_through DESC"
        ") AS recency_rank FROM rival_xmarket_models "
        "WHERE asset=? AND horizon_minutes IN (5,15,60) "
        "AND gates_passed=1 AND imported_at<=?"
        ") AS ranked WHERE recency_rank<=20",
        (asset, clock.isoformat()), what="XMarket imported-fit status")
    if fits is None:
        return {"status": "unavailable", "partial": True, "asset": asset,
                "checked_at": clock.isoformat()}
    rows = optional_rows(store,
        "SELECT horizon_minutes,COUNT(*) AS issued,MAX(issued_at) AS latest_at,"
        "SUM(CASE WHEN settled_at IS NOT NULL AND settled_at<=? "
        "AND settled_at>issued_at AND outcome_up IN (0,1) THEN 1 ELSE 0 END) AS settled "
        "FROM rival_model_shadow_forecasts WHERE asset=? AND family='xmarket_v1' "
        "AND horizon_minutes IN (5,15,60) AND issued_at>=? AND issued_at<=? "
        "GROUP BY horizon_minutes",
        (clock.isoformat(), asset, (clock-timedelta(days=1)).isoformat(), clock.isoformat()),
        what="XMarket prospective activity")
    if rows is None:
        return {"status": "unavailable", "partial": True, "asset": asset,
                "checked_at": clock.isoformat()}
    imported = {h: [0, 0] for h in (5, 15, 60)}
    for fit in fits:
        h = int(fit["horizon_minutes"])
        if h not in imported:
            continue
        imported[h][0] += 1
        try:
            doc = json.loads(fit["document"])
            valid = (isinstance(doc, dict) and doc.get("asset") == asset
                     and doc.get("horizon_minutes") == h
                     and usable(doc, now=clock))
        except (ValueError, KeyError, TypeError):
            valid = False
        imported[h][1] += int(valid)
    activity = {int(r["horizon_minutes"]): r for r in rows}
    scopes = []
    for h, (stored, valid) in imported.items():
        row = activity.get(h)
        issued, settled = (int(row["issued"]), int(row["settled"] or 0)) if row else (0, 0)
        latest = str(row["latest_at"]) if row else None
        recent = (latest is not None and timedelta(0) <=
                  clock-datetime.fromisoformat(latest) <= timedelta(minutes=20))
        status = ("no_qualifying_fit" if not stored else "fit_unavailable" if not valid else
                  "imported_no_shadows" if not issued else "shadowing" if recent else
                  "shadow_stale")
        scopes.append({"horizon_minutes": h, "status": status, "imported": stored,
                       "usable_imports": valid, "issued_24h": issued, "settled_24h": settled,
                       "pending_24h": issued-settled, "latest_at": latest})
    return {"status": "recorded", "asset": asset, "checked_at": clock.isoformat(),
            "scopes": scopes, "window_hours": 24, "proves_edge": False}
