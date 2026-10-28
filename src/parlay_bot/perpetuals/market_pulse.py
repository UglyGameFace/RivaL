"""Read-only five-minute cross-venue market pulse, not a betting signal.

Outputs synchronized descriptive market movement from validated PostgreSQL
candles. Does not invent price targets, stop losses or calibrated confidence.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from parlay_bot.perpetuals.analysis_evidence import InsufficientEvidence, build_evidence
from parlay_bot.perpetuals.market_store import PerpetualMarketStore


@dataclass(frozen=True)
class MarketPulse:
    asset: str
    observed_at: datetime
    sources: int
    configured: int
    median_5m_pct: float
    median_15m_pct: float | None
    median_1h_pct: float | None
    up: int
    down: int
    flat: int
    range_5m_pct: tuple[float, float]
    median_5m_volatility_pct: float | None
    missing: int
    model_edge_verified: bool = False
    horizon_minutes: int = 5


def calculate_market_pulse(
    market: PerpetualMarketStore, *,
    asset: str, venues: tuple[str, ...],
    now: datetime | None = None, horizon_minutes: int = 5,
) -> MarketPulse:
    """Compare five-minute CLOSE-to-CLOSE returns within each venue.

    All 5m values describe a *completed past* candle, never the next one.
    Agreement counts do not imply independent statistical evidence: exchanges
    often react to the same underlying global market move.
    """
    instant = now or datetime.now(UTC)
    if instant.utcoffset() is None:
        raise ValueError("market pulse requires timezone-aware time")
    evidence, _digest = build_evidence(
        market, asset=asset, venues=venues, now=instant,
        horizon_minutes=horizon_minutes,
    )
    short_returns: list[float] = []
    quarter_returns: list[float] = []
    hour_returns: list[float] = []
    volatilities: list[float] = []
    closed_times: list[datetime] = []
    for source in evidence["sources"]:
        windows = source["windows"]
        short = windows.get("5m")
        if not isinstance(short, dict):
            continue
        change = float(short["return_pct"])
        if not math.isfinite(change):
            continue
        short_returns.append(change)
        quarter = windows.get("15m")
        if isinstance(quarter, dict):
            change15 = float(quarter["return_pct"])
            if math.isfinite(change15):
                quarter_returns.append(change15)
        hour = windows.get("1h")
        if isinstance(hour, dict):
            change60 = float(hour["return_pct"])
            if math.isfinite(change60):
                hour_returns.append(change60)
        vol = quarter.get("volatility_pct_per_5m") if isinstance(quarter, dict) else None
        if vol is not None and math.isfinite(float(vol)):
            volatilities.append(float(vol))
        closed = datetime.fromisoformat(str(source["last_candle_close"]))
        if closed.utcoffset() is None or closed > instant + timedelta(seconds=1):
            raise InsufficientEvidence("Source candle timestamps are inconsistent.")
        closed_times.append(closed.astimezone(UTC))
    if len(short_returns) < 3:
        raise InsufficientEvidence("Three fresh sources with closed 5m history required.")
    return MarketPulse(
        asset=asset,
        observed_at=min(closed_times),
        sources=len(short_returns),
        configured=int(evidence["coverage"]["configured_venues"]),
        median_5m_pct=statistics.median(short_returns),
        median_15m_pct=statistics.median(quarter_returns) if quarter_returns else None,
        median_1h_pct=statistics.median(hour_returns) if hour_returns else None,
        up=sum(r > 0 for r in short_returns),
        down=sum(r < 0 for r in short_returns),
        flat=sum(r == 0 for r in short_returns),
        range_5m_pct=(min(short_returns), max(short_returns)),
        median_5m_volatility_pct=(
            statistics.median(volatilities) if volatilities else None
        ),
        missing=len(evidence["unavailable_sources"]),
        horizon_minutes=horizon_minutes,
    )
