from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class BookmakerEntitlement(BaseModel):
    has_live_odds: bool = False
    has_player_props: bool = False


class AccountSnapshot(BaseModel):
    """Normalized view of the active OddsPapi subscription."""

    subscription_id: str
    request_limit: int
    request_count: int
    sport_ids: list[int] = Field(default_factory=list)
    bookmakers: dict[str, BookmakerEntitlement] = Field(default_factory=dict)
    websocket_access: int = 0
    last_request: datetime | None = None

    @property
    def remaining_requests(self) -> int:
        return max(self.request_limit - self.request_count, 0)


class Fixture(BaseModel):
    """Provider-neutral fixture fields needed by the first ingestion milestone."""

    model_config = ConfigDict(populate_by_name=True)

    fixture_id: str = Field(alias="fixtureId")
    sport_id: int = Field(alias="sportId")
    tournament_id: int = Field(alias="tournamentId")
    status_id: int = Field(alias="statusId")
    status_name: str = Field(alias="statusName")
    has_odds: bool = Field(alias="hasOdds")
    start_time: datetime = Field(alias="startTime")
    participant1_name: str | None = Field(default=None, alias="participant1Name")
    participant2_name: str | None = Field(default=None, alias="participant2Name")
    tournament_name: str | None = Field(default=None, alias="tournamentName")


class HistoricalOddsEnvelope(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    fixture_id: str = Field(alias="fixtureId")
    bookmakers: dict[str, dict] = Field(default_factory=dict)
