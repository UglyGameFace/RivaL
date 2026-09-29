from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class MarketOutcomeDefinition(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    outcome_id: str = Field(alias="outcomeId")
    outcome_name: str = Field(alias="outcomeName")


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
