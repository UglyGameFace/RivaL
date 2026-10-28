"""Combine independently sourced perpetual forecasts without inventing missing data.

Inputs must be point-in-time predictions from validated upstream models, NOT
raw exchange prices. This code alone is not a trained forecasting model.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable, Sequence

HORIZONS = frozenset({5, 15, 60, 240})


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def finite(value: float, low: float | None = None, high: float | None = None) -> bool:
    return math.isfinite(value) and (low is None or value >= low) and (
        high is None or value <= high
    )


@dataclass(frozen=True)
class SourceForecast:
    source: str
    origin: str  # Independent upstream venue, not a duplicated/resold feed.
    asset: str
    quote: str
    horizon_minutes: int
    observed_at: datetime
    probability_up: float
    quality: float
    reference_price: float
    contract_kind: str = "linear_perpetual"

    def __post_init__(self) -> None:
        if not all((self.source.strip(), self.origin.strip(), self.asset.strip(),
                    self.quote.strip())):
            raise ValueError("incomplete instrument or source identity")
        if self.horizon_minutes not in HORIZONS:
            raise ValueError("unsupported horizon")
        if self.contract_kind not in {"linear_perpetual", "inverse_perpetual"}:
            raise ValueError("not a supported perpetual contract")
        utc(self.observed_at)
        if not finite(self.probability_up, 0, 1) or not finite(self.quality, 0, 1):
            raise ValueError("probability and quality must be between 0 and 1")
        if not finite(self.reference_price, 1e-12):
            raise ValueError("reference price must be positive")


@dataclass(frozen=True)
class SourceSkill:
    count: int = 0
    brier_sum: float = 0.0
    recent_count: float = 0.0
    recent_brier_sum: float = 0.0

    def reliability(self) -> float:
        """Weight settled outcomes using discounted Brier loss and a neutral prior."""
        if self.count < 0 or self.recent_count < 0 or any(
            not finite(x, 0) for x in (self.brier_sum, self.recent_brier_sum)
        ):
            raise ValueError("invalid skill statistics")
        brier = (self.recent_brier_sum + 1.25) / (self.recent_count + 5)
        return min(1.5, max(0.5, 0.25 / max(0.08, brier)))


@dataclass(frozen=True)
class FusedForecast:
    id: str
    asset: str
    quote: str
    horizon_minutes: int
    issued_at: datetime
    due_at: datetime
    probability_up: float
    agreement: float  # Agreement is NOT forecast accuracy.
    reference_source: str
    reference_price: float
    contributors: tuple[SourceForecast, ...]
    weights: tuple[float, ...]
    contract_kind: str = "linear_perpetual"
    model_version: str = "reliability-fusion-v1"


def capped_weights(raw: Sequence[float], cap: float = 0.4) -> tuple[float, ...]:
    """Iterative water filling prevents one venue dominating the ensemble."""
    if len(raw) * cap < 1:
        raise ValueError("insufficient independent sources for cap")
    remainder = set(range(len(raw)))
    weights = [0.0] * len(raw)
    budget = 1.0
    while remainder:
        total = sum(raw[i] for i in remainder)
        if total <= 0:
            raise ValueError("no usable source weights")
        oversized = {i for i in remainder if budget * raw[i] / total > cap}
        if not oversized:
            for i in remainder:
                weights[i] = budget * raw[i] / total
            break
        for i in oversized:
            weights[i] = cap
            budget -= cap
        remainder -= oversized
    return tuple(weights)


def fuse_forecasts(
    forecasts: Sequence[SourceForecast],
    *,
    issued_at: datetime,
    skill_for: Callable[[str, str, str, int], SourceSkill],
    max_age: timedelta = timedelta(minutes=2),
    min_sources: int = 3,
) -> FusedForecast | None:
    """Require three fresh independent venues, matched contracts, and settled skill.

    Does NOT average raw prices or treat source agreement as confidence.
    An unavailable/expired source is omitted; low quorum means no prediction.
    """
    now = utc(issued_at)
    if max_age <= timedelta(0) or min_sources < 3:
        raise ValueError("invalid source policy")
    if not forecasts:
        return None
    first = forecasts[0]
    key = (first.asset, first.quote, first.horizon_minutes, first.contract_kind)
    origins: dict[str, SourceForecast] = {}
    for item in forecasts:
        if (item.asset, item.quote, item.horizon_minutes, item.contract_kind) != key:
            raise ValueError("cannot fuse mismatched contracts or horizons")
        age = now - utc(item.observed_at)
        if age < timedelta(0) or age > max_age or item.quality < 0.2:
            continue
        old = origins.get(item.origin)
        if old is None or utc(item.observed_at) > utc(old.observed_at):
            origins[item.origin] = item
    # Multiple API wrappers around one actual venue count as only one source.
    sources: list[SourceForecast] = []
    seen: set[str] = set()
    for origin in sorted(origins):
        item = origins[origin]
        if item.source not in seen:
            sources.append(item)
            seen.add(item.source)
    if len(sources) < min_sources:
        return None
    contributors = tuple(sources)
    raw_weights = [
        item.quality * skill_for(
            item.asset, item.quote, item.origin, item.horizon_minutes
        ).reliability()
        for item in contributors
    ]
    weights = capped_weights(raw_weights)
    mean = sum(w * item.probability_up for item, w in zip(contributors, weights))
    variance = sum(
        w * (item.probability_up - mean) ** 2
        for item, w in zip(contributors, weights)
    )
    agreement = max(0.0, 1.0 - 2.0 * math.sqrt(variance))
    # When sources disagree, retreat towards an uninformative 50% forecast.
    probability = 0.5 + (mean - 0.5) * agreement
    reference = sorted(
        contributors, key=lambda x: (-x.quality, -utc(x.observed_at).timestamp(), x.origin)
    )[0]
    digest = hashlib.sha256(
        f"{key}|{now.isoformat()}".encode("utf-8")
    ).hexdigest()
    return FusedForecast(
        id=digest, asset=key[0], quote=key[1], horizon_minutes=key[2],
        issued_at=now, due_at=now + timedelta(minutes=key[2]),
        probability_up=probability, agreement=agreement,
        reference_source=reference.source, reference_price=reference.reference_price,
        contributors=contributors, weights=weights, contract_kind=key[3],
    )
