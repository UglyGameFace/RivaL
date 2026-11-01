"""Safe, bounded PostgreSQL startup recovery for Discloud private DNS.

Only retry name resolution faults. Wrong passwords, malformed schemas,
unavailable tables and other PostgreSQL errors must continue to fail closed.
No SQLite fallback and no raw database URL or credentials in logs.
"""
from __future__ import annotations

import logging
import socket
import time
from collections.abc import Callable, Sequence

import psycopg

from parlay_bot.storage.hot import RelationalHotStore

_LOG = logging.getLogger(__name__)
_DNS_MARKERS = (
    "failed to resolve host",
    "could not translate host name",
    "name or service not known",
    "temporary failure in name resolution",
)
_STARTUP_PAUSES = (2.0, 4.0, 8.0, 12.0)


def _is_dns_operational_error(error: psycopg.OperationalError) -> bool:
    """Recognize resolver failures, never broad auth/network/SQL errors."""
    current: BaseException | None = error
    seen: set[int] = set()
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, socket.gaierror):
            return True
        detail = str(current).lower()
        if any(marker in detail for marker in _DNS_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


def initialize_with_dns_recovery(
    store: RelationalHotStore,
    *, pauses: Sequence[float] = _STARTUP_PAUSES,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Initialize the same authoritative DB after bounded DNS backoff.

    This cannot fix a genuinely wrong Discloud hostname or disabled VLAN.
    The correct host must be set in the protected RIVAL_DATABASE_URL variable.
    """
    if store.database.backend != "postgres":
        store.initialize()
        return
    if any(not 0 < pause <= 30 for pause in pauses) or len(pauses) > 8:
        raise ValueError("invalid PostgreSQL startup retry budget")
    for attempt in range(len(pauses) + 1):
        try:
            store.initialize()
            if attempt:
                _LOG.info(
                    "RivaL PostgreSQL private DNS recovered on attempt=%d",
                    attempt + 1,
                )
            return
        except psycopg.OperationalError as exc:
            if not _is_dns_operational_error(exc):
                raise
            if attempt == len(pauses):
                _LOG.error(
                    "RivaL PostgreSQL private DNS unresolved after %d attempts. "
                    "Check the existing Discloud database service is ONLINE, "
                    "the exact hostname in RIVAL_DATABASE_URL matches its "
                    "private HOSTNAME, and both applications share enabled VLAN.",
                    attempt + 1,
                )
                raise RuntimeError(
                    "RivaL PostgreSQL private hostname cannot resolve. "
                    "Check the database's Discloud hostname, running state and "
                    "private VLAN, and correct RIVAL_DATABASE_URL in Discloud "
                    "Variables. Existing data was not replaced."
                ) from None
            pause = float(pauses[attempt])
            _LOG.warning(
                "RivaL PostgreSQL private DNS unavailable; "
                "attempt=%d/%d retry_in=%.1fs (no credential details)",
                attempt + 1, len(pauses) + 1, pause,
            )
            sleep(pause)
