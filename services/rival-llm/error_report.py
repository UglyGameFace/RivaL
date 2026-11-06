"""Standard-library Sentry reporting for RivaL Intelligence (no pip dependencies).

Sends ERROR log records (with their traceback, when present) and the
memory-pressure warning to the ``rival-intelligence`` Sentry project via
Sentry's public envelope endpoint. Sending happens on a background thread and
never blocks or breaks inference. No request bodies, prompts, tokens or model
answers are sent: only the log message and code frames. The same message is
sent at most once every DEDUPE_SECONDS.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
import traceback
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

# A DSN is send-only by design. RIVAL_LLM_SENTRY_DSN overrides; "" disables.
DEFAULT_DSN = ("https://76f3dc94f8d48c788d7b0fad78d129db@o4512385053097984"
               ".ingest.us.sentry.io/4512385076625408")
DEDUPE_SECONDS = 600
_DSN = re.compile(r"^https://([0-9a-f]{32})@([A-Za-z0-9.-]+)/(\d+)$")
_SECRET = re.compile(r"(token|secret|password|authorization)\S*\s*[=:]\s*\S+", re.IGNORECASE)


def parse_dsn(dsn: str) -> tuple[str, str] | None:
    """(envelope URL, auth header) for a Sentry DSN, or None if invalid."""
    match = _DSN.fullmatch(dsn.strip())
    if not match:
        return None
    key, host, project = match.groups()
    return (f"https://{host}/api/{project}/envelope/",
            f"Sentry sentry_version=7, sentry_key={key}, sentry_client=rival-llm/1.0")


def _release() -> str | None:
    try:
        value = Path(__file__).with_name("UPSTREAM_COMMIT").read_text("ascii").strip()
    except (OSError, UnicodeError):
        return None
    return value if re.fullmatch(r"[0-9a-f]{40}", value) else None


def build_event(record: logging.LogRecord) -> dict:
    message = _SECRET.sub(r"\1=[scrubbed]", record.getMessage())[:2000]
    event = {
        "event_id": uuid.uuid4().hex,
        "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
        "platform": "python",
        "level": "error" if record.levelno >= logging.ERROR else "warning",
        "logger": record.name,
        "environment": os.environ.get("SENTRY_ENVIRONMENT", "production"),
        "release": _release(),
        "message": {"formatted": message},
        "tags": {"service": "rival-intelligence"},
    }
    if record.exc_info and record.exc_info[1] is not None:
        exc_type, exc, tb = record.exc_info
        frames = [
            {"filename": Path(f.filename).name, "abs_path": f.filename,
             "function": f.name, "lineno": f.lineno, "context_line": f.line or "",
             "in_app": "site-packages" not in f.filename}
            for f in traceback.extract_tb(tb)
        ]
        event["exception"] = {"values": [{
            "type": exc_type.__name__,
            "value": _SECRET.sub(r"\1=[scrubbed]", str(exc))[:1000],
            "stacktrace": {"frames": frames},
            "mechanism": {"type": "logging", "handled": True},
        }]}
    return event


def envelope(event: dict) -> bytes:
    header = {"event_id": event["event_id"], "sent_at": datetime.now(UTC).isoformat()}
    body = json.dumps(event, default=str).encode()
    item = {"type": "event", "length": len(body)}
    return b"\n".join((json.dumps(header).encode(), json.dumps(item).encode(), body)) + b"\n"


class SentryHandler(logging.Handler):
    """ERROR records, plus the memory-pressure WARNING, to Sentry."""

    def __init__(self, dsn: str, *, sender=None) -> None:
        super().__init__(level=logging.WARNING)
        parsed = parse_dsn(dsn)
        if parsed is None:
            raise ValueError("Invalid Sentry DSN")
        self.url, self.auth = parsed
        self._sent: dict[str, float] = {}
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=50)
        self._sender = sender or self._post
        threading.Thread(target=self._drain, daemon=True, name="sentry-report").start()

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.ERROR and not record.getMessage().startswith(
                "memory_receipt"):
            return
        key = f"{record.name}|{record.msg}|{record.exc_info[0] if record.exc_info else ''}"
        now = time.monotonic()
        if now - self._sent.get(key, -DEDUPE_SECONDS) < DEDUPE_SECONDS:
            return
        self._sent[key] = now
        try:
            self._queue.put_nowait(envelope(build_event(record)))
        except (queue.Full, Exception):  # noqa: BLE001, S110 - no logging inside a log handler
            pass

    def _post(self, data: bytes) -> None:
        request = urllib.request.Request(self.url, data=data, method="POST", headers={
            "Content-Type": "application/x-sentry-envelope", "X-Sentry-Auth": self.auth})
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read(200)

    def _drain(self) -> None:
        while True:
            data = self._queue.get()
            try:
                self._sender(data)
            except Exception:  # noqa: BLE001, S110 - offline Sentry must not matter
                pass


def install(root: logging.Logger | None = None) -> SentryHandler | None:
    dsn = os.environ.get("RIVAL_LLM_SENTRY_DSN", DEFAULT_DSN).strip()
    if not dsn or ("PYTEST_CURRENT_TEST" in os.environ
                   and "RIVAL_LLM_SENTRY_DSN" not in os.environ):
        return None
    handler = SentryHandler(dsn)
    (root or logging.getLogger()).addHandler(handler)
    return handler
