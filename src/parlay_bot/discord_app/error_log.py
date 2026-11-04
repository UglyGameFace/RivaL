"""Digest of recent RivaL warnings and errors for the status card.

Groups repeated log lines by logger and message template, counts them and
keeps the latest rendered text, redacted and truncated; secrets that slip
into a message are masked. Counts are also saved per UTC hour to
``rival_problem_log`` (``flush``), so the card's "last 24 h" and the
auto-repair task see real history across restarts instead of only the
time since the last deploy.
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
        # (hour, logger, level, template) -> [new count, last_at, text], not yet saved.
        self._pending: dict[tuple[str, str, str, str], list] = {}

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
                hour = now.astimezone(UTC).strftime("%Y-%m-%dT%H:00:00+00:00")
                pending = self._pending.setdefault((hour, *key), [0, now, text])
                pending[0] += 1
                pending[1], pending[2] = now, text
                if len(self._pending) > MAX_GROUPS * 4:  # database down for long
                    self._pending.pop(next(iter(self._pending)))
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


    # ---- durable history ----------------------------------------------
    SCHEMA = """CREATE TABLE IF NOT EXISTS rival_problem_log (
        hour TEXT NOT NULL, logger TEXT NOT NULL, level TEXT NOT NULL,
        template TEXT NOT NULL, count INTEGER NOT NULL, last_at TEXT NOT NULL,
        text TEXT NOT NULL, PRIMARY KEY (hour, logger, level, template))"""

    def flush(self, store) -> int:
        """Add unsaved counts to the hourly table; on failure they are kept."""
        with self._mutex:
            pending, self._pending = self._pending, {}
        if not pending:
            return 0
        try:
            with store.connect() as conn:
                conn.execute(self.SCHEMA)
                for (hour, logger, level, template), (count, last_at, text) in pending.items():
                    conn.execute(
                        """INSERT INTO rival_problem_log
                           (hour,logger,level,template,count,last_at,text)
                           VALUES (?,?,?,?,?,?,?)
                           ON CONFLICT(hour,logger,level,template) DO UPDATE SET
                             count=rival_problem_log.count+excluded.count,
                             last_at=excluded.last_at, text=excluded.text""",
                        (hour, logger, level, template, count, last_at.isoformat(), text))
                cutoff = (self._clock() - timedelta(days=8)).strftime("%Y-%m-%dT%H:00:00+00:00")
                conn.execute("DELETE FROM rival_problem_log WHERE hour<?", (cutoff,))
        except Exception:
            with self._mutex:  # put them back so nothing is lost
                for key, value in pending.items():
                    merged = self._pending.setdefault(key, [0, value[1], value[2]])
                    merged[0] += value[0]
            raise
        return len(pending)

    def stored(self, store, *, since: timedelta = timedelta(hours=24),
               limit: int = 6) -> tuple[list[ProblemGroup], dict[str, int]]:
        """The card's problems and totals from saved history plus unsaved counts."""
        cutoff = self._clock() - since
        hour_cutoff = cutoff.astimezone(UTC).strftime("%Y-%m-%dT%H:00:00+00:00")
        with store.connect() as conn:
            conn.execute(self.SCHEMA)
            rows = conn.execute(
                """SELECT logger, level, template, SUM(count) AS n, MIN(hour) AS first_hour,
                          MAX(last_at) AS last_at
                   FROM rival_problem_log WHERE hour>=?
                   GROUP BY logger, level, template""", (hour_cutoff,)).fetchall()
            texts = {
                (r["logger"], r["level"], r["template"]): r["text"]
                for r in conn.execute(
                    """SELECT p.logger, p.level, p.template, p.text FROM rival_problem_log p
                       WHERE p.hour>=? ORDER BY p.last_at""", (hour_cutoff,)).fetchall()}
        merged: dict[tuple[str, str, str], ProblemGroup] = {}
        for r in rows:
            key = (r["logger"], r["level"], r["template"])
            last = datetime.fromisoformat(str(r["last_at"]))
            merged[key] = ProblemGroup(r["logger"], r["level"], int(r["n"]),
                                       datetime.fromisoformat(str(r["first_hour"])), last,
                                       texts.get(key, ""))
        with self._mutex:
            unsaved = [(k[1:], v) for k, v in self._pending.items()]
        for key, (count, last_at, text) in unsaved:
            group = merged.get(key)
            if group is None:
                merged[key] = ProblemGroup(key[0], key[1], count, last_at, last_at, text)
            else:
                group.count += count
                if last_at > group.last_at:
                    group.last_at, group.text = last_at, text
        groups = [g for g in merged.values() if g.last_at >= cutoff]
        totals = {"ERROR": 0, "WARNING": 0}
        for g in groups:
            totals["ERROR" if g.level in ("ERROR", "CRITICAL") else "WARNING"] += g.count
        groups.sort(key=lambda g: (g.level != "ERROR" and g.level != "CRITICAL",
                                   -g.count, -g.last_at.timestamp()))
        return groups[:limit], totals


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
