"""Read-only, causal forecast readiness for RivaL's live mathematical track.

Explain why a source can collect 5m bars yet have zero trustworthy forward
predictions. Never treat bulk historical backfill as live training evidence.
This does not issue predictions, change weights or call any exchange/Gemini API.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.features import live_examples, persisted_candles
from parlay_bot.perpetuals.fusion import utc
from parlay_bot.perpetuals.learning import walk_forward
from parlay_bot.perpetuals.market_store import PerpetualMarketStore

_MIN_ROWS = 65
_MIN_SAMPLES = 40
_MIN_EVALUATED = 20
_MAX_AGE = timedelta(minutes=10)


def _timestamp(value: object) -> datetime:
    return utc(datetime.fromisoformat(str(value)))


def _source_identity(venue: str, asset: str) -> tuple[str, str] | None:
    from parlay_bot.discord_app.data_dashboard import MARKET_SYMBOLS

    if venue not in MARKET_SYMBOLS:
        return None
    pattern, quote = MARKET_SYMBOLS[venue]
    contract = (
        "XBTUSDTM" if venue == "kucoin" and asset == "BTC"
        else pattern.format(asset=asset)
    )
    return contract, quote


def forecast_readiness(
    market: PerpetualMarketStore, *, asset: str,
    venues: tuple[str, ...], now: datetime | None = None,
) -> dict:
    """Recompute on demand from actual PostgreSQL observations.

    Only streams with a recent successful API check, recent fully closed
    candles and enough causally released labels can be called eligible.
    Eligibility is NOT a guarantee that the scheduling cycle issued a
    prediction, nor a claim of a profitable/approved model.
    """
    if asset not in ("BTC", "ETH", "SOL"):
        raise ValueError("unsupported perp asset")
    current = utc(now or datetime.now(UTC))
    status = {
        (str(r["venue"]), str(r["asset"])): r
        for r in market.feed_health()
    }
    records = []
    for venue in dict.fromkeys(venues):
        identity = _source_identity(venue, asset)
        if identity is None:
            continue
        record = {
            "venue": venue, "status": "not_collecting",
            "observations": 0, "recent_live_streak": 0,
            "causal_5m": 0, "causal_15m": 0,
            "evaluated_5m": 0, "evaluated_15m": 0,
        }
        health = status.get((venue, asset))
        if health is None or health.get("status") != "ok":
            record["status"] = "blocked_or_unavailable"
            records.append(record)
            continue
        try:
            last_success = _timestamp(health["last_success_at"])
            last_close = _timestamp(health["last_candle_close"])
            if not (
                timedelta(0) <= current - last_success <= _MAX_AGE
                and timedelta(0) <= current - last_close <= _MAX_AGE
            ):
                record["status"] = "stale"
                records.append(record)
                continue
            contract, quote = identity
            rows = market.recent_candles(
                venue=venue, contract=contract, quote=quote,
                interval_minutes=5, count=180, as_of=current,
            )
            record["observations"] = len(rows)
            if not rows:
                record["status"] = "no_usable_candles"
                records.append(record)
                continue
            candles = persisted_candles(rows)
            latest = candles[-1]
            if not timedelta(0) <= current - utc(latest.observed_at) <= _MAX_AGE:
                record["status"] = "stale"
                records.append(record)
                continue
            streak = 0
            last_open = None
            for bar in reversed(candles):
                opened = utc(bar.opened_at)
                closed = utc(bar.closed_at)
                observed = utc(bar.observed_at)
                # A late historical backfill cannot extend the live streak.
                if not (
                    timedelta(0) < observed - closed <= _MAX_AGE
                    and math.isfinite(bar.close_price) and bar.close_price > 0
                    and (last_open is None or last_open - opened == timedelta(minutes=5))
                ):
                    break
                streak += 1
                last_open = opened
            record["recent_live_streak"] = streak
            if len(candles) < _MIN_ROWS or streak < _MIN_ROWS:
                record["status"] = "needs_live_history"
                records.append(record)
                continue
            for horizon in (5, 15):
                samples = live_examples(candles, horizon_minutes=horizon)
                record[f"causal_{horizon}m"] = len(samples)
                if len(samples) < _MIN_SAMPLES:
                    continue
                model = walk_forward(
                    samples, as_of=current, minimum_evaluated=50,
                )
                record[f"evaluated_{horizon}m"] = model.evaluated
            if min(record["evaluated_5m"], record["evaluated_15m"]) >= _MIN_EVALUATED:
                record["status"] = "eligible_shadow"
            else:
                record["status"] = "needs_matured_labels"
        except (KeyError, TypeError, ValueError, ArithmeticError, OverflowError):
            record["status"] = "invalid_or_missing_history"
        records.append(record)

    ready = [r for r in records if r["status"] == "eligible_shadow"]
    waiting = [r for r in records if r["status"] == "needs_live_history"]
    return {
        "asset": asset, "as_of": current.isoformat(),
        "configured": len(records), "eligible": len(ready),
        "waiting_history": len(waiting),
        "stale_or_blocked": sum(
            r["status"] in ("blocked_or_unavailable", "stale") for r in records
        ),
        "min_live_bars": _MIN_ROWS,
        "min_evaluations": _MIN_EVALUATED,
        "best_live_streak": max((r["recent_live_streak"] for r in records), default=0),
        "best_causal_5m": max((r["causal_5m"] for r in records), default=0),
        "best_evaluated_5m": max((r["evaluated_5m"] for r in records), default=0),
        "streams": records,
    }
