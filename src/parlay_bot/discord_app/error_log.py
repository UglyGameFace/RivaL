"""In-memory digest of recent RivaL warnings and errors for the status card.

Groups repeated log lines by logger and message template, counts them and
keeps the latest rendered text, redacted and truncated. Nothing is written to
disk or the database, and secrets that slip into a message are masked.
"""
from __future__ import annotations

import logging
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

MAX_GROUPS = 200
MAX_TEXT = 180
_REDACTIONS = (
    (re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]{8,}"), "Bearer [redacted]"),
    (re.compile(r"(?i)\b(postgres(?:ql)?|https?|wss?)://[^\s'\"]+"), r"\1://[redacted]"),
    (re.compile(r"(?i)(token|secret|password|api[_-]?key|passphrase)=\S+"), r"\1=[redacted]"),
    # Long opaque tokens (letters AND digits), not hyphenated worker names.
    (re.compile(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{32,}\b"),
     "[redacted]"),
)


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


@dataclass
class ProblemGroup:
    logger: str
    level: str
    count: int
    first_at: datetime
    last_at: datetime
    text: str


class RecentProblems(logging.Handler):
    """Logging handler that keeps WARNING+ lines grouped in a bounded buffer."""

    def __init__(self, *, clock=lambda: datetime.now(UTC)) -> None:
        super().__init__(level=logging.WARNING)
        self._clock = clock
        self._groups: OrderedDict[tuple[str, str, str], ProblemGroup] = OrderedDict()
        self._mutex = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
            if record.exc_info and record.exc_info[0] is not None:
                text = f"{text} [{record.exc_info[0].__name__}: {record.exc_info[1]}]"
            text = redact(text.splitlines()[0] if text else "")[:MAX_TEXT]
            key = (record.name, record.levelname, str(record.msg)[:120])
            now = self._clock()
            with self._mutex:
                group = self._groups.pop(key, None)
                if group is None:
                    group = ProblemGroup(record.name, record.levelname, 0, now, now, text)
                group.count += 1
                group.last_at, group.text = now, text
                self._groups[key] = group
                while len(self._groups) > MAX_GROUPS:
                    self._groups.popitem(last=False)
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)

    def recent(self, *, since: timedelta = timedelta(hours=24),
               limit: int = 6) -> list[ProblemGroup]:
        """Errors before warnings, then most frequent, within the window."""
        cutoff = self._clock() - since
        with self._mutex:
            groups = [g for g in self._groups.values() if g.last_at >= cutoff]
        groups.sort(key=lambda g: (g.level != "ERROR" and g.level != "CRITICAL",
                                   -g.count, -g.last_at.timestamp()))
        return groups[:limit]

    def totals(self, *, since: timedelta = timedelta(hours=24)) -> dict[str, int]:
        cutoff = self._clock() - since
        out = {"ERROR": 0, "WARNING": 0}
        with self._mutex:
            for group in self._groups.values():
                if group.last_at >= cutoff:
                    level = "ERROR" if group.level in ("ERROR", "CRITICAL") else "WARNING"
                    out[level] += group.count
        return out


_INSTALLED: RecentProblems | None = None


def install(logger_names: tuple[str, ...] = ("parlay_bot", "discord")) -> RecentProblems:
    """Attach one shared handler to RivaL's and discord.py's loggers (idempotent).

    discord.py's own warnings (gateway reconnects, rate limits, failed views)
    are part of RivaL's health, so they are captured too.
    """
    global _INSTALLED
    if _INSTALLED is None:
        _INSTALLED = RecentProblems()
        for name in logger_names:
            logging.getLogger(name).addHandler(_INSTALLED)
    return _INSTALLED
