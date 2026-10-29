"""Conservative provider-budget policy shared by RivaL's API integrations.

Limits are in provider-specific *billable units*. SportsGameOdds entity units
must never be conflated with OddsPapi requests. Unknown provider usage fails
closed instead of assuming a free-tier allowance.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderBudget:
    """Owner-configured limit with an independent provider-verified ceiling.

    All counts apply to the same provider billing window. The provider ceiling
    must be obtained from the API account endpoint, not guessed from plan name.
    """

    configured_limit: int
    reserve: int
    verified_limit: int | None
    verified_used: int | None
    locally_reserved: int = 0

    def __post_init__(self) -> None:
        if self.configured_limit < 0 or self.reserve < 0 or self.locally_reserved < 0:
            raise ValueError("budget values must be nonnegative")
        if self.verified_limit is not None and self.verified_limit < 0:
            raise ValueError("verified limit must be nonnegative")
        if self.verified_used is not None and self.verified_used < 0:
            raise ValueError("verified used must be nonnegative")

    @property
    def effective_limit(self) -> int | None:
        if self.verified_limit is None or self.verified_used is None:
            return None
        return min(self.configured_limit, self.verified_limit)

    @property
    def available(self) -> int:
        ceiling = self.effective_limit
        if ceiling is None:
            return 0
        return max(0, ceiling - self.verified_used - self.reserve - self.locally_reserved)

    def permits(self, estimated_billable_units: int) -> bool:
        if estimated_billable_units <= 0:
            raise ValueError("estimated billable units must be positive")
        return estimated_billable_units <= self.available


def implied_probability(decimal_odds: float) -> float:
    """Market-implied probability, NOT RivaL's calibrated win probability."""
    import math
    if not math.isfinite(decimal_odds) or decimal_odds <= 1:
        raise ValueError("decimal odds must be finite and greater than 1")
    return 1 / decimal_odds
