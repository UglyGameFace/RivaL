"""Source-scoped recovery decisions, never guesses about missing exchange history.

Only an independently verified provider contract-start timestamp or explicit
provider discontinuation can establish a permanent non-applicable period.
A 0-row HTTP 200, absent Drive name, 404, or repeated error is NOT that proof.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

MONTH = re.compile(r"20\d{2}-(?:0[1-9]|1[0-2])\Z")
FAMILIES = frozenset(("market", "mark", "index", "premium"))
ASSETS = frozenset(("BTC", "ETH", "SOL", "XRP"))


def decision(*, asset: str, family: str, month: str, reason: str,
             now: datetime, attempts: int = 1,
             contract_start: str | None = None) -> dict:
    """Evidence status plus an explicit next check. Never infer permanent absence."""
    if (asset not in ASSETS or family not in FAMILIES
        or not MONTH.fullmatch(month) or not 1 <= attempts <= 1000
        or now.utcoffset() is None):
        raise ValueError("invalid source-specific recovery evidence")
    stamp = now.astimezone(UTC)
    state = "source_gap_needs_recheck"
    days = 1
    permanent = False
    if contract_start is not None:
        opened = datetime.fromisoformat(contract_start)
        if opened.utcoffset() is None:
            raise ValueError("contract start must be official offset-aware timestamp")
        if month < opened.astimezone(UTC).strftime("%Y-%m"):
            state = "pre_current_contract_listing_official_evidence"
            days = 0
            permanent = True
    if not permanent:
        if "incomplete UTC grid" in reason and "(0/" in reason:
            state, days = "source_returned_no_rows_not_permanently_proven", 7
        elif "incomplete UTC grid" in reason:
            state, days = "isolated_source_gap_repair_pending", 1
        elif any(x in reason for x in ("429", "503", "504", "transport exhausted")):
            state, days = "temporary_source_throttle_or_transport", 0
        elif "restricted HTTP" in reason:
            state, days = "source_access_restricted_not_permanently_proven", 7
        else:
            state, days = "source_rejected_or_schema_failure_needs_review", 1
    # Bounded exponential defer for repeated failed confirmations, not deletion.
    pause = (timedelta(days=min(30, days * 2 ** min(attempts - 1, 3)))
             if days else timedelta(hours=min(12, 2 ** min(attempts, 4))))
    return {
        "asset": asset, "family": family, "month": month,
        "source_status": state, "permanent": permanent,
        "reason": reason[:180], "attempts": attempts,
        "last_checked_utc": stamp.isoformat(),
        "retry_after_utc": None if permanent else (stamp + pause).isoformat(),
        "provider_original_bytes_verified": False,
    }


def deferred(previous: dict, *, asset: str, family: str, month: str,
             now: datetime) -> bool:
    """Only valid same-scope negative evidence delays rechecks."""
    if (not isinstance(previous, dict) or previous.get("asset") != asset
        or previous.get("family") != family or previous.get("month") != month
        or now.utcoffset() is None):
        return False
    if previous.get("permanent") is True:
        return previous.get("source_status") == (
            "pre_current_contract_listing_official_evidence")
    value = previous.get("retry_after_utc")
    if not isinstance(value, str):
        return False
    try:
        until = datetime.fromisoformat(value)
        checked = datetime.fromisoformat(previous["last_checked_utc"])
    except (KeyError, TypeError, ValueError):
        return False
    if until.utcoffset() is None or checked.utcoffset() is None:
        return False
    # Never extend stale/untrusted cached outages indefinitely.
    instant = now.astimezone(UTC)
    return checked <= instant and checked >= instant - timedelta(days=35) and until > instant
