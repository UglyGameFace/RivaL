from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class RiskMode(StrEnum):
    LOWER = "lower"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"
    LONGSHOT = "longshot"


class ParlayLeg(BaseModel):
    fixture_id: str
    event_name: str
    bookmaker: str
    market_id: str
    market_name: str
    outcome_id: str
    outcome_name: str
    player_id: str
    player_name: str | None = None
    line_value: float | None = None
    deeplink: str | None = None
    price_decimal: float
    price_american: str | None = None
    market_fair_probability: float = Field(ge=0, le=1)
    price_edge: float
    reference_books: int = Field(ge=1)


class ParlaySlip(BaseModel):
    bookmaker: str
    risk_mode: RiskMode
    legs: list[ParlayLeg]
    combined_decimal_odds: float
    market_fair_probability: float = Field(ge=0, le=1)
    market_implied_edge: float
    correlation_policy: str = "one_leg_per_fixture"


class ParlayBuildError(ValueError):
    """RivaL cannot construct a valid slip from the currently cached market."""
