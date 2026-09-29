from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MarketOutcomeDefinition(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    outcome_id: str = Field(alias="outcomeId")
    outcome_name: str = Field(alias="outcomeName")

    @field_validator("outcome_id", mode="before")
    @classmethod
    def stringify_outcome_id(cls, value: Any) -> str:
        if value is None:
            raise ValueError("outcomeId is required")
        return str(value)


class MarketDefinition(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    market_id: str = Field(alias="marketId")
    market_length: int = Field(alias="marketLength", ge=1)
    market_name: str = Field(alias="marketName")
    player_prop: bool = Field(alias="playerProp")
    sport_id: int = Field(alias="sportId")
    handicap: float | None = None
    period: str | None = None
    market_type: str | None = Field(default=None, alias="marketType")
    outcomes: list[MarketOutcomeDefinition] = Field(default_factory=list)

    @field_validator("market_id", mode="before")
    @classmethod
    def stringify_market_id(cls, value: Any) -> str:
        if value is None:
            raise ValueError("marketId is required")
        return str(value)
