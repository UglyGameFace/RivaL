"""Leakage-checked live-observation examples from immutable venue candles.

The extractor deliberately ignores historical candles first observed only
during later backfill. That history can be used in separately labeled offline
research, but is not evidence of an executable point-in-time prediction.
"""
from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from parlay_bot.perpetuals.fusion import HORIZONS, utc
from parlay_bot.perpetuals.hyperliquid import MarketCandle

LOOKBACK = 5
INDICATOR_BARS = 40


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


def _ema(values: Sequence[float], period: int) -> list[float]:
    """Wilder-style EMA seeded by the SMA of the first period values."""
    if len(values) < period or period < 1:
        raise ValueError("not enough values for EMA")
    seed = sum(values[:period]) / period
    out = [seed]
    multiplier = 2.0 / (period + 1)
    for value in values[period:]:
        out.append((value - out[-1]) * multiplier + out[-1])
    return out


def indicator_features(
    closes: Sequence[float], highs: Sequence[float],
    lows: Sequence[float], volumes: Sequence[float],
) -> tuple[float, float, float, float, float]:
    """Causal indicator states over a 40-bar window, all walk-forward safe.

    Every input is a past closed bar; nothing here reads the target or any
    bar after the issuance bar. All five outputs are clamped to the
    [-5, 5] range the walk-forward fitter requires. Order is fixed:
    RSI state, MACD histogram state, Bollinger z-score, ATR percent,
    volume regime.
    """
    if not (
        len(closes) == len(highs) == len(lows) == len(volumes) == INDICATOR_BARS
    ):
        raise ValueError("indicator window must hold exactly 40 bars")
    if any(not math.isfinite(x) or x <= 0 for x in closes):
        raise ValueError("invalid indicator closes")
    if any(not math.isfinite(x) or x < 0 for x in volumes):
        raise ValueError("invalid indicator volumes")
    current = closes[-1]
    # RSI(14): Wilder smoothing over close-to-close changes.
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain = sum(gains[:14]) / 14
    avg_loss = sum(losses[:14]) / 14
    for gain, loss in zip(gains[14:], losses[14:]):
        avg_gain = (avg_gain * 13 + gain) / 14
        avg_loss = (avg_loss * 13 + loss) / 14
    rsi = (
        50.0 if avg_gain == 0 and avg_loss == 0
        else 100.0 if avg_loss == 0
        else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    )
    rsi_state = max(-5.0, min(5.0, (rsi - 50.0) / 10.0))
    # MACD(12, 26, 9) histogram: EMA12 minus EMA26, minus its 9-bar signal.
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    macd = [fast - slow for fast, slow in zip(ema12[14:], ema26)]
    signal = _ema(macd, 9)
    histogram = macd[-1] - signal[-1]
    macd_state = max(-5.0, min(5.0, histogram / current * 100.0))
    # Bollinger(20) z-score: distance from the 20-bar mean in 2-sigma units.
    recent = closes[-20:]
    mean20 = sum(recent) / 20
    variance20 = sum((value - mean20) ** 2 for value in recent) / 20
    std20 = math.sqrt(variance20)
    bollinger = 0.0 if std20 == 0 else (current - mean20) / (2.0 * std20)
    bollinger_state = max(-5.0, min(5.0, bollinger))
    # ATR(14) percent: mean true range over the last 14 bars, scaled by price.
    ranges = []
    for idx in range(len(closes) - 14, len(closes)):
        prev_close = closes[idx - 1]
        ranges.append(max(
            highs[idx] - lows[idx],
            abs(highs[idx] - prev_close),
            abs(lows[idx] - prev_close),
        ))
    atr_state = max(0.0, min(5.0, sum(ranges) / len(ranges) / current * 100.0))
    # Volume regime: current volume against the 39-bar mean, log scale.
    volume_mean = sum(volumes[:-1]) / (INDICATOR_BARS - 1)
    volume_state = max(-2.0, min(2.0, math.log(
        (volumes[-1] + 1) / (volume_mean + 1))))
    return (rsi_state, macd_state, bollinger_state, atr_state, volume_state)


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
    # A batch can reveal several historical candles at the same instant.
    # They are not independent live issuance opportunities. Keep only the
    # latest causally eligible window per real observation timestamp.
    by_issuance: dict[datetime, LabeledExample] = {}
    ordered_by_open: dict[datetime, datetime] = {}
    for idx in range(INDICATOR_BARS - 1, len(ordered) - horizon_bars):
        history = ordered[idx - LOOKBACK:idx + 1]
        extended = ordered[idx - INDICATOR_BARS + 1:idx + 1]
        current = ordered[idx]
        target = ordered[idx + horizon_bars]
        issued = utc(current.observed_at)
        available = utc(target.observed_at)
        # A backfill that reveals both sides together cannot be scored as live.
        if available <= issued or utc(target.closed_at) <= issued:
            continue
        if any(
            int((utc(b.opened_at) - utc(a.opened_at)).total_seconds()) != seconds
            for a, b in itertools.pairwise(extended)
        ):
            continue
        if int((utc(target.opened_at) - utc(current.opened_at)).total_seconds()) != (
            horizon_bars * seconds
        ):
            continue
        if any(utc(c.observed_at) > issued for c in extended):
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
            *indicator_features(
                [x.close_price for x in extended],
                [x.high_price for x in extended],
                [x.low_price for x in extended],
                [x.volume_base for x in extended],
            ),
        )
        if target.close_price == current.close_price:
            continue
        example = LabeledExample(
            venue=first.venue, contract=first.contract, quote=first.quote,
            horizon_minutes=horizon_minutes, issued_at=issued,
            label_known_at=available, features=features,
            label_up=int(target.close_price > current.close_price),
        )
        # Retain the most recent candle among windows visible at this instant.
        # Do not rewrite observed_at or create synthetic earlier predictions.
        previous = by_issuance.get(issued)
        if previous is None or utc(current.opened_at) > utc(ordered_by_open[issued]):
            by_issuance[issued] = example
            ordered_by_open[issued] = current.opened_at
    return tuple(by_issuance[key] for key in sorted(by_issuance))
