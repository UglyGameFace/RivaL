"""Cross-process, pessimistic API budget reservations in the operational database.

Do not use this for market probabilities or trading. Only account-scoped
quota claims are stored. Never store an API key, quote, or provider payload.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from parlay_bot.providers.errors import QuotaReserveReached
from parlay_bot.providers.owner_limits import KEY_PREFIX, OwnerLimit
from parlay_bot.storage.hot import RelationalHotStore

UNKNOWN_PERIOD = "0000-unverified"


@dataclass(frozen=True)
class QuotaClaim:
    metric: str
    period: str
    observed: int
    maximum: int | None
    reserve: int
    requested: int
    partial: bool = False
    owner_cap: bool = False


class SharedProviderBudget:
    """Serialize reservations across bot processes sharing a DB and API key.

    PostgreSQL uses a transaction-scoped advisory lock for the short database
    transaction, and SQLite uses BEGIN IMMEDIATE. No network IO is performed
    while either lock is held. A request reserves units BEFORE contacting the
    provider; errors may still bill, so reservations are never refunded.
    """

    def __init__(self, store: RelationalHotStore, *, provider: str, api_key: str) -> None:
        if provider not in ("oddspapi", "sportsgameodds") or not api_key:
            raise ValueError("Unsupported provider account")
        self.store = store
        self.provider = provider
        self.account_key = provider + ":" + hashlib.sha256(api_key.encode()).hexdigest()
        self.store.initialize()

    def reserve(self, *claims: QuotaClaim) -> tuple[int, ...]:
        if not claims or len({claim.metric for claim in claims}) != len(claims):
            raise ValueError("Quota claims must have distinct metrics")
        for claim in claims:
            if (
                claim.requested <= 0
                or claim.observed < 0
                or claim.reserve < 0
                or not claim.period
                or (claim.maximum is not None and claim.maximum < 0)
            ):
                raise ValueError("Invalid provider quota claim")

        planned: list[tuple[QuotaClaim, int, str, int]] = []
        stamp = datetime.now(UTC).isoformat()
        with self.store.connect() as conn:
            if self.store.database.backend == "postgres":
                # One short lock across both provider metrics and owner changes.
                conn.execute("SELECT pg_advisory_xact_lock(72649113004)")
            else:
                conn.execute("BEGIN IMMEDIATE")

            owner: OwnerLimit | None = None
            if any(c.owner_cap for c in claims):
                setting = conn.execute(
                    "SELECT state_value FROM runtime_state WHERE state_key=?",
                    (KEY_PREFIX + self.provider,),
                ).fetchone()
                if setting is not None:
                    owner = OwnerLimit(**json.loads(setting["state_value"]))

            for claim in claims:
                row = conn.execute(
                    """SELECT period_key,projected_units
                       FROM rival_provider_quota_reservations
                       WHERE account_key=? AND metric=?""",
                    (self.account_key, claim.metric),
                ).fetchone()
                previous = str(row["period_key"]) if row is not None else None
                projected = int(row["projected_units"]) if row is not None else 0

                if previous is not None and previous != claim.period:
                    if claim.period == UNKNOWN_PERIOD or claim.period < previous:
                        raise QuotaReserveReached("Provider reported older or unknown billing period")
                    if previous != UNKNOWN_PERIOD:
                        # Only the provider's NEWER, verified boundary may
                        # reset projected units. An unknown->known transition
                        # preserves the prior pessimistic reservation.
                        projected = 0
                spent = max(claim.observed, projected)
                # Provider reserve and owner reserve are independent ceilings;
                # do not charge the provider reserve against the owner ceiling.
                effective_limit = (
                    claim.maximum - claim.reserve
                    if claim.maximum is not None else None
                )
                if claim.owner_cap and owner is not None:
                    owner_limit = owner.maximum_units - owner.reserve_units
                    effective_limit = (
                        min(effective_limit, owner_limit)
                        if effective_limit is not None else owner_limit
                    )
                available = (
                    claim.requested if effective_limit is None
                    else effective_limit - spent
                )
                if available <= 0:
                    raise QuotaReserveReached(
                        "Shared " + self.provider + " " + claim.metric + " budget exhausted"
                    )
                granted = min(claim.requested, available) if claim.partial else claim.requested
                if granted > available:
                    raise QuotaReserveReached(
                        "Shared " + self.provider + " " + claim.metric + " budget exhausted"
                    )
                planned.append((claim, spent + granted, claim.period, granted))

            for claim, total, period, _granted in planned:
                conn.execute(
                    """INSERT INTO rival_provider_quota_reservations
                       (account_key,metric,period_key,projected_units,updated_at)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(account_key,metric) DO UPDATE SET
                       period_key=excluded.period_key,
                       projected_units=excluded.projected_units,
                       updated_at=excluded.updated_at""",
                    (self.account_key, claim.metric, period, total, stamp),
                )
        return tuple(granted for _claim, _total, _period, granted in planned)
