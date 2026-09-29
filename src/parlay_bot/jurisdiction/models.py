from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class SportsbookAvailability(BaseModel):
    """Time-aware jurisdiction mapping, independent from stored market observations."""

    jurisdiction: str = Field(min_length=2, max_length=2)
    sportsbook: str
    online_available: bool
    effective_from: date | None = None
    effective_until: date | None = None
    source_url: str | None = None
    last_verified: date | None = None
