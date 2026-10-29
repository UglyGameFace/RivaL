"""Conservative, independently held-out PRICE TOUCH probability research.

This model uses fixed momentum cohorts and barrier-distance normalization.
Its time-ordered holdout is NOT used for parameter tuning, calibration,
training, or issuing past outcomes as prospective predictions. It can say
"NOT QUALIFIED" indefinitely; this is preferable to fabricated confidence.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from parlay_bot.discord_app.price_targets import PriceGoal

if TYPE_CHECKING:
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore

LOOKBACK = 12
MAX_CANDLES = 5000
MIN_TRAIN = 160
MIN_HOLDOUT = 70
MIN_DAYS = 7
MIN_COHORT = 35
MIN_GAIN = 0.005


@dataclass(frozen=True)
class HitProbabilityEvidence:
    status: str
    reason: str
    probability: float | None
    trained: int
    holdout: int
    holdout_days: int
    brier: float | None
    baseline_brier: float | None
    venue: str
    horizon_minutes: int

    @property
    def qualified(self) -> bool:
        return self.status == "qualified_shadow" and self.probability is not None


@dataclass(frozen=True)
class _Sample:
    day: str
    aligned: bool
    hit: int


def _time(value: str) -> datetime:
    t = datetime.fromisoformat(value)
    if t.utcoffset() is None:
        raise ValueError("Naive timestamp")
    return t.astimezone(UTC)


def _volatility(prices: list[float]) -> float:
    returns = [math.log(b / a) for a, b in itertools.pairwise(prices)]
    mean = sum(returns) / len(returns)
    return math.sqrt(sum((x - mean) ** 2 for x in returns) / len(returns))


def _insufficient(
    venue: str, horizon: int, reason: str,
    trained: int = 0, holdout: int = 0, days: int = 0,
    brier: float | None = None, baseline: float | None = None,
) -> HitProbabilityEvidence:
    return HitProbabilityEvidence(
        "not_qualified", reason, None, trained, holdout, days,
        brier, baseline, venue, horizon,
    )


def evaluate_price_hit_model(
    market: PerpetualMarketStore | None, *, goal: PriceGoal,
    horizon_minutes: int, now: datetime | None = None,
) -> HitProbabilityEvidence:
    """Return P(touch) ONLY if chronological forward holdout beats baseline.

    Labels are non-overlapping and require original provider-observed candle
    provenance. A retrospective backfill is not called prospective evidence.
    """
    if horizon_minutes not in (5, 15):
        raise ValueError("unsupported horizon")
    if goal.quote != "USDT":
        raise ValueError("only USDT-targeted instruments supported")
    if market is None:
        return _insufficient(goal.venue, horizon_minutes, "No market collector")
    stamp = now or datetime.now(UTC)
    if stamp.utcoffset() is None or goal.started_at.utcoffset() is None:
        raise ValueError("UTC timestamps required")
    cutoff = min(stamp.astimezone(UTC), goal.started_at.astimezone(UTC))
    try:
        rows = market.recent_candles(
            venue=goal.venue, contract=goal.contract, quote=goal.quote,
            interval_minutes=5, as_of=cutoff, count=MAX_CANDLES,
        )
        if len(rows) < LOOKBACK + MIN_TRAIN + MIN_HOLDOUT:
            return _insufficient(goal.venue, horizon_minutes, "Insufficient observed 5m history")
        bars = horizon_minutes // 5
        # Fixed barrier distance in rolling volatility units. Never optimize
        # this number on the future holdout outcomes.
        recent_prices = [float(row["close_price"]) for row in rows[-LOOKBACK - 1:]]
        if any(not math.isfinite(x) or x <= 0 for x in recent_prices):
            return _insufficient(goal.venue, horizon_minutes, "Invalid reference history")
        live_sigma = _volatility(recent_prices)
        reference = float(goal.reference_price)
        target = float(goal.price)
        if not 0 < reference < math.inf or not 0 < target < math.inf:
            return _insufficient(goal.venue, horizon_minutes, "Invalid price barrier")
        distance_z = abs(math.log(target / reference)) / max(
            live_sigma * math.sqrt(bars), 0.0000001,
        )
        if distance_z > 5 or distance_z < 0.02:
            return _insufficient(goal.venue, horizon_minutes, "Barrier outside modeled volatility range")
        ascending = target > reference
        samples: list[_Sample] = []
        # Uncorrelated labels: take one reference per prediction horizon.
        # Nonoverlapping label windows; a boundary purge below excludes overlaps.
        for i in range(LOOKBACK, len(rows) - bars, bars + 1):
            history = rows[i - LOOKBACK:i + 1]
            future = rows[i + 1:i + 1 + bars]
            group = history + future
            if len(future) != bars:
                continue
            if any(not math.isfinite(float(c["close_price"])) or
                   float(c["close_price"]) <= 0 for c in group):
                continue
            opened = [_time(str(c["opened_at"])) for c in group]
            if any(int((b - a).total_seconds()) != 300
                   for a, b in itertools.pairwise(opened)):
                continue
            issued = _time(str(history[-1]["first_observed_at"]))
            close_time = _time(str(history[-1]["closed_at"]))
            if not close_time <= issued < cutoff:
                continue
            # No future outcome known at issuance. Reject backfilled
            # historical data that was not seen close to the actual bar.
            if issued > close_time + timedelta(minutes=10):
                continue
            clean = True
            for row in future:
                observed = _time(str(row["first_observed_at"]))
                closed = _time(str(row["closed_at"]))
                if not (
                    closed <= observed < cutoff
                    and observed > issued
                    and observed <= closed + timedelta(minutes=10)
                ):
                    clean = False
                    break
            if not clean:
                continue
            prices = [float(c["close_price"]) for c in history]
            sigma = _volatility(prices)
            if not math.isfinite(sigma) or sigma <= 0:
                continue
            level = prices[-1] * math.exp(
                (1 if ascending else -1) * distance_z * sigma * math.sqrt(bars)
            )
            aligned = (
                (prices[-1] > prices[-4]) if ascending
                else (prices[-1] < prices[-4])
            )
            verified = False
            for row in future:
                start_at = _time(str(row["opened_at"]))
                if start_at >= issued:
                    price = float(row["high_price"] if ascending else row["low_price"])
                else:
                    # Its partial pre-issuance high/low would be look-ahead.
                    price = float(row["close_price"])
                if not math.isfinite(price) or price <= 0:
                    clean = False
                    break
                if (price >= level) if ascending else (price <= level):
                    verified = True
            if clean:
                samples.append(_Sample(
                    issued.date().isoformat(), aligned, int(verified),
                ))
        minimum = MIN_TRAIN + MIN_HOLDOUT
        if len(samples) < minimum:
            return _insufficient(
                goal.venue, horizon_minutes,
                "Too few nonoverlapping, truly observed future outcomes",
                trained=len(samples),
            )
        split = len(samples) // 2
        train, holdout = samples[:split - 1], samples[split:]
        days = len({s.day for s in holdout})
        if len(train) < MIN_TRAIN or len(holdout) < MIN_HOLDOUT or days < MIN_DAYS:
            return _insufficient(
                goal.venue, horizon_minutes,
                "Need ≥160 training events, ≥70 untouched holdout outcomes over ≥7 UTC days",
                len(train), len(holdout), days,
            )
        baseline = (sum(x.hit for x in train) + 2) / (len(train) + 4)
        cohorts = {
            aligned: [s for s in train if s.aligned is aligned]
            for aligned in (True, False)
        }
        if any(len(group) < MIN_COHORT for group in cohorts.values()):
            return _insufficient(
                goal.venue, horizon_minutes,
                "Both momentum cohorts require at least 35 training outcomes",
                len(train), len(holdout), days,
            )
        # Fixed Bayesian shrinkage to the independent training-only baseline.
        estimates = {
            key: (sum(x.hit for x in group) + baseline * 20)
                 / (len(group) + 20)
            for key, group in cohorts.items()
        }
        brier = sum((estimates[x.aligned] - x.hit) ** 2 for x in holdout) / len(holdout)
        base_brier = sum((baseline - x.hit) ** 2 for x in holdout) / len(holdout)
        if base_brier - brier < MIN_GAIN:
            return _insufficient(
                goal.venue, horizon_minutes,
                "Chronological holdout did not beat the frozen baseline Brier",
                len(train), len(holdout), days, brier, base_brier,
            )
        # A daily majority consistency gate helps reject a handful of lucky
        # overlapping regimes; it does not prove future profitability.
        day_gain: dict[str, list[float]] = {}
        for x in holdout:
            day_gain.setdefault(x.day, []).append(
                (baseline - x.hit) ** 2 - (estimates[x.aligned] - x.hit) ** 2
            )
        positive_days = sum(sum(v) > 0 for v in day_gain.values())
        if positive_days < math.ceil(days * 0.75):
            return _insufficient(
                goal.venue, horizon_minutes,
                "Holdout advantage is inconsistent across UTC days",
                len(train), len(holdout), days, brier, base_brier,
            )
        aligned_now = (
            recent_prices[-1] > recent_prices[-4] if ascending
            else recent_prices[-1] < recent_prices[-4]
        )
        return HitProbabilityEvidence(
            "qualified_shadow",
            "Conditional empirical hit probability beat frozen baseline "
            "on untouched chronological holdout. Shadow research only.",
            round(estimates[aligned_now], 4),
            len(train), len(holdout), days,
            round(brier, 5), round(base_brier, 5),
            goal.venue, horizon_minutes,
        )
    except (ValueError, TypeError, KeyError, OverflowError, ZeroDivisionError):
        return _insufficient(
            goal.venue, horizon_minutes, "Source chronology or price data invalid"
        )
