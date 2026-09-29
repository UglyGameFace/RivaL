from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class MarketObservation(BaseModel):
    """Provider-neutral price state for one sportsbook selection."""

    provider: str
    fixture_id: str
    sport_id: str | int | None = None
    tournament_id: str | int | None = None
    bookmaker: str
    market_id: str
    market_name: str | None = None
    outcome_id: str
    outcome_name: str | None = None
    player_id: str
    player_name: str | None = None
    line_value: float | None = None
    line_group_value: float | None = None
    deeplink: str | None = None

    active: bool
    main_line: bool = False
    price_decimal: float
    price_american: str | None = None
    price_fractional: str | None = None
    limit: float | None = None

    changed_at: datetime
    bookmaker_changed_at: datetime | None = None
    observed_at: datetime = Field(default_factory=utc_now)
    exchange_meta: Any | None = None

    @field_validator("changed_at", "bookmaker_changed_at", "observed_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("market timestamps must be timezone-aware")
        return value

    @property
    def selection_key(self) -> str:
        identity = (
            f"{self.provider}\x1f{self.fixture_id}\x1f{self.bookmaker}\x1f"
            f"{self.market_id}\x1f{self.outcome_id}\x1f{self.player_id}"
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @property
    def state_fingerprint(self) -> str:
        """Hash only fields whose change matters to line-state history."""

        payload = {
            "active": self.active,
            "main_line": self.main_line,
            "price_decimal": self.price_decimal,
            "price_american": self.price_american,
            "price_fractional": self.price_fractional,
            "limit": self.limit,
            "line_value": self.line_value,
            "line_group_value": self.line_group_value,
            "exchange_meta": self.exchange_meta,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class NormalizedOddsBoard(BaseModel):
    provider: str
    fixture_id: str
    sport_id: str | int | None = None
    tournament_id: str | int | None = None
    status_id: int | None = None
    status_name: str | None = None
    start_time: datetime | None = None
    updated_at: datetime | None = None
    participant1_name: str | None = None
    participant2_name: str | None = None
    sport_name: str | None = None
    tournament_name: str | None = None
    observations: list[MarketObservation] = Field(default_factory=list)
