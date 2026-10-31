"""Official macro/regulatory event observations for RivaL's existing research brain.

This layer records public facts, first-seen timestamps and source health. It
does not estimate market direction, sentiment, surprise, probabilities or trade
returns. Calendar releases can withhold new WATCH decisions near event time.
An LLM may explain the recorded context but cannot override the rule.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

_LOG = logging.getLogger(__name__)
SOURCES = {
    "fed_monetary": ("https://www.federalreserve.gov/feeds/press_monetary.xml", "rss"),
    "sec_releases": ("https://www.sec.gov/news/pressreleases.rss", "rss"),
    "bls_calendar": ("https://www.bls.gov/schedule/news_release/bls.ics", "ics"),
}
POLL_EVERY = timedelta(minutes=30)
FRESH_FOR = timedelta(hours=2)
MAX_BYTES = 1_500_000
MAX_ITEMS = 250
ASSETS = ("BTC", "ETH", "SOL", "XRP")
BLS_HIGH_IMPACT = (
    "consumer price index", "employment situation", "producer price index",
    "job openings and labor turnover survey",
)
RELEVANT_REGULATION = re.compile(
    r"\b(crypto|cryptocurrency|digital asset|bitcoin|ethereum|ether|"
    r"stablecoin|tokeniz|exchange.traded fund|etf)\b", re.I,
)
RELEVANT_MACRO = re.compile(
    r"\b(fomc|federal open market|interest rate|monetary policy|"
    r"discount rate|federal funds)\b", re.I,
)


def _aware(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("UTC-aware timestamp required")
    return value.astimezone(UTC)


def _identity(source: str, original: str) -> str:
    return hashlib.sha256((source + "\0" + original).encode()).hexdigest()


def _official_link(source: str, url: str) -> str:
    """Never store redirect/injected links as validated official resources."""
    host = urlparse(SOURCES[source][0]).hostname
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != host or parsed.username:
        raise ValueError("unverified event source URL")
    return url


def _title(raw: str) -> str:
    # Feed text is untrusted, NEVER used as instructions to the LLM or code.
    cleaned = " ".join(raw.split())[:220]
    if not cleaned or any(ord(c) < 32 for c in cleaned):
        raise ValueError("invalid event title")
    return cleaned


def _rss(payload: bytes, source: str, now: datetime) -> list[dict]:
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise ValueError("DOCTYPE/entities are not permitted")
    root = ET.fromstring(payload)
    # RSS 2.0; fail closed on unexpected source formats.
    if root.tag != "rss":
        raise ValueError("unexpected official RSS format")
    results = []
    for item in root.findall("./channel/item")[:MAX_ITEMS]:
        title = _title(item.findtext("title") or "")
        if source == "sec_releases" and not RELEVANT_REGULATION.search(title):
            continue
        if source == "fed_monetary" and not RELEVANT_MACRO.search(title):
            continue
        url = _official_link(source, (item.findtext("link") or "").strip())
        raw_date = item.findtext("pubDate")
        if not raw_date:
            continue
        published = parsedate_to_datetime(raw_date)
        if published.utcoffset() is None:
            continue
        published = _aware(published)
        if not now - timedelta(days=90) <= published <= now + timedelta(minutes=5):
            continue
        results.append({
            "uid": _identity(source, (item.findtext("guid") or url).strip()),
            "title": title, "url": url, "category": (
                "regulatory_announcement" if source == "sec_releases"
                else "monetary_policy_announcement"
            ),
            "published_at": published.isoformat(), "scheduled_at": None,
        })
    return results


def _ics_time(property_name: str, raw: str) -> datetime | None:
    if "VALUE=DATE" in property_name.upper() and "VALUE=DATE-TIME" not in property_name.upper():
        return None  # All-day markers are not a high-impact release timestamp.
    if raw.endswith("Z"):
        return _aware(datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC))
    timezone = None
    for piece in property_name.split(";")[1:]:
        if piece.upper().startswith("TZID="):
            timezone = piece.split("=", 1)[1].strip('"')
    if timezone not in ("America/New_York", "US/Eastern"):
        return None
    return _aware(datetime.strptime(raw, "%Y%m%dT%H%M%S").replace(
        tzinfo=ZoneInfo(timezone),
    ))


def _ics(payload: bytes, now: datetime) -> list[dict]:
    raw = payload.decode("utf-8-sig")
    if "BEGIN:VCALENDAR" not in raw[:100]:
        raise ValueError("unexpected BLS calendar format")
    unfolded = []
    for line in raw.replace("\r\n", "\n").split("\n"):
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    events = []
    entry = None
    for line in unfolded:
        if line == "BEGIN:VEVENT":
            entry = {}
        elif line == "END:VEVENT" and entry is not None:
            title = _title(entry.get("SUMMARY", "")) if entry.get("SUMMARY") else ""
            if any(label in title.lower() for label in BLS_HIGH_IMPACT):
                at = entry.get("DTSTART")
                if at is not None and (now - timedelta(days=2) <= at <= now + timedelta(days=90)):
                    uid = entry.get("UID") or (title + at.isoformat())
                    events.append({
                        "uid": _identity("bls_calendar", uid[:512]), "title": title,
                        "url": SOURCES["bls_calendar"][0], "category": "scheduled_macro_release",
                        "published_at": None, "scheduled_at": at.isoformat(),
                    })
            entry = None
            if len(events) >= MAX_ITEMS:
                break
        elif entry is not None and ":" in line:
            key, value = line.split(":", 1)
            kind = key.split(";", 1)[0].upper()
            if kind == "DTSTART":
                try:
                    entry["DTSTART"] = _ics_time(key, value)
                except ValueError:
                    entry["DTSTART"] = None
            elif kind in ("SUMMARY", "UID"):
                entry[kind] = value.strip()
    return events


class EventIntelligence:
    """Bounded official-source polling plus read-only event context/risk."""

    def __init__(self, store):
        self.store = store
        self._ready = False
        self._lock = threading.Lock()

    def initialize(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            with self.store.connect() as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS rival_macro_sources (
                        source TEXT PRIMARY KEY,
                        last_attempt_at TEXT NOT NULL,
                        last_success_at TEXT,
                        status TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS rival_macro_events (
                        source TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        url TEXT NOT NULL,
                        category TEXT NOT NULL,
                        published_at TEXT,
                        scheduled_at TEXT,
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        content_sha256 TEXT NOT NULL,
                        PRIMARY KEY (source,event_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_macro_scheduled
                        ON rival_macro_events(category,scheduled_at);
                    CREATE INDEX IF NOT EXISTS idx_macro_observed
                        ON rival_macro_events(first_seen_at);
                    CREATE TABLE IF NOT EXISTS rival_macro_revisions (
                        source TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        content_sha256 TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        PRIMARY KEY(source,event_id,content_sha256)
                    );
                """)
            self._ready = True

    def _claim(self, source: str, now: datetime) -> bool:
        with self.store.connect() as conn:
            r = conn.execute(
                """INSERT INTO rival_macro_sources(source,last_attempt_at,status)
                   VALUES (?,?,'pending')
                   ON CONFLICT(source) DO UPDATE SET
                       last_attempt_at=excluded.last_attempt_at,
                       status=excluded.status
                   WHERE rival_macro_sources.last_attempt_at<=?""",
                (source, now.isoformat(), (now-POLL_EVERY).isoformat()),
            )
            return r.rowcount == 1

    def _status(self, source: str, now: datetime, state: str) -> None:
        with self.store.connect() as conn:
            if state == "ok":
                conn.execute(
                    "UPDATE rival_macro_sources SET last_success_at=?,status=? WHERE source=?",
                    (now.isoformat(), state, source),
                )
            else:
                conn.execute(
                    "UPDATE rival_macro_sources SET status=? WHERE source=?",
                    (state, source),
                )

    def _store(self, source: str, rows: list[dict], now: datetime) -> int:
        inserted = 0
        with self.store.connect() as conn:
            for item in rows:
                if source not in SOURCES or len(item["uid"]) != 64:
                    raise ValueError("invalid event provenance")
                # Only the source's own official link can identify this item.
                _official_link(source, item["url"])
                encoded = "|".join(str(item.get(k) or "") for k in (
                    "title", "url", "category", "published_at", "scheduled_at",
                ))
                digest = hashlib.sha256(encoded.encode()).hexdigest()
                conn.execute(
                    """INSERT INTO rival_macro_revisions
                       (source,event_id,content_sha256,observed_at)
                       VALUES (?,?,?,?) ON CONFLICT DO NOTHING""",
                    (source, item["uid"], digest, now.isoformat()),
                )
                result = conn.execute(
                    """INSERT INTO rival_macro_events(
                        source,event_id,title,url,category,published_at,scheduled_at,
                        first_seen_at,last_seen_at,content_sha256
                       ) VALUES (?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(source,event_id) DO UPDATE SET
                         title=excluded.title,url=excluded.url,
                         category=excluded.category,published_at=excluded.published_at,
                         scheduled_at=excluded.scheduled_at,
                         last_seen_at=excluded.last_seen_at,
                         content_sha256=excluded.content_sha256""",
                    (source, item["uid"], item["title"], item["url"], item["category"],
                     item["published_at"], item["scheduled_at"],
                     now.isoformat(), now.isoformat(), digest),
                )
                inserted += int(result.rowcount == 1)
        return inserted

    def refresh(self, *, now: datetime | None = None, client=None) -> dict:
        """Call only from the autonomous background worker, never a Discord command."""
        stamp = _aware(now or datetime.now(UTC))
        self.initialize()
        summary = {"sources_checked": 0, "sources_ok": 0, "events_seen": 0,
                   "source_failures": [], "forecast_probabilities_updated": False}
        owner = client is None
        http = client or httpx.Client(timeout=7, follow_redirects=False,
                                       headers={"User-Agent": "RivaL-research/1.0 (official feeds)"})
        try:
            for source, (url, kind) in SOURCES.items():
                if not self._claim(source, stamp):
                    continue
                summary["sources_checked"] += 1
                try:
                    with http.stream("GET", url, follow_redirects=False) as response:
                        response.raise_for_status()
                        if response.is_redirect:
                            raise ValueError("official source redirected")
                        payload = bytearray()
                        for chunk in response.iter_bytes():
                            payload.extend(chunk)
                            if len(payload) > MAX_BYTES:
                                raise ValueError("source size budget exceeded")
                    rows = (_rss(bytes(payload), source, stamp)
                            if kind == "rss" else _ics(bytes(payload), stamp))
                    summary["events_seen"] += self._store(source, rows, stamp)
                    summary["sources_ok"] += 1
                    self._status(source, stamp, "ok")
                except (httpx.HTTPError, ValueError, ET.ParseError, UnicodeError) as exc:
                    code = type(exc).__name__[:60]
                    self._status(source, stamp, "unavailable")
                    summary["source_failures"].append({"source": source, "error": code})
                    _LOG.warning("RivaL official event feed unavailable source=%s error=%s",
                                 source, code)
        finally:
            if owner:
                http.close()
        return summary

    def context(self, *, asset: str, horizon: int, now: datetime | None = None) -> dict:
        """No artificial 'all clear' when the official source could not be checked."""
        if asset not in ASSETS or horizon not in (5, 15, 60, 240):
            raise ValueError("unsupported event-context asset or horizon")
        stamp = _aware(now or datetime.now(UTC))
        try:
            with self.store.connect() as conn:
                health = conn.execute(
                    "SELECT source,last_success_at,status FROM rival_macro_sources",
                ).fetchall()
                events = conn.execute(
                    """SELECT source,title,url,category,published_at,scheduled_at,
                              first_seen_at,content_sha256
                       FROM rival_macro_events
                       WHERE (scheduled_at>=? AND scheduled_at<=?)
                          OR (first_seen_at>=? AND first_seen_at<=?)
                       ORDER BY first_seen_at DESC LIMIT 80""",
                    ((stamp-timedelta(minutes=35)).isoformat(),
                     (stamp+timedelta(days=3)).isoformat(),
                     (stamp-timedelta(hours=6)).isoformat(), stamp.isoformat()),
                ).fetchall()
        except Exception:  # noqa: BLE001 - read-only legacy stores may not have this table yet
            return {"status": "unavailable", "decision": "not_verified",
                    "event_risk_cleared": False, "events": [], "sources": {}}
        sources = {
            row["source"]: (
                "fresh" if row["status"] == "ok" and row["last_success_at"]
                and timedelta(0) <= stamp-datetime.fromisoformat(
                    str(row["last_success_at"])
                ).astimezone(UTC) <= FRESH_FOR else "unavailable_or_stale"
            ) for row in health
        }
        candidates = []
        paused = False
        for raw in events:
            e = dict(raw)
            scheduled = (datetime.fromisoformat(str(e["scheduled_at"]))
                         if e["scheduled_at"] else None)
            observed = datetime.fromisoformat(str(e["first_seen_at"]))
            published = (datetime.fromisoformat(str(e["published_at"]))
                         if e["published_at"] else None)
            release = e["category"] == "scheduled_macro_release"
            # Scheduled data gets a cautious no-new-watch window from 15m
            # before any forecast horizon crosses the event until 30m after.
            around_release = (release and scheduled is not None
                              and stamp-timedelta(minutes=30) <= scheduled
                              <= stamp+timedelta(minutes=horizon+15))
            # A newly recorded official FOMC *statement* can invalidate
            # stale live forecasts even if it arrived after the statement.
            fed_statement = (
                e["source"] == "fed_monetary"
                and "fomc statement" in e["title"].lower()
                and published is not None
                and timedelta(0) <= stamp-published <= timedelta(minutes=45)
                and timedelta(0) <= stamp-observed <= timedelta(minutes=45)
            )
            pause = around_release or fed_statement
            if pause:
                paused = True
            if release or (stamp-observed <= timedelta(hours=3)
                           and observed <= stamp):
                candidates.append({
                    "source": e["source"], "title": e["title"],
                    "source_url": e["url"], "category": e["category"],
                    "first_seen_at": e["first_seen_at"],
                    "source_published_at": e["published_at"],
                    "scheduled_at": e["scheduled_at"],
                    "content_sha256": e["content_sha256"],
                    "risk_window_active": bool(pause),
                    "market_direction_inferred": False,
                })
        candidates.sort(key=lambda e: (not e["risk_window_active"],
                                        e["scheduled_at"] or e["first_seen_at"]))
        # A valid source response is not necessarily complete global-news
        # coverage. No feed data means unknown, NEVER an empirical all clear.
        return {
            "status": "official_sources_available" if any(
                value == "fresh" for value in sources.values()
            ) else "unavailable",
            "decision": "pause" if paused else "no_verified_active_pause",
            "event_risk_cleared": False,
            "events": candidates[:8],
            "sources": {name: sources.get(name, "not_checked") for name in SOURCES},
            "probabilities_from_news": None,
            "historical_first_seen_invented": False,
        }
