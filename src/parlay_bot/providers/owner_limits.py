"""Persistent, owner-editable API spending ceilings.

These are owner safety ceilings, not provider entitlements. Provider-reported
allowances must always be checked independently before billable requests.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from uuid import uuid4

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
            self._lock_budget_transaction(conn, self.store)
            row = conn.execute(
                "SELECT state_value FROM runtime_state WHERE state_key=?",
                (KEY_PREFIX + provider,),
            ).fetchone()
        if row is None:
            return None
        return OwnerLimit(**json.loads(row["state_value"]))

    @staticmethod
    def _record_change(conn, value: OwnerLimit, previous: str | None, stamp: str) -> None:
        conn.execute(
            """INSERT INTO owner_api_limit_audit
               (audit_id,provider,changed_at,previous_json,new_json)
               VALUES(?,?,?,?,?)""",
            (str(uuid4()), value.provider, stamp, previous, json.dumps(asdict(value))),
        )

    @staticmethod
    def _lock_budget_transaction(conn, store: RelationalHotStore) -> None:
        # Share the same lock as billable quota reservations. An owner
        # downgrade cannot race a concurrent provider claim.
        if store.database.backend == "postgres":
            conn.execute("SELECT pg_advisory_xact_lock(72649113004)")
        else:
            conn.execute("BEGIN IMMEDIATE")

    def save(self, value: OwnerLimit) -> None:
        """Keep existing programmatic upsert; record every committed change."""
        self.store.initialize()
        stamp = datetime.now(UTC).isoformat()
        serialized = json.dumps(asdict(value))
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT state_value FROM runtime_state WHERE state_key=?",
                (KEY_PREFIX + value.provider,),
            ).fetchone()
            previous = str(row["state_value"]) if row is not None else None
            conn.execute(
                """INSERT INTO runtime_state(state_key,state_value,updated_at)
                   VALUES(?,?,?)
                   ON CONFLICT(state_key) DO UPDATE SET
                     state_value=excluded.state_value,
                     updated_at=excluded.updated_at""",
                (KEY_PREFIX + value.provider, serialized, stamp),
            )
            self._record_change(conn, value, previous, stamp)

    def save_if_unchanged(
        self, value: OwnerLimit, *, expected: OwnerLimit | None,
    ) -> None:
        """Atomic owner-confirmed compare-and-swap; never overwrite newer UI edits."""
        self.store.initialize()
        stamp = datetime.now(UTC).isoformat()
        serialized = json.dumps(asdict(value))
        with self.store.connect() as conn:
            self._lock_budget_transaction(conn, self.store)
            if expected is None:
                updated = conn.execute(
                    """INSERT INTO runtime_state(state_key,state_value,updated_at)
                       VALUES(?,?,?) ON CONFLICT(state_key) DO NOTHING
                       RETURNING state_key""",
                    (KEY_PREFIX + value.provider, serialized, stamp),
                ).fetchone()
            else:
                updated = conn.execute(
                    """UPDATE runtime_state
                       SET state_value=?,updated_at=?
                       WHERE state_key=? AND state_value=?
                       RETURNING state_key""",
                    (serialized, stamp, KEY_PREFIX + value.provider,
                     json.dumps(asdict(expected))),
                ).fetchone()
            if updated is None:
                raise ValueError(
                    "API settings were changed in another menu; review them again"
                )
            self._record_change(
                conn, value,
                json.dumps(asdict(expected)) if expected is not None else None,
                stamp,
            )

    def history(self, provider: str, *, limit: int = 5) -> list[dict]:
        if provider not in PROVIDERS or not 1 <= limit <= 25:
            raise ValueError("Invalid provider or history limit")
        self.store.initialize()
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT changed_at,previous_json,new_json
                   FROM owner_api_limit_audit
                   WHERE provider=? ORDER BY changed_at DESC LIMIT ?""",
                (provider, limit),
            ).fetchall()
        return [dict(row) for row in rows]
