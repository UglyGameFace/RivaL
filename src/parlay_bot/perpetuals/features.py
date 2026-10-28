"""Leakage-checked live-observation examples from immutable venue candles.

The extractor deliberately ignores historical candles first observed only
during later backfill. That history can be used in separately labeled offline
research, but is not evidence of an executable point-in-time prediction.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from parlay_bot.perpetuals.fusion import HORIZONS, utc
from parlay_bot.perpetuals.hyperliquid import MarketCandle

LOOKBACK = 5


@dataclass(frozen=True)
class LabeledExample:
    venue: str
    contract: str
    quote: str
    horizon_minutes: int
    issued_at: datetime
    label_known_at: datetime
    features: tuple[float, ...]
    label_up: int


def persisted_candles(rows: Sequence[dict]) -> tuple[MarketCandle, ...]:
    """Read existing PostgreSQL candle records without inferring availability."""
    result = []
    for row in rows:
        result.append(MarketCandle(
            venue=str(row["venue"]), contract=str(row["contract"]),
            quote=str(row["quote"]), interval_minutes=int(row["interval_minutes"]),
            opened_at=datetime.fromisoformat(str(row["opened_at"])),
            closed_at=datetime.fromisoformat(str(row["closed_at"])),
            observed_at=datetime.fromisoformat(str(row["first_observed_at"])),
            open_price=float(row["open_price"]), high_price=float(row["high_price"]),
            low_price=float(row["low_price"]), close_price=float(row["close_price"]),
            volume_base=float(row["volume_base"]), trades=int(row["trades"]),
        ))
    return tuple(result)


def live_examples(
    candles: Sequence[MarketCandle], *, horizon_minutes: int,
) -> tuple[LabeledExample, ...]:
    """Return only outcomes that could genuinely be predicted from prior observations.

    All inputs share one venue/instrument/timeframe. Gaps and batched backfills
    prevent samples. Targets only contribute labels after first observation.
    """
    if horizon_minutes not in HORIZONS:
        raise ValueError("unsupported forecast horizon")
    if not candles:
        return ()
    ordered = sorted(candles, key=lambda c: utc(c.opened_at))
    first = ordered[0]
    interval = first.interval_minutes
    if interval <= 0 or horizon_minutes % interval:
        raise ValueError("incompatible source resolution for horizon")
    seconds = interval * 60
    identity = (first.venue, first.contract, first.quote, interval)
    prev_open = None
    for candle in ordered:
        opened = utc(candle.opened_at)
        closed = utc(candle.closed_at)
        observed = utc(candle.observed_at)
        if (
            (candle.venue, candle.contract, candle.quote, candle.interval_minutes)
            != identity
            or (prev_open is not None and opened <= prev_open)
            or closed <= opened or observed <= closed
            or not all(math.isfinite(x) and x > 0 for x in (
                candle.open_price, candle.close_price, candle.high_price,
                candle.low_price,
            ))
            or not math.isfinite(candle.volume_base) or candle.volume_base < 0
        ):
            raise ValueError("invalid candle chronology, provenance or prices")
        prev_open = opened

    horizon_bars = horizon_minutes // interval
    examples = []
    for idx in range(LOOKBACK, len(ordered) - horizon_bars):
        history = ordered[idx - LOOKBACK:idx + 1]
        current = ordered[idx]
        target = ordered[idx + horizon_bars]
        issued = utc(current.observed_at)
        available = utc(target.observed_at)
        # A backfill that reveals both sides together cannot be scored as live.
        if available <= issued or utc(target.closed_at) <= issued:
            continue
        if any(
            int((utc(b.opened_at) - utc(a.opened_at)).total_seconds()) != seconds
            for a, b in zip(history, history[1:])
        ):
            continue
        if int((utc(target.opened_at) - utc(current.opened_at)).total_seconds()) != (
            horizon_bars * seconds
        ):
            continue
        if any(utc(c.observed_at) > issued for c in history):
            continue
        closes = [x.close_price for x in history]
        returns = [
            math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes))
        ]
        mean = sum(returns) / len(returns)
        variance = sum((value - mean) ** 2 for value in returns) / len(returns)
        volume_mean = sum(x.volume_base for x in history[:-1]) / LOOKBACK
        features = (
            max(-5., min(5., returns[-1] * 100)),
            max(-5., min(5., math.log(closes[-1] / closes[0]) * 100)),
            max(0., min(5., math.sqrt(variance) * 100)),
            max(0., min(5., (current.high_price - current.low_price)
                              / current.close_price * 100)),
            max(-2., min(2., math.log((current.volume_base + 1)
                                     / (volume_mean + 1)))),
        )
        if target.close_price == current.close_price:
            continue
        examples.append(LabeledExample(
            venue=first.venue, contract=first.contract, quote=first.quote,
            horizon_minutes=horizon_minutes, issued_at=issued,
            label_known_at=available, features=features,
            label_up=int(target.close_price > current.close_price),
        ))
    return tuple(examples)
