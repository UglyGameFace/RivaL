"""One authoritative, private Stop All path for every RivaL DM producer.

The live Polymarket tracker, exchange target, and optional Auto-Hunt
notifications persist independently. Stop every service, even if one fails.
Never report success for a service whose cancellation is unverified.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

_LOG = logging.getLogger(__name__)


async def stop_all_notifications(
    user_id: int, *, auto_hunt: object | None,
    targets: object | None, tracker: object | None,
) -> str:
    """Cancel all known per-user delivery sources without coupling failures.

    A timeout means that source is unconfirmed, not stopped. Running all
    cancellations concurrently prevents a stalled DM edit from blocking
    storage-level subscription revocation in the other providers.
    """
    operations: list[tuple[str, Callable[[], Awaitable[bool]]]] = []
    if auto_hunt is not None:
        operations.append(("optional alerts", lambda: auto_hunt.stop(user_id)))
    if targets is not None:
        operations.append(("exchange forecast watch", lambda: targets.stop(user_id)))
    if tracker is not None:
        operations.append(("live Polymarket tracker", lambda: tracker.stop(user_id)))
    if not operations:
        return "No alert services are available. Nothing was changed."

    async def cancel(name: str, action: Callable[[], Awaitable[bool]]):
        try:
            return name, bool(await asyncio.wait_for(action(), timeout=10)), False
        except Exception:  # noqa: BLE001 - each cancellation is independent
            _LOG.exception("RivaL Stop All could not confirm %s", name)
            return name, False, True

    results = await asyncio.gather(*(cancel(name, action) for name, action in operations))
    failed = [name for name, _, error in results if error]
    stopped = [name for name, done, error in results if done and not error]
    if failed:
        return (
            "Stop All was only partly confirmed. Could not verify: "
            + ", ".join(failed)
            + ". Retry Stop All. "
            + (("Stopped: " + ", ".join(stopped) + ".") if stopped
               else "Other sources were already off or unavailable.")
        )
    if stopped:
        return "Stopped all personal notifications and live tracking."
    return "All personal notifications and live tracking are already off."
