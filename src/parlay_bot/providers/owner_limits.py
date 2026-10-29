"""Persistent, owner-editable API spending ceilings.

These are owner safety ceilings, not provider entitlements. Provider-reported
allowances must always be checked independently before billable requests.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from parlay_bot.storage.hot import RelationalHotStore

PROVIDERS = frozenset({"oddspapi", "sportsgameodds"})
KEY_PREFIX = "rival:api-budget:v1:"


@dataclass(frozen=True)
class OwnerLimit:
    provider: str
    plan: str
    maximum_units: int
    reserve_units: int

    def __post_init__(self) -> None:
        if self.provider not in PROVIDERS:
            raise ValueError("Unsupported provider")
        if not 1 <= len(self.plan) <= 32 or not self.plan.replace("-", "").replace("_", "").isalnum():
            raise ValueError("Invalid plan label")
        if not 0 <= self.maximum_units <= 1_000_000_000:
            raise ValueError("Limit must be between 0 and 1 billion")
        if not 0 <= self.reserve_units <= self.maximum_units:
            raise ValueError("Reserve cannot exceed allowance")


class OwnerLimitStore:
    def __init__(self, store: RelationalHotStore) -> None:
        self.store = store

    def get(self, provider: str) -> OwnerLimit | None:
        if provider not in PROVIDERS:
            raise ValueError("Unsupported provider")
        self.store.initialize()
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT state_value FROM runtime_state WHERE state_key=?",
                (KEY_PREFIX + provider,),
            ).fetchone()
        if row is None:
            return None
        return OwnerLimit(**json.loads(row["state_value"]))

    def save(self, value: OwnerLimit) -> None:
        self.store.initialize()
        with self.store.connect() as conn:
            conn.execute(
                """INSERT INTO runtime_state(state_key,state_value,updated_at)
                   VALUES(?,?,?)
                   ON CONFLICT(state_key) DO UPDATE SET
                     state_value=excluded.state_value,
                     updated_at=excluded.updated_at""",
                (KEY_PREFIX + value.provider, json.dumps(asdict(value)), datetime.now(UTC).isoformat()),
            )
