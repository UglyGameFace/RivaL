"""Sentry error and performance reporting for the RivaL bot.

Privacy: no default PII (no user IPs, emails or usernames), no request
bodies, and any value whose key looks like a credential is scrubbed before
an event leaves the process. Quota: a repeated error is sent at most once
per DEDUPE_SECONDS per fingerprint; Sentry still groups the issue.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path

_LOG = logging.getLogger(__name__)

# A DSN only lets a client *send* events to this project; it is designed to be
# embedded in shipped code. SENTRY_DSN overrides it; SENTRY_DSN="" disables.
DEFAULT_DSN = (
    "https://11a93ebf1f74649f63190fefe4e8e42e@o4512385053097984"
    ".ingest.us.sentry.io/4512385088684032"
)
TRACES_SAMPLE_RATE = 0.1
DEDUPE_SECONDS = 600
_SECRET_KEY = re.compile(r"(token|secret|password|passphrase|api[_-]?key|dsn|"
                         r"authorization|cookie|database_url|credential)", re.IGNORECASE)
_SECRET_VALUE = re.compile(r"(postgres(ql)?://\S+|[A-Za-z0-9_\-]{24,}\.[A-Za-z0-9_\-]{6,}"
                           r"\.[A-Za-z0-9_\-]{27,})")
_recent: dict[str, float] = {}
_lock = threading.Lock()


def release() -> str | None:
    """The deployed commit, written next to the package by the release workflow."""
    path = Path(__file__).with_name("RELEASE_SHA")
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        value = os.environ.get("SENTRY_RELEASE", "").strip()
    return value if re.fullmatch(r"[0-9a-f]{7,40}", value or "") else None


def _scrub(value, depth: int = 0):
    if depth > 8:
        return value
    if isinstance(value, dict):
        return {k: ("[scrubbed]" if isinstance(k, str) and _SECRET_KEY.search(k)
                    else _scrub(v, depth + 1)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, depth + 1) for v in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("[scrubbed]", value)
    return value


# Exceptions that RivaL code raises on purpose and handles (an empty order
# book, a region-blocked exchange, a slow third party). Sentry's asyncio
# integration reports them when they leave a task even though the awaiting
# code catches them, which is noise, not a crash. Real failures of these
# loops are still logged with _LOG.exception and reported through logging.
EXPECTED_IN_TASKS = frozenset({
    "MarketUnavailable", "QuoteUnavailable", "HTTPStatusError", "ConnectError",
    "ConnectTimeout", "ReadTimeout", "ReadError", "RemoteProtocolError",
    "PoolTimeout", "TimeoutError", "CancelledError",
})


def _expected_task_error(event) -> bool:
    values = (event.get("exception") or {}).get("values") or []
    if not values:
        return False
    last = values[-1]
    return ((last.get("mechanism") or {}).get("type") == "asyncio"
            and last.get("type") in EXPECTED_IN_TASKS)


def before_send(event, hint):
    """Scrub credentials and drop repeats of the same error within 10 minutes."""
    if event.get("type") == "check_in":
        # The SDK passes cron check-ins through before_send too. They carry
        # no logger, message or exception, so every check-in from every
        # monitor shared one dedupe key and only one per 10 minutes left
        # the process: RIVAL-A/B/8/9 reported "missed" for loops that were
        # running on time. heartbeat() already limits each monitor to one
        # check-in per minute, and a check-in holds no user data to scrub.
        return event
    if _expected_task_error(event):
        return None
    key = "|".join(str(p) for p in (
        event.get("logger"),
        (event.get("logentry") or {}).get("message"),
        *[(e.get("type"), (e.get("stacktrace") or {}).get("frames", [{}])[-1].get("lineno"))
          for e in (event.get("exception") or {}).get("values", [])],
    ))
    now = time.monotonic()
    with _lock:
        last = _recent.get(key)
        if last is not None and now - last < DEDUPE_SECONDS:
            return None
        _recent[key] = now
        if len(_recent) > 500:
            cutoff = now - DEDUPE_SECONDS
            for stale in [k for k, t in _recent.items() if t < cutoff]:
                _recent.pop(stale, None)
    return _scrub(event)


def init_sentry() -> bool:
    """Start Sentry if a DSN is configured; never stops the bot from starting."""
    dsn = os.environ.get("SENTRY_DSN", DEFAULT_DSN).strip()
    if "PYTEST_CURRENT_TEST" in os.environ and "SENTRY_DSN" not in os.environ:
        dsn = ""  # tests never report to the production project
    if not dsn:
        _LOG.info("Sentry disabled (SENTRY_DSN is empty)")
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.asyncio import AsyncioIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        _LOG.warning("Sentry SDK not installed; error reporting disabled")
        return False
    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
            release=release(),
            send_default_pii=False,
            traces_sample_rate=TRACES_SAMPLE_RATE,
            max_request_body_size="never",
            before_send=before_send,
            integrations=[
                AsyncioIntegration(),
                LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
            ],
        )
    except Exception:  # noqa: BLE001 - monitoring must never block the bot
        _LOG.warning("Sentry could not start; error reporting disabled", exc_info=True)
        return False
    _LOG.info("Sentry error reporting on release=%s", release() or "unknown")
    return True


_beats: dict[str, float] = {}
HEARTBEAT_SECONDS = 60


def heartbeat(slug: str) -> None:
    """Tell a Sentry cron monitor this loop just completed a cycle.

    At most one check-in per minute per monitor. A loop that stops or hangs
    misses its check-ins and Sentry opens an issue. Never raises.
    """
    now = time.monotonic()
    if now - _beats.get(slug, -HEARTBEAT_SECONDS) < HEARTBEAT_SECONDS:
        return
    _beats[slug] = now
    try:
        import sentry_sdk
        from sentry_sdk.crons import capture_checkin

        if sentry_sdk.get_client().is_active():
            capture_checkin(monitor_slug=slug, status="ok")
    except Exception:  # noqa: BLE001 - monitoring must never break a loop
        _LOG.debug("Sentry heartbeat failed slug=%s", slug, exc_info=True)
