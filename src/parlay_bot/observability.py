"""Sentry error and performance reporting for the RivaL bot.

Privacy: no default PII (no user IPs, emails or usernames), no request
bodies, and any value whose key looks like a credential is scrubbed before
an event leaves the process. Quota: a repeated error is sent at most once
per DEDUPE_SECONDS per fingerprint; Sentry still groups the issue.
"""
from __future__ import annotations

import contextlib
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
    # Every Drive caller catches and logs it (RIVAL-W: each failure arrived
    # twice, once handled via logging and once as an asyncio "crash").
    "DriveArchiveError",
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
    # capture_message events carry a plain "message" string and no logentry;
    # without it every message event shared one dedupe key ("None|None")
    # and only the first model-quality alert in 10 minutes would leave.
    raw_message = event.get("message")
    if isinstance(raw_message, dict):
        raw_message = raw_message.get("formatted")
    # The fingerprint is part of the key: health alerts share one log
    # template ("RivaL HEALTH ALERT %s: %s"), so without it a second,
    # different alert within the window was dropped as a "repeat".
    key = "|".join(str(p) for p in (
        event.get("logger"),
        event.get("fingerprint"),
        (event.get("logentry") or {}).get("message") or raw_message,
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


# A challenger losing to the incumbent by more than the promotion bar is
# the promotion gate working as designed, not a fault in the host, a loop
# or the model code (the known example: RIVAL-F, liq_v1 phemex:ETH, held
# below the 0.025 bar in runtime_health._score_regressions). Those alerts
# still reach Sentry, but as warnings under their own fingerprint, so real
# faults stand out. The alert text is unchanged; only level, fingerprint
# and tag differ.
MODEL_QUALITY_FINGERPRINT = "model-quality"


def report_model_quality(key: str, detail: str) -> None:
    """Send a model-quality alert to Sentry as a warning, never an error.

    One Sentry issue per regression (fingerprint ["model-quality", key]),
    tagged alert_kind=model_quality. Never raises; a no-op when Sentry is
    not active (tests, empty DSN). Callers log the same text locally at
    warning level: logging it at error would ALSO create a second,
    error-level Sentry event via the logging integration.
    """
    try:
        import sentry_sdk
        if not sentry_sdk.get_client().is_active():
            return
        sentry_sdk.capture_message(
            f"RivaL HEALTH ALERT {key}: {detail}",
            level="warning",
            fingerprint=[MODEL_QUALITY_FINGERPRINT, key],
            tags={"alert_kind": "model_quality"},
        )
    except Exception:  # noqa: BLE001 - monitoring must never break a loop
        _LOG.debug("model-quality Sentry report failed key=%s", key, exc_info=True)


HEALTH_FINGERPRINT = "health-alert"


@contextlib.contextmanager
def health_alert_scope(key: str):
    """Group an error-level health alert as its own Sentry issue.

    Every watchdog alert is logged with one template, so Sentry put all of
    them (stale feeds, Drive reads, memory, ...) in a single issue
    (RIVAL-F): a new kind of fault was just another event on an old issue
    and never sent a "new issue" email. Inside this scope the logging
    integration's event carries fingerprint ["health-alert", key]. Never
    raises; without the SDK it is a plain no-op scope.
    """
    try:
        import sentry_sdk
        scope_cm = sentry_sdk.new_scope()
    except Exception:  # noqa: BLE001 - monitoring must never break a loop
        yield
        return
    with scope_cm as scope:
        scope.fingerprint = [HEALTH_FINGERPRINT, key]
        yield


def init_sentry() -> bool:
    """Start Sentry if a DSN is configured; never stops the bot from starting."""
    if "PYTEST_CURRENT_TEST" in os.environ:
        # Tests never report to the production project, even when SENTRY_DSN
        # is exported in the shell: an explicit DSN used to defeat this
        # guard, so a test run sent health-alert errors as production
        # issues in environment "production", indistinguishable from real
        # ones. (The SDK's own init inside tests uses a fake DSN with a
        # capture transport; see test_observability.py.)
        return False
    dsn = os.environ.get("SENTRY_DSN", DEFAULT_DSN).strip()
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
