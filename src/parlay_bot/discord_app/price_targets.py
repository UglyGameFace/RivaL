"""Source-verified personal BTC/ETH/SOL price-touch watches.

A price-touch observation is not a directional forecast or a probability of
hitting the goal. Until executable ticks exist, this scanner only counts
future *completed* 5m candles, and never invents intrabar execution.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

import discord

from parlay_bot.discord_app.opportunities import ASSETS, _cash, _utc
from parlay_bot.perpetuals.readiness import _source_identity

if TYPE_CHECKING:
    from parlay_bot.perpetuals.market_store import PerpetualMarketStore

HORIZONS = (5, 15)
FRESHNESS = timedelta(minutes=10)
MAX_PRICE = Decimal("1000000000000")


def parse_price(value: float | str | Decimal) -> str:
    """Canonical decimal, excluding NaN, infinity, negative and excessive precision."""
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Enter a valid positive USDT price.") from exc
    if not price.is_finite() or not Decimal("0") < price <= MAX_PRICE:
        raise ValueError("Enter a finite USDT price greater than zero.")
    if price.as_tuple().exponent < -8:
        raise ValueError("Use at most eight decimal places for the target price.")
    return format(price.normalize(), "f")


@dataclass(frozen=True)
class PriceGoal:
    price: str
    started_at: datetime
    venue: str
    contract: str
    quote: str
    reference_price: float

    @property
    def numeric_price(self) -> float:
        return float(self.price)


@dataclass(frozen=True)
class PriceResult:
    asset: str
    horizon_minutes: int
    target_price: str
    reference_price: float
    required_direction: str
    status: str
    explanation: str
    venue: str
    last_price: float | None
    observed_at: datetime | None
    started_at: datetime
    deadline: datetime
    hit_at: datetime | None = None

    @property
    def fingerprint(self) -> str:
        """No UTC-now noise; only real changes to the monitoring result."""
        value = {
            "asset": self.asset, "horizon": self.horizon_minutes,
            "target": self.target_price, "reference": self.reference_price,
            "required_direction": self.required_direction,
            "status": self.status, "explanation": self.explanation,
            "venue": self.venue, "last_price": self.last_price,
            "observed": self.observed_at.isoformat() if self.observed_at else None,
            "started": self.started_at.isoformat(),
            "deadline": self.deadline.isoformat(),
            "hit_at": self.hit_at.isoformat() if self.hit_at else None,
        }
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def create_price_goal(
    market: PerpetualMarketStore | None, *, asset: str,
    horizon_minutes: int, target_price: float | str | Decimal,
    now: datetime | None = None,
) -> PriceGoal:
    if asset not in ASSETS or horizon_minutes not in HORIZONS:
        raise ValueError("Choose BTC/ETH/SOL and 5m or 15m.")
    price = parse_price(target_price)
    stamp = now or datetime.now(UTC)
    if stamp.utcoffset() is None:
        raise ValueError("The market clock is unavailable.")
    stamp = stamp.astimezone(UTC)
    if market is None:
        raise ValueError("Live market data is unavailable. Cannot start a verified price target.")
    # Query only observed, contract-verified sources. Never use historical
    # offline imports as evidence of a live entry/reference quote.
    try:
        sources = market.feed_health()
        for health in sources:
            venue = str(health["venue"])
            if health["asset"] != asset or health["status"] != "ok":
                continue
            ident = _source_identity(venue, asset)
            if ident is None:
                continue
            contract, quote = ident
            if quote != "USDT":
                continue
            poll_at = _utc(str(health["last_success_at"]))
            if not timedelta(0) <= stamp - poll_at <= FRESHNESS:
                continue
            candles = market.recent_candles(
                venue=venue, contract=contract, quote=quote,
                interval_minutes=5, count=1, as_of=stamp,
            )
            if not candles:
                continue
            candle = candles[-1]
            observed = _utc(str(candle["first_observed_at"]))
            close = _utc(str(candle["closed_at"]))
            if not (
                close <= observed <= stamp
                and timedelta(0) <= stamp - observed <= FRESHNESS
                and timedelta(0) <= stamp - close <= FRESHNESS
            ):
                continue
            reference = float(candle["close_price"])
            if not math.isfinite(reference) or reference <= 0:
                continue
            if Decimal(price) == Decimal(str(reference)):
                raise ValueError("That target equals the latest known closed price.")
            return PriceGoal(
                price=price, started_at=stamp, venue=venue,
                contract=contract, quote=quote, reference_price=reference,
            )
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError("Cannot verify the latest exchange price right now.") from exc
    raise ValueError(
        "No recent verified USDT exchange candle. Cannot start a price target yet."
    )


def price_result(
    market: PerpetualMarketStore | None, *, asset: str,
    horizon_minutes: int, goal: PriceGoal,
    now: datetime | None = None,
) -> PriceResult:
    if asset not in ASSETS or horizon_minutes not in HORIZONS:
        raise ValueError("Invalid target market or horizon.")
    target = Decimal(parse_price(goal.price))
    stamp = now or datetime.now(UTC)
    if stamp.utcoffset() is None or goal.started_at.utcoffset() is None:
        raise ValueError("Naive target timestamp.")
    stamp = stamp.astimezone(UTC)
    start = goal.started_at.astimezone(UTC)
    deadline = start + timedelta(minutes=horizon_minutes)
    direction = "UP" if target > Decimal(str(goal.reference_price)) else "DOWN"
    base = dict(
        asset=asset, horizon_minutes=horizon_minutes,
        target_price=goal.price, reference_price=goal.reference_price,
        required_direction=direction, venue=goal.venue,
        started_at=start, deadline=deadline,
    )
    if market is None:
        return PriceResult(
            **base, status="WAIT", explanation="Live collector unavailable.",
            last_price=None, observed_at=None,
        )
    last_price: float | None = None
    latest_observed: datetime | None = None
    try:
        # Locked to the source and contract used at registration, not an
        # arbitrarily different venue with another price or contract type.
        with market.store.connect() as conn:
            rows = conn.execute(
                """SELECT opened_at,closed_at,first_observed_at,
                          high_price,low_price,close_price
                   FROM perp_market_candles
                   WHERE venue=? AND contract=? AND quote=?
                     AND interval_minutes=5
                     AND closed_at>=? AND closed_at<=?
                     AND first_observed_at>=? AND first_observed_at<=?
                   ORDER BY opened_at ASC LIMIT 8""",
                (
                    goal.venue, goal.contract, goal.quote,
                    start.isoformat(), deadline.isoformat(),
                    start.isoformat(), stamp.isoformat(),
                ),
            ).fetchall()
        for candle in rows:
            opened = _utc(str(candle["opened_at"]))
            closed = _utc(str(candle["closed_at"]))
            observed = _utc(str(candle["first_observed_at"]))
            high = float(candle["high_price"])
            low = float(candle["low_price"])
            close_price = float(candle["close_price"])
            if (
                not start <= closed <= deadline
                or not closed <= observed <= stamp
                or not all(math.isfinite(v) and v > 0 for v in (high, low, close_price))
                or not low <= close_price <= high
            ):
                continue
            # If the 5m candle started before opt-in, its high/low could
            # have occurred BEFORE the watch; use its final close only.
            reached = (
                Decimal(str(high if direction == "UP" else low))
                if opened >= start else Decimal(str(close_price))
            )
            last_price, latest_observed = close_price, observed
            touched = reached >= target if direction == "UP" else reached <= target
            if touched:
                return PriceResult(
                    **base, status="HIT",
                    explanation="Verified completed candle crossed your target after opt-in.",
                    last_price=close_price, observed_at=observed, hit_at=closed,
                )
        if stamp >= deadline:
            return PriceResult(
                **base, status="EXPIRED",
                explanation=(
                    "No target touch VERIFIED by available completed candles. "
                    "Intrabar or missing observations cannot be ruled out."
                ), last_price=last_price, observed_at=latest_observed,
            )
        feeds = {
            (str(x["venue"]), str(x["asset"])): x
            for x in market.feed_health()
        }
        health = feeds.get((goal.venue, asset))
        if health is None or health["status"] != "ok":
            return PriceResult(
                **base, status="WAIT",
                explanation="The selected exchange feed is unavailable.",
                last_price=last_price, observed_at=latest_observed,
            )
        poll_at = _utc(str(health["last_success_at"]))
        if not timedelta(0) <= stamp - poll_at <= FRESHNESS:
            return PriceResult(
                **base, status="WAIT",
                explanation="The selected exchange feed is stale.",
                last_price=last_price, observed_at=latest_observed,
            )
        latest = market.recent_candles(
            venue=goal.venue, contract=goal.contract, quote=goal.quote,
            interval_minutes=5, count=1, as_of=stamp,
        )
        if latest:
            last = latest[-1]
            observed = _utc(str(last["first_observed_at"]))
            closed = _utc(str(last["closed_at"]))
            close_price = float(last["close_price"])
            if (
                closed <= observed <= stamp
                and timedelta(0) <= stamp - observed <= FRESHNESS
                and math.isfinite(close_price) and close_price > 0
            ):
                last_price, latest_observed = close_price, observed
        return PriceResult(
            **base, status="WATCH",
            explanation="Monitoring completed candles. Target-hit probability is not validated.",
            last_price=last_price, observed_at=latest_observed,
        )
    except (ValueError, KeyError, TypeError, OverflowError):
        return PriceResult(
            **base, status="WAIT",
            explanation="Cannot verify this target with current market evidence.",
            last_price=None, observed_at=None,
        )


def price_embed(result: PriceResult) -> discord.Embed:
    title_status = {
        "WATCH": "👀 WATCH", "HIT": "✅ HIT", "EXPIRED": "⌛ EXPIRED",
        "WAIT": "⏳ WAIT",
    }[result.status]
    colors = {
        "WATCH": discord.Color.blurple(),
        "HIT": discord.Color.green(),
        "EXPIRED": discord.Color.orange(),
        "WAIT": discord.Color.orange(),
    }
    embed = discord.Embed(
        title=f"{result.asset} • {result.horizon_minutes}m • {title_status}",
        description=f"**{result.explanation}**",
        color=colors[result.status],
    )
    embed.add_field(name="Your target", value=f"**{_cash(float(result.target_price))}**")
    embed.add_field(
        name="Direction required", value=f"**{result.required_direction}**",
    )
    embed.add_field(
        name="Reference candle", value=_cash(result.reference_price),
    )
    if result.last_price is not None and result.observed_at is not None:
        embed.add_field(
            name="Last verified close",
            value=f"{_cash(result.last_price)} · "
                  f"{discord.utils.format_dt(result.observed_at, style='R')}",
            inline=False,
        )
    embed.add_field(
        name="Deadline", value=discord.utils.format_dt(result.deadline, style="R"),
    )
    if result.hit_at is not None:
        embed.add_field(
            name="Verified crossing candle",
            value=discord.utils.format_dt(result.hit_at, style="F"),
            inline=False,
        )
    embed.set_footer(
        text="5m completed-candle evidence, NOT instant price or hit probability • /rival-stop",
    )
    return embed
