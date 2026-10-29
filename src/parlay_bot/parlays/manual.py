"""Read-only selection catalog for user-built parlays.

Never triggers sportsbook API calls. Quotes are bounded by freshness and
jurisdiction, and the UI must label the resulting probabilities as implied,
not model-calibrated predictions.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import isfinite

from parlay_bot.parlays.math import combined_decimal_odds, implied_probability
from parlay_bot.storage.hot import RelationalHotStore


def available_selections(
    store: RelationalHotStore,
    *,
    eligible_books: set[str],
    now: datetime | None = None,
    max_age: timedelta = timedelta(minutes=30),
    limit: int = 100,
) -> list[dict]:
    if not 1 <= limit <= 100 or max_age <= timedelta(0):
        raise ValueError("invalid discovery bounds")
    if not eligible_books:
        return []
    stamp = now or datetime.now(UTC)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("clock must be timezone-aware")
    stamp = stamp.astimezone(UTC)
    placeholders = ",".join("?" for _ in eligible_books)
    store.initialize()
    with store.connect() as conn:
        rows = conn.execute(
            f"""SELECT c.selection_key,c.provider,c.fixture_id,c.bookmaker,
                       c.market_id,c.market_name,c.outcome_id,c.outcome_name,
                       c.player_id,c.player_name,c.line_value,c.price_decimal,
                       c.price_american,c.observed_at,f.sport_name,
                       f.tournament_name,f.participant1_name,f.participant2_name
                FROM current_odds c JOIN fixtures f
                  ON f.provider=c.provider AND f.fixture_id=c.fixture_id
                WHERE c.active=1 AND c.main_line=1 AND c.price_decimal>1
                  AND c.bookmaker IN ({placeholders})
                  AND c.observed_at>=? AND c.observed_at<=?
                  AND f.start_time IS NOT NULL AND f.start_time>?
                ORDER BY f.start_time,c.fixture_id,c.market_name,c.outcome_name
                LIMIT ?""",
            (*sorted(eligible_books), (stamp-max_age).isoformat(),
             stamp.isoformat(), stamp.isoformat(), limit),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        price = float(item["price_decimal"])
        if not isfinite(price) or price <= 1:
            continue
        item["implied_probability"] = implied_probability(price)
        item["event_name"] = " vs ".join(
            str(p) for p in (item["participant1_name"], item["participant2_name"]) if p
        ) or item["fixture_id"]
        result.append(item)
    return result


def selected_slip(selections: list[dict]) -> dict:
    if not 1 <= len(selections) <= 6:
        raise ValueError("Choose between 1 and 6 legs")
    if len({row["bookmaker"] for row in selections}) != 1:
        raise ValueError("Choose selections from one sportsbook")
    if len({row["fixture_id"] for row in selections}) != len(selections):
        raise ValueError("Same-game legs need a verified correlation model")
    if len({row["selection_key"] for row in selections}) != len(selections):
        raise ValueError("Duplicate selection")
    prices = [float(row["price_decimal"]) for row in selections]
    if any(not isfinite(price) or price <= 1 for price in prices):
        raise ValueError("Invalid sportsbook quote")
    combined = combined_decimal_odds(prices)
    return {
        "bookmaker": selections[0]["bookmaker"],
        "combined_decimal_odds": combined,
        "combined_implied_probability": implied_probability(combined),
        "assumption": "independent fixtures; not a calibrated model prediction",
    }
