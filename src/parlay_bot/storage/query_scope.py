"""Per-task PostgreSQL statement limits for Discord interaction work.

A Discord tap waits on its queries; a background worker may legitimately
run long ones (archiving, history recovery). The limit therefore follows
the asyncio task that serves an interaction, and every ``asyncio.to_thread``
call it makes, through a context variable. Most background workers never
set it; the 5-minute perp cycle sets a 60 s bound via ``mark_background``
so a statement parked on a lock aborts with QueryCanceled instead of
parking the cycle and its heartbeat. The watchdog health check sets a
12 s bound for its own block via ``statement_timeout`` (never the whole
loop: the one-time acceptance join spawned beside it needs ~20 s).
"""
from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

# 8 s: below the 10 s dashboard render wait and the 9 s forecast render
# wait, so a parked render query aborts before the render gives up instead
# of holding a pooled connection for up to 5 s after it (the RIVAL-N
# shape). It is the largest value that still does that: the 12 s GuidedHome
# redraw and the 10 s signal wait are total budgets for sequences of
# individually capped reads (largest single-statement budgets on the
# interaction path are the 8 s tracking starts and the 7 s Performance
# readers), and the 11 s CDNA lookup is HTTP, not a statement — so no
# interaction-path query that could still serve its caller is killed by it.
INTERACTIVE_STATEMENT_TIMEOUT_MS = 8_000

# 12 s: under every 15 s wait on the watchdog health-check path (inspect,
# settlement repair, publish), so a parked statement aborts before the wait
# abandons it — the abandoned thread no longer holds a pooled connection
# indefinitely. Long enough for a warm health check (point reads plus
# 10-minute-cached aggregates); the one-time post-restart acceptance join
# (~20 s) is deliberately outside this scope.
WATCHDOG_STATEMENT_TIMEOUT_MS = 12_000

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


@contextmanager
def statement_timeout(timeout_ms: int) -> Iterator[None]:
    """Bound statements for the enclosed block, then restore the previous bound.

    ``mark_*`` set the bound for the rest of the task; this scopes it to one
    block instead, so a short bound for the watchdog health check cannot leak
    into the one-time acceptance join spawned beside it (which needs ~20 s).
    The bound follows into ``asyncio.to_thread`` calls made inside the block
    and into tasks spawned inside it, but not into tasks spawned after it.
    """
    if not 100 <= timeout_ms <= 600_000:
        raise ValueError("statement timeout must be 100 ms to 10 minutes")
    token = _statement_timeout_ms.set(timeout_ms)
    try:
        yield
    finally:
        _statement_timeout_ms.reset(token)
