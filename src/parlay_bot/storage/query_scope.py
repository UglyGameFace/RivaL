"""Per-task PostgreSQL statement limits for Discord interaction work.

A Discord tap waits on its queries; a background worker may legitimately
run long ones (archiving, history recovery). The limit therefore follows
the asyncio task that serves an interaction, and every ``asyncio.to_thread``
call it makes, through a context variable. Most background workers never
set it; the 5-minute perp cycle sets a 60 s bound via ``mark_background``
so a statement parked on a lock aborts with QueryCanceled instead of
parking the cycle and its heartbeat.
"""
from __future__ import annotations

import contextvars

# 15 s: far longer than any menu should take, short enough that a runaway
# render cannot hold a pooled connection while the user has moved on.
INTERACTIVE_STATEMENT_TIMEOUT_MS = 15_000

# 60 s: an order of magnitude above the longest legitimate single statement
# on the 5-minute perp cycle path, 5x below the 300 s cycle budget. A
# statement parked on a Postgres lock aborts with QueryCanceled instead of
# parking the cycle; the stage logs it and the iteration still heartbeats.
BACKGROUND_STATEMENT_TIMEOUT_MS = 60_000

_statement_timeout_ms: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "rival_statement_timeout_ms", default=None,
)


def mark_interactive(timeout_ms: int = INTERACTIVE_STATEMENT_TIMEOUT_MS) -> None:
    """Limit queries for the rest of the current task (and threads it starts)."""
    if not 100 <= timeout_ms <= 600_000:
        raise ValueError("statement timeout must be 100 ms to 10 minutes")
    _statement_timeout_ms.set(timeout_ms)


def mark_background(timeout_ms: int = BACKGROUND_STATEMENT_TIMEOUT_MS) -> None:
    """Limit queries for the rest of the current background task (and threads it starts).

    Same mechanism as ``mark_interactive`` with a longer bound: the 5-minute
    perp cycle calls this once, and every ``to_thread`` in that task then
    issues ``SET LOCAL statement_timeout`` per checkout.
    """
    if not 100 <= timeout_ms <= 600_000:
        raise ValueError("statement timeout must be 100 ms to 10 minutes")
    _statement_timeout_ms.set(timeout_ms)


def current_statement_timeout_ms() -> int | None:
    return _statement_timeout_ms.get()
