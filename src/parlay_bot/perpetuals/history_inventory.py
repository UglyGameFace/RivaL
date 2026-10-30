"""Six-calendar-month immutable candle inventory, no fabricated history."""
from __future__ import annotations

import calendar
from datetime import UTC, datetime

from parlay_bot.discord_app.data_dashboard import MARKET_SYMBOLS
from parlay_bot.perpetuals.market_store import PerpetualMarketStore
from parlay_bot.perpetuals.markets import ASSETS


def six_months_prior(moment: datetime) -> datetime:
    if moment.utcoffset() is None:
        raise ValueError("six-month audit needs timezone-aware clock")
    now = moment.astimezone(UTC)
    month_zero = now.year * 12 + now.month - 1 - 6
    year, month_index = divmod(month_zero, 12)
    month = month_index + 1
    day = min(now.day, calendar.monthrange(year, month)[1])
    return now.replace(year=year, month=month, day=day)


def history_inventory(
    market: PerpetualMarketStore, *,
    asset: str, venues: tuple[str, ...], now: datetime | None = None,
) -> dict:
    if asset not in ASSETS:
        raise ValueError("unsupported asset")
    instant = (now or datetime.now(UTC)).astimezone(UTC)
    end = instant.replace(minute=instant.minute // 5 * 5,
                          second=0, microsecond=0)
    start = six_months_prior(end).replace(
        minute=six_months_prior(end).minute // 5 * 5,
        second=0, microsecond=0,
    )
    expected = int((end - start).total_seconds() // 300)
    streams = []
    market.initialize()
    with market.store.connect() as conn:
        for venue in dict.fromkeys(venues):
            if venue not in MARKET_SYMBOLS:
                continue
            template, quote = MARKET_SYMBOLS[venue]
            contract = (
                "XBTUSDTM" if venue == "kucoin" and asset == "BTC"
                else template.format(asset=asset)
            )
            row = conn.execute(
                """SELECT COUNT(*) AS n,
                          MIN(opened_at) AS first_open,
                          MAX(opened_at) AS last_open,
                          MAX(first_observed_at) AS newest_observed
                   FROM perp_market_candles
                   WHERE venue=? AND contract=? AND quote=?
                     AND interval_minutes=5
                     AND opened_at>=? AND opened_at<?""",
                (venue, contract, quote, start.isoformat(), end.isoformat()),
            ).fetchone()
            streams.append({
                "venue": venue, "contract": contract, "quote": quote,
                "saved": int(row["n"]) if row else 0,
                "expected": expected,
                "first_open": row["first_open"] if row else None,
                "last_open": row["last_open"] if row else None,
            })
    saved = sum(min(x["saved"], expected) for x in streams)
    target = expected * len(streams)
    return {
        "asset": asset, "from_utc": start.isoformat(), "to_utc": end.isoformat(),
        "saved": saved, "expected": target,
        "coverage_pct_ceiling": round(100 * saved / target, 3) if target else 0,
        "streams": streams,
        "gap_audit": "not_yet_performed",
        "backfill_is_live_data": False,
    }
